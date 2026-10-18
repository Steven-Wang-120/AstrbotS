"""Guardian 守护系统 —— 巡检编排引擎。

对应文档：B03 §7-§9、B15 §3.3

职责：探针并发执行 -> 判定 -> 安全授权 -> 动作执行 -> 事件记录 -> 通知。
全程 asyncio，阻塞操作下沉到 to_thread / 子进程；重入受锁保护。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..actions.base import get_action
from ..notify.notifier import Notifier
from ..utils.textutil import fmt_ts, human_duration, status_icon
from . import registry  # noqa: F401  触发探针注册
from .announce import SleepyFacts, now_text
from .audit import AuditLog
from .bus import EventBus
from .config import DEFAULTS_LIFECYCLE_PROMPTS, GuardianConfig
from .lifecycle import LifecyclePolicy, LifecycleState, drowsy_visible
from .log import logger
from .models import (
    ActionSpec,
    Finding,
    Incident,
    ProbeResult,
    Severity,
    Status,
    severity_at_least,
    worst_status,
)
from .policy import PolicyEngine
from .safety import SafetyGuard
from .state import StateStore
from .timewindow import in_window, minutes_until_window

MAX_INCIDENTS_KEPT = 200


class HealthEngine:
    def __init__(self, config: GuardianConfig, state: StateStore, audit: AuditLog,
                 bus: EventBus, notifier: Notifier, safety: SafetyGuard,
                 policy: PolicyEngine, lifecycle_policy: LifecyclePolicy,
                 deps: dict[str, Any]) -> None:
        self.config = config
        self.state = state
        self.audit = audit
        self.bus = bus
        self.notifier = notifier
        self.safety = safety
        self.policy = policy
        self.lifecycle_policy = lifecycle_policy
        self.deps = deps
        deps["engine"] = self

        self.lifecycle_state = LifecycleState.from_dict(state.get("lifecycle.state"))
        self.mode_adapter = None
        self._lock = asyncio.Lock()
        self._watchdog_lock = asyncio.Lock()
        self._last_results: dict[str, ProbeResult] = {}
        self._last_run_ts: float = 0.0
        self._last_trigger = ""
        self._incidents: list[Incident] = []
        self._probe_sem = asyncio.Semaphore(6)

    # ================================================================== #
    # 启动 / 模式
    # ================================================================== #
    async def start(self) -> None:
        from ..modes.factory import create_mode
        mode = self.state.get("mode") or self.config.mode
        self.mode_adapter = create_mode(mode, self.config, self.deps)
        self.state.set("mode", mode)
        logger.info(f"[guardian] 引擎启动 | {self.mode_adapter.describe()}")

    async def apply_mode(self, mode: str, reason: str = "") -> None:
        from ..modes.factory import create_mode
        if self.mode_adapter is not None:
            await self.mode_adapter.close()
        self.mode_adapter = create_mode(mode, self.config, self.deps)
        self.state.set("mode", mode)
        self._last_results.clear()
        self.state.set("cooldowns", {})
        self.audit.append(kind="mode.switch", target=mode, detail=reason)
        logger.info(f"[guardian] 模式切换 -> {mode}（{reason}）")

    async def apply_profile(self, profile: str, reason: str = "") -> None:
        self.state.set("profile", profile)
        self.audit.append(kind="profile.switch", target=profile, detail=reason)
        logger.info(f"[guardian] 档位切换 -> {profile}（{reason}）")
        await self.bus.publish("profile.changed", {"profile": profile})

    async def close(self) -> None:
        if self.mode_adapter is not None:
            await self.mode_adapter.close()

    # ================================================================== #
    # 巡检
    # ================================================================== #
    async def run_once(self, trigger: str = "interval", scope: str = "all") -> dict[str, Any]:
        from ..utils.watchdog import reconcile
        await reconcile(self)
        if self._lock.locked():
            logger.debug("[guardian] 上一次巡检未结束，跳过本次触发")
            return {"skipped": True, "reason": "busy"}
        async with self._lock:
            started = time.time()
            self._last_trigger = trigger
            probes = self._select_probes(scope)
            results = await self._run_probes(probes)
            current = {f"{r.probe}:{r.target}": r for r in results}
            if scope in ("all", "", None):
                self._last_results = current
            else:
                # 独立 QQ/局部巡检不能抹掉其他探针的最新健康快照。
                self._last_results.update(current)
            self._last_run_ts = time.time()
            self._persist_tz_offset()

            findings = self.policy.evaluate(results)
            incidents = await self._handle_findings(findings, trigger)

            # 生命周期推进（仅 full 巡检时）
            if scope == "all" and self.config.lifecycle.enabled:
                await self._lifecycle_tick(trigger)

            await self.state.flush()
            elapsed = int((time.time() - started) * 1000)
            self.audit.append(
                kind="patrol", detail=f"探针 {len(results)} 项，事件 {len(incidents)} 条",
                trigger=trigger, elapsed_ms=elapsed, ok=True)
            return {
                "skipped": False,
                "elapsed_ms": elapsed,
                "results": [r.to_dict() for r in results],
                "incidents": [i.to_dict() for i in incidents],
            }

    def _select_probes(self, scope: str) -> list[Any]:
        all_probes = self.mode_adapter.build_probes() if self.mode_adapter else []
        if scope in ("all", "", None):
            return all_probes
        allow: set[str] = set()
        if scope == "qq":
            allow = {"qq_login"}
        elif scope == "containers":
            allow = {"docker_daemon", "container_health", "container_memory", "self_health"}
        elif scope == "ros2":
            allow = {"ros2_nodes", "ros2_topics", "ros2_doctor"}
        return [p for p in all_probes if p.name in allow]

    async def _run_probes(self, probes: list[Any]) -> list[ProbeResult]:
        async def _one(probe) -> ProbeResult:
            async with self._probe_sem:
                return await probe.check()
        if not probes:
            return []
        return list(await asyncio.gather(*[_one(p) for p in probes]))

    # ================================================================== #
    # QQ 独立巡检（20 分钟节拍）
    # ================================================================== #
    async def run_qq_check(self) -> None:
        await self.run_once(trigger="qq_interval", scope="qq")

    # ================================================================== #
    # 判定 -> 动作
    # ================================================================== #
    async def _handle_findings(self, findings: list[Finding],
                               trigger: str) -> list[Incident]:
        incidents: list[Incident] = []
        last_status: dict[str, str] = dict(self.state.get("last_status") or {})
        to_notify: list[Incident] = []

        for finding in findings:
            key = f"{finding.probe}:{finding.target}"
            prev = last_status.get(key, "unknown")
            now_val = finding.status.value
            last_status[key] = now_val
            if finding.probe == "qq_login" and prev != now_val:
                logger.info(f"[guardian] QQ 登录态变更: {prev} -> {now_val} | {finding.reason}")

            if finding.status == Status.OK:
                if prev in ("warn", "crit"):
                    inc = Incident(
                        probe=finding.probe, target=finding.target,
                        status=Status.OK, severity=Severity.INFO,
                        message=f"{finding.target} 已恢复正常",
                        trigger=trigger)
                    incidents.append(inc)
                    to_notify.append(inc)
                continue

            inc = Incident(
                probe=finding.probe, target=finding.target,
                status=finding.status, severity=finding.severity,
                message=finding.reason, trigger=trigger)

            # 主动睡眠窗口：目标是被我们自己故意睡下的，不是故障。
            # 不动作、不推送，但**仍然记录**（I4：抑制要留痕，不能静默消失）。
            sleeping = self.sleeping_targets()
            if finding.target in sleeping:
                inc.suppressed = True
                inc.suppress_reason = "sleeping"
                inc.severity = Severity.INFO
                incidents.append(inc)
                continue

            # 动作（maintenance 档位不动作）
            #
            # profile 必须**每轮重新读取**：本轮前面的动作可能刚因连续失败把档位
            # 自动降级为 maintenance（B00 §10.6）。若沿用循环开始时的旧值，
            # 降级后同一趟巡检里剩余的 finding 仍会继续执行破坏性动作。
            profile = self.state.get("profile") or self.config.profile
            if finding.suggested_action is not None and profile != "maintenance":
                spec = finding.suggested_action
                spec.actor = "system"
                spec.trigger = trigger
                await self._execute_action(spec, inc)

            # 跃迁去噪：同状态重复且非 CRIT 以上，不重复推送
            is_transition = prev != now_val
            if is_transition or severity_at_least(inc.severity, Severity.CRIT):
                to_notify.append(inc)
            incidents.append(inc)

        self.state.set("last_status", last_status)

        # 记录与通知
        for inc in incidents:
            self._record_incident(inc)
        if to_notify:
            await self.notifier.push_incidents(to_notify)
            for inc in to_notify:
                inc.notified = True
        return incidents

    async def _execute_action(self, spec: ActionSpec, inc: Incident) -> None:
        decision = self.safety.authorize(spec)
        inc.action = spec
        if not decision.allowed:
            inc.suppressed = True
            inc.suppress_reason = decision.reason
            self.audit.append(kind="action.denied", target=spec.target,
                              detail=f"{spec.kind}: {decision.reason}",
                              suppressed=True, reason=decision.code)
            return
        spec.dry_run = decision.dry_run
        action_cls = get_action(spec.kind)
        if action_cls is None:
            inc.suppressed = True
            inc.suppress_reason = f"未知动作 {spec.kind}"
            return
        action = action_cls(self.config, self.deps)
        result = await action.execute(spec)
        inc.result = result
        if not spec.params.get("_watchdog_committed"):
            degraded = self.safety.commit(spec, result.ok)
            # 用 `spec.trigger`（由 `_handle_findings` 在调用前写入），
            # 而不是裸 `trigger`——本方法没有该形参，此前会抛 NameError，
            # 导致**任何**由巡检发现触发的动作在动作已执行之后崩溃：
            # 动作真的做了，但 `action.execute` 审计与 `action.done` 事件双双丢失。
            await self._note_degradation(degraded, spec.trigger)
        self.audit.append(
            kind="action.execute", target=spec.target,
            detail=f"{spec.kind} -> {result.detail}",
            actor=spec.actor, trigger=spec.trigger, ok=result.ok,
            elapsed_ms=result.elapsed_ms, params=spec.params)
        await self.bus.publish("action.done", {"spec": spec.to_dict(),
                                              "result": result.to_dict()})

    # ================================================================== #
    # 连续失败自动降级（B00 §10.6）
    # ================================================================== #
    async def _note_degradation(self, degraded: list[tuple[str, int]],
                                trigger: str = "") -> None:
        """把"连续失败达阈值"落实为：只告警 + 进入 maintenance 档位。

        safety 层已保证同一目标只在**恰好跨过阈值**时上报一次；这里再做一次
        幂等保护：已经处于 maintenance 就只补审计与通知，不重复切换档位。
        """
        if not degraded:
            return
        targets = ", ".join(f"{t}(x{n})" for t, n in degraded)
        self.audit.append(kind="safety.degrade", target=targets,
                          detail="连续失败达阈值，降级为只告警并进入 maintenance",
                          trigger=trigger, suppressed=True, reason="fail_streak")
        current = self.state.get("profile") or self.config.profile
        if current != "maintenance":
            await self.apply_profile(
                "maintenance",
                reason=f"目标 {targets} 连续失败达阈值，自动降级等待人工")
        await self.notifier.push(
            f"[安全降级] 目标 {targets} 连续失败达到阈值，已自动切换为"
            "「维护档位」：暂停全部自动动作、静默告警，等待人工介入。",
            severity=Severity.FATAL, force=True, target="safety", status="degrade")

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    #: 顺延上限的默认值，仅在配置缺失时兜底（配置层已夹到 [1, 72]）。
    DEFAULT_MAX_DEFER_H = 12.0

    def _circadian_cfg(self) -> tuple[bool, str, str, float]:
        """\u53d6 (enabled, start, end, max_defer_h)。配置段缺失一律视为禁用。"""
        raw = self.config.get("circadian")
        if not isinstance(raw, dict):
            return False, "02:00", "07:00", self.DEFAULT_MAX_DEFER_H
        try:
            max_defer = float(raw.get("max_defer_hours", self.DEFAULT_MAX_DEFER_H))
        except (TypeError, ValueError):
            max_defer = self.DEFAULT_MAX_DEFER_H
        return (
            bool(raw.get("enabled", False)),
            str(raw.get("asleep_start", "02:00")),
            str(raw.get("asleep_end", "07:00")),
            max_defer,
        )

    def _tz_offset_h(self) -> float:
        """本地时间相对 UTC 的偏移（小时）。

        只读取 `env_platform` 探针落库的值：进程内的 `time.timezone` 在容器里
        经常是 0（镜像没设 `TZ`），用它算窗口会把 02:00 当成 UTC 02:00。
        探针拿不到时回退 +8（本项目默认部署时区），而不是不判——不判等于
        窗口永不生效，比"按东八区算"更糟。
        """
        value = self.state.get("env.tz_offset_h")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 8.0
        offset = float(value)
        if not -14.0 <= offset <= 14.0:
            return 8.0
        return offset

    def in_asleep_window(self, now: float | None = None) -> bool:
        """此刻是否**允许**入睡。禁用或配置非法时为 True（不设限）。"""
        enabled, start, end, _ = self._circadian_cfg()
        if not enabled:
            return True
        return in_window(time.time() if now is None else now, start, end,
                         self._tz_offset_h())

    def _max_defer_h(self) -> float:
        """顺延上限（小时）。配置层已夹到 [1, 72]，这里只做取值。"""
        return self._circadian_cfg()[3] or self.DEFAULT_MAX_DEFER_H

    def _recent_talk(self, now: float) -> bool:
        """最近 `quiet_minutes` 分钟内是否有人说过话。

        `last_event_ts` 由适配层 `note_event()` 写入（带 10s 合并），因此
        这里不需要新增任何适配层代码，也不需要解析任何对话内容。
        """
        raw = self.config.get("circadian")
        quiet_min = 10.0
        if isinstance(raw, dict):
            try:
                quiet_min = float(raw.get("quiet_minutes", quiet_min))
            except (TypeError, ValueError):
                quiet_min = 10.0
        if quiet_min <= 0:
            return False
        last = self.state.get("last_event_ts")
        if isinstance(last, bool) or not isinstance(last, (int, float)):
            return False
        age = now - float(last)
        # 负数说明时钟被回拨过，不能据此判定"刚说过话"
        return 0.0 <= age < quiet_min * 60.0

    def _deferred_too_long(self, now: float) -> bool:
        """顺延是否已超过 `max_defer_hours`（超过即无视窗口强制执行）。"""
        since = self.lifecycle_state.deferred_since
        if not since:
            return False
        return (now - float(since)) >= self._max_defer_h() * 3600.0

    def _defer_sleep_reason(self, now: float, *, in_win: bool,
                            recent_talk: bool) -> str:
        """给"这次为什么没睡"一句运维能看懂的话。"""
        if not in_win:
            _, start, end, _ = self._circadian_cfg()
            reopen = self._window_open_ts(now)
            wait_min = max(0, int((reopen - now) / 60))
            return (f"当前不在作息窗口（{start}-{end}），"
                    f"约 {wait_min // 60} 小时 {wait_min % 60} 分后开启；本次睡眠已推迟")
        if recent_talk:
            return ("最近有人在对话，暂不打断；本次睡眠已推迟"
                    "（顺延超过上限后将无视窗口强制入睡）")
        return "作息窗口判定推迟本次睡眠"

    def _l1_threshold_h(self) -> float:
        """L1 的 `after_hours`，作为"困意开始可见"的门槛。

        `lifecycle.levels` 允许用户自定义，所以不能硬编码 168。取"最低一个
        after_hours > 0 的等级"阈值；若用户只配了 L0（或全部为 0），退回 0，
        此时只要等级进入 arm_sleep 就算困——那是用户自己配出来的语义。
        """
        best: float | None = None
        for spec in (self.lifecycle_policy.levels or []):
            hours = float(getattr(spec, "after_hours", 0.0) or 0.0)
            if hours > 0 and (best is None or hours < best):
                best = hours
        return 0.0 if best is None else best

    def _window_open_ts(self, now: float) -> float:
        """下一次窗口开启的时刻（已在窗口内则返回 now）。"""
        enabled, start, end, _ = self._circadian_cfg()
        if not enabled:
            return now
        lead = minutes_until_window(now, start, end, self._tz_offset_h())
        if lead <= 0:
            return now
        return now + lead * 60.0

    # ================================================================== #
    # 主动消息：Bot 自己去跟管理员说要睡了
    # ================================================================== #
    # 分工：
    #   - 这里只回答「现在该不该开口」+ 「要告诉模型哪些事实」；
    #   - 「具体怎么说」由适配层 `compose_announce()` 调 LLM 生成。
    #
    # 内核进程不持有 provider 凭据、也不 import astrbot（I6），所以它不能
    # 自己调模型。这个边界不是洁癖：`kernel.mode=ipc` 时内核是独立进程，
    # 让它去要凭据等于把 LLM 配置复制一份到内核侧，且一旦 provider 不可用
    # 内核会被网络超时卡住。
    #
    # **没有冷却参数**：Bot 说完就去睡，睡下后不可能再发第二条，频率限制
    # 在这个场景里没有约束对象。要防的只有"同一件事说两遍"，用事件键去重。
    ANNOUNCE_KINDS = ("sleep", "deferred", "resume")
    ANNOUNCE_DEFAULT_MAX_UNANSWERED = 3

    def announce_config(self) -> dict[str, Any]:
        """主动消息的有效配置（含模板回退），供适配层与状态展示使用。"""
        raw = self.config.get("lifecycle")
        lc = raw if isinstance(raw, dict) else {}

        mode = str(lc.get("sleepy_announce", "on_arm") or "on_arm").strip().lower()
        if mode not in ("off", "on_arm", "always"):
            mode = "on_arm"

        try:
            max_unanswered = int(lc.get("sleepy_announce_max_unanswered",
                                        self.ANNOUNCE_DEFAULT_MAX_UNANSWERED))
        except (TypeError, ValueError):
            max_unanswered = self.ANNOUNCE_DEFAULT_MAX_UNANSWERED
        # 0 / 负数 = 不设限（用户显式关掉自限）。上限 50 只为防手滑填个天文数字。
        max_unanswered = max(0, min(50, max_unanswered))

        # 模板留空 -> 用内置。用户没改过就跟着插件升级拿到新文案，
        # 而不是被一份早期快照钉死。
        prompts: dict[str, str] = {}
        for key, default in DEFAULTS_LIFECYCLE_PROMPTS.items():
            value = lc.get(key)
            text = str(value).strip() if isinstance(value, str) else ""
            prompts[key] = text or default

        return {
            "mode": mode,
            "post_sleep": bool(lc.get("post_sleep_announce", True)),
            "max_unanswered": max_unanswered,
            "prompts": prompts,
        }

    def peer_stats(self) -> dict[str, Any]:
        """该报备给谁。

        不做任何对话内容解析（P1）：只看消息来自哪个会话，以及发送者是不是
        管理员。适配层在 `note_event()` 里写这两个值，这里只读。

        优先级：管理员 > 最近说话的人 > `notify.umo_list[0]`。
        管理员的写入绕过 10 秒合并窗口，否则同一时段群里有人说话就会把
        地址覆盖掉，报备消息会发给一个普通群友。
        """
        for key in ("announce.admin_umo", "announce.last_umo"):
            value = self.state.get(key)
            if isinstance(value, str) and value.strip():
                return {
                    "umo": value.strip(),
                    "place": str(self.state.get(key.replace("_umo", "_place")) or ""),
                    "count": int(self.state.get("announce.unanswered") or 0),
                    "from_admin": key == "announce.admin_umo",
                }
        # 兜底：用户配了 notify.umo_list 就直接用它（老部署的既有行为）。
        raw = self.config.get("notify")
        if isinstance(raw, dict):
            targets = raw.get("umo_list") or []
            if isinstance(targets, str):
                targets = [targets]
            for t in targets:
                if isinstance(t, str) and t.strip():
                    return {"umo": t.strip(), "place": "", "count": 0,
                            "from_admin": False}
        return {"umo": "", "place": "", "count": 0, "from_admin": False}

    def unanswered_count(self) -> int:
        try:
            return max(0, int(self.state.get("announce.unanswered") or 0))
        except (TypeError, ValueError):
            return 0

    def note_unanswered(self, answered: bool = False) -> int:
        """更新"说了但没人应"的累计数。返回更新后的值。

        `answered=True` 是任何一条用户消息到达时调用的：立刻清零。
        否则"昨晚连说三次没人理"会一直挂着，今天有人说话了反而继续沉默。
        """
        value = 0 if answered else min(999, self.unanswered_count() + 1)
        self.state.set("announce.unanswered", value)
        return value

    def announce_state(self) -> dict[str, Any]:
        """运维视角的主动消息状态：说了没、什么时候说的、被什么挡过。"""
        st = self.lifecycle_state
        cfg = self.announce_config()
        return {
            "mode": cfg["mode"],
            "post_sleep": cfg["post_sleep"],
            "max_unanswered": cfg["max_unanswered"],
            "unanswered": self.unanswered_count(),
            "announced_key": st.announced_key,
            "announced_at": st.announced_at,
            "announced_at_text": (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(st.announced_at))
                if st.announced_at else ""),
            "peer": self.peer_stats(),
        }

    def _announce_key(self, facts: dict[str, Any]) -> str:
        """把一次"事件"折叠成一个稳定标识，用于"这件事说过了吗"。

        粒度取整到小时：同一等级下 uptime 每涨一小时才算新的一轮。
        如果取精确值，L3 报备之后每 120 秒轮询都会算出不同的键，去重失效；
        如果只取等级，管理员推迟一次之后又不肯再说第二次。
        """
        kind = str(facts.get("kind") or "")
        if kind == "resume":
            # 醒来事件天然唯一：绑在"上次入睡时刻"上。
            return f"resume:{int(self.lifecycle_state.last_sleep_at)}"
        level = str(facts.get("level_id") or "")
        if kind == "deferred":
            # 顺延事件按"顺延起点"折叠：同一次顺延只报一次。
            since = int(self.lifecycle_state.deferred_since or 0)
            return f"deferred:{level}:{since}"
        uptime_h = float(facts.get("uptime_hours") or 0.0)
        return f"sleep:{level}:{int(uptime_h)}"

    def claim_announce(self, key: str, *, now: float | None = None) -> bool:
        """占用"这件事我要说了"的标记。已占用则返回 False。

        **必须在 LLM 生成成功之后调用**，不能在算出 facts 时就调用：
        否则一次网络超时就把这一轮的事件键烧掉，接下来一小时都不会再试。
        """
        if not key:
            return False
        st = self.lifecycle_state
        if st.announced_key == key:
            return False
        st.announced_key = key
        st.announced_at = time.time() if now is None else float(now)
        self.state.set("lifecycle.state", st.to_dict())
        return True

    def sleepy_facts(self, now: float | None = None) -> dict[str, Any] | None:
        """现在该不该说"我要睡了"，以及要说清哪些事实。

        返回 `None` 表示这次不开口。返回的字典里带 `template_key`，
        适配层据此挑模板；模板本体从 `announce_config()` 取。

        只读：不改任何状态。占坑由调用方在 LLM 成功后 `claim_announce()`。

        三条闸门（顺序不可换）：

        1. `mode == off`               -> 不说
        2. 等级动作必须是 arm_sleep / force_sleep -> 否则不说
           （困意是 uptime 的函数，不是时钟的函数；L0-L2 不该喊困）
        3. `on_arm` 模式下必须"真睡得成"：窗口内、或顺延超限、或强制
           -> 否则不说（`always` 模式下改成说"还没睡成"，走 deferred）
        """
        cfg = self.announce_config()
        if cfg["mode"] == "off":
            return None

        res = self._last_results.get("uptime:system")
        if res is None or res.status != Status.OK:
            # uptime 不可信就不开口：宁可这一轮沉默，也不拿 0 小时去说
            # "我连续在线 0 小时，要睡了"。
            return None
        uptime_h = float(res.metrics.get("uptime_hours", 0.0) or 0.0)
        moment = time.time() if now is None else float(now)

        idx, spec = self.lifecycle_policy.resolve_level(uptime_h)
        action = str(getattr(spec, "action", "") or "none")
        # 只有 arm_sleep / force_sleep 才可能喊困。L1/L2 是 notify，
        # 它们靠 `lifecycle.inject_prompt` 在对话里渗透，不走主动消息。
        if action not in ("arm_sleep", "force_sleep"):
            return None

        enabled, start, end, _ = self._circadian_cfg()
        in_win = self.in_asleep_window(moment)
        forced = action == "force_sleep"
        deferred_too_long = self._deferred_too_long(moment)
        recent_talk = self._recent_talk(moment)

        # 真正睡得成的三个条件。`forced` 不看窗口：L4 已到硬上限，
        # 窗口让不让睡都得睡，这时候报备不能因为"现在不是凌晨"就不说。
        can_sleep = forced or deferred_too_long or (in_win and not recent_talk)

        if not can_sleep:
            if cfg["mode"] != "always":
                return None
            return self._deferred_facts(moment, cfg, spec, uptime_h, idx,
                                        in_win=in_win, recent_talk=recent_talk)

        hours_to_window = 0.0
        if enabled and not in_win:
            lead_min = minutes_until_window(moment, start, end, self._tz_offset_h())
            hours_to_window = max(0.0, lead_min / 60.0)

        facts = SleepyFacts(
            kind="sleep",
            level_id=str(getattr(spec, "id", "") or ""),
            level_name=str(getattr(spec, "name", "") or ""),
            uptime_hours=uptime_h,
            now_text=now_text(moment, self._tz_offset_h()),
            period=f"{start} - {end}" if enabled else "",
            hours_to_window=hours_to_window,
            in_window=in_win,
            duration_min=int(self.config.lifecycle.sleep_duration_min or 0),
            targets=tuple(self.config.lifecycle.sleep_targets or ()),
            remaining_hours=self._remaining_hours(uptime_h),
            forced=forced,
            recent_talk=recent_talk,
            reason=(f"等级 {getattr(spec, 'id', '')} 动作={action}"
                    + ("；已在硬上限，强制执行" if forced else "")
                    + ("" if in_win else "；窗口外但顺延已超限")),
        )
        data = facts.to_dict()
        data["template_key"] = "sleepy_prompt"
        data["event_key"] = self._announce_key(data)
        return data

    def _deferred_facts(self, moment: float, cfg: dict[str, Any], spec: Any,
                        uptime_h: float, idx: int, *,
                        in_win: bool, recent_talk: bool) -> dict[str, Any]:
        """`always` 模式专用：想睡但这次睡不成，说一声自己还醒着。"""
        enabled, start, end, _ = self._circadian_cfg()
        hours_to_window = 0.0
        if enabled and not in_win:
            lead_min = minutes_until_window(moment, start, end, self._tz_offset_h())
            hours_to_window = max(0.0, lead_min / 60.0)

        since = float(self.lifecycle_state.deferred_since or 0.0)
        wait_hours = max(0.0, (moment - since) / 3600.0) if since else 0.0

        facts = SleepyFacts(
            kind="deferred",
            level_id=str(getattr(spec, "id", "") or ""),
            level_name=str(getattr(spec, "name", "") or ""),
            uptime_hours=uptime_h,
            now_text=now_text(moment, self._tz_offset_h()),
            period=f"{start} - {end}" if enabled else "",
            hours_to_window=hours_to_window,
            in_window=in_win,
            duration_min=int(self.config.lifecycle.sleep_duration_min or 0),
            targets=tuple(self.config.lifecycle.sleep_targets or ()),
            remaining_hours=self._remaining_hours(uptime_h),
            forced=False,
            defer_reason=self._defer_sleep_reason(moment, in_win=in_win,
                                                  recent_talk=recent_talk),
            wait_hours=wait_hours,
            recent_talk=recent_talk,
            reason=f"等级 {getattr(spec, 'id', '')} 已想睡，但本次被顺延",
        )
        data = facts.to_dict()
        data["template_key"] = "sleepy_deferred_prompt"
        data["event_key"] = self._announce_key(data)
        return data

    def resume_facts(self, now: float | None = None) -> dict[str, Any] | None:
        """睡醒了，该不该报一句"我回来了"。只读。"""
        cfg = self.announce_config()
        if cfg["mode"] == "off" or not cfg["post_sleep"]:
            return None

        last_sleep = float(self.lifecycle_state.last_sleep_at or 0.0)
        if last_sleep <= 0:
            return None
        moment = time.time() if now is None else float(now)

        # 太旧的不报：插件重载后把三天前那次睡眠翻出来说"我醒了"会很怪。
        slept_minutes = int(max(0.0, (moment - last_sleep) / 60.0))
        if slept_minutes > 6 * 60:
            return None

        res = self._last_results.get("uptime:system")
        uptime_h = 0.0
        if res is not None and res.status == Status.OK:
            uptime_h = float(res.metrics.get("uptime_hours", 0.0) or 0.0)

        facts = SleepyFacts(
            kind="resume",
            level_id=str(self.lifecycle_state.level_id or ""),
            level_name=str(self.lifecycle_state.level_name or ""),
            uptime_hours=uptime_h,
            now_text=now_text(moment, self._tz_offset_h()),
            slept_minutes=slept_minutes,
            reason=f"上次入睡于 {time.strftime('%m-%d %H:%M', time.localtime(last_sleep))}",
        )
        data = facts.to_dict()
        data["template_key"] = "resume_prompt"
        data["event_key"] = self._announce_key(data)
        return data

    def _remaining_hours(self, uptime_h: float) -> float | None:
        try:
            return float(self.lifecycle_policy.remaining_to_deadline(uptime_h))
        except Exception:  # noqa: BLE001 - 展示用数字，算不出就当未知
            return None

    async def _lifecycle_tick(self, trigger: str) -> None:
        res = self._last_results.get("uptime:system")
        if res is None or res.status != Status.OK:
            # 只有 Status.OK 的 uptime 才可信。
            #
            # - UNKNOWN：Docker 不可达/启动时间缺失，本来就没有依据。
            # - WARN：`uptime` 探针只在"时钟回拨（容器启动时间在未来）"时给 WARN，
            #   此时 metrics 里没有 uptime_hours；若照旧往下走会取到 0.0，把等级
            #   静默降回 L0、清掉注入提示词，等时钟恢复正常后又重新注入一次。
            #   宁可保持上一轮的可信等级不动。
            return
        uptime_h = float(res.metrics.get("uptime_hours", 0.0) or 0.0)
        now = time.time()
        decision = self.lifecycle_policy.evaluate(uptime_h, self.lifecycle_state, now)

        # ---- 困意可见性（M3） ------------------------------------------ #
        # `drowsy_visible` 只决定"要不要把困意说出来/注入提示词"，**不参与**
        # `level_index` 与 `sleep_armed` 的计算——那两项永远只由 uptime 决定，
        # 窗口无权改写。这是"窗口是闸门不是触发器"在代码里的落点。
        drowsy = drowsy_visible(
            decision["action"], self.in_asleep_window(now), uptime_h,
            self._l1_threshold_h())
        if not drowsy and decision["inject_prompt"]:
            # 想睡但不在窗口内：把提示词压住，并记下"顺延从何时开始"。
            # 提示词已经在 evaluate() 里就消费掉了冷却，所以这里清了之后
            # 下一轮不会立刻又冒出来（注入冷却由 last_inject_at 掌管）。
            self.lifecycle_state.suppressed_until = self._window_open_ts(now)
            if not self.lifecycle_state.deferred_since:
                self.lifecycle_state.deferred_since = now
            self.audit.append(
                kind="lifecycle.suppressed", target=decision["level"].id,
                detail=(f"{decision['reason']} | 不在作息窗口内，"
                        "困意注入已推迟至窗口开启"),
                trigger=trigger, suppressed=True, reason="circadian_window")
            decision = dict(decision, inject_prompt=None)
        elif drowsy:
            # 回到窗口内：顺延计时结束（真正的清零在睡过一觉之后）。
            self.lifecycle_state.suppressed_until = 0.0

        if decision["inject_prompt"]:
            self.state.set("lifecycle.inject_prompt", decision["inject_prompt"])
            self.state.set("lifecycle.inject_level", decision["level"].id)
            self.audit.append(kind="lifecycle.inject", target=decision["level"].id,
                              detail=decision["reason"], trigger=trigger)
            logger.info(f"[guardian] 生命周期注入 {decision['level'].id}: "
                        f"{decision['reason']}")
            if decision["action"] in ("notify", "arm_sleep"):
                await self.notifier.push(
                    f"[生命周期] {decision['level'].name}：已提升引导等级"
                    f"（运行 {uptime_h:.0f} 小时）",
                    severity=Severity.INFO, target="lifecycle", status=decision["level"].id)
        else:
            self.state.set("lifecycle.inject_prompt", "")

        if decision["force_sleep"]:
            # 必须区分"等级自己配了 force_sleep"和"到达硬上限兜底"：两者都叫
            # force_sleep，但归因完全不同，日志与审计若都写成"硬上限触发"，
            # 运维就无法判断到底是哪条策略在要求睡眠。
            source = decision.get("force_source") or "deadline"
            source_txt = ("等级自身 action=force_sleep" if source == "level"
                          else "到达硬上限兜底")
            logger.warning(
                f"[guardian] 生命周期强制睡眠（{source_txt}）：{decision['reason']}")
            await self.request_sleep(
                reason=f"生命周期强制睡眠（{source_txt}：{decision['reason']}）",
                duration_min=self.config.lifecycle.sleep_duration_min,
                actor="lifecycle",
                trigger=source, bypass_confirm=True)

        self.state.set("lifecycle.state", self.lifecycle_state.to_dict())

    async def request_sleep(self, reason: str, duration_min: int = 0, *,
                            actor: str = "llm", trigger: str = "llm",
                            bypass_confirm: bool = False) -> tuple[bool, str]:
        """执行一次真睡眠。返回 (ok, detail)。

        二次确认（B12 §L-D / B09 §6.2）：`bypass_confirm=False` 时**拒绝执行**并返回
        确认提示。真睡眠会停机再启动目标容器（服务中断若干秒），属于破坏性动作；
        引擎作为权威执行者，不能假定调用方一定做过确认——否则一旦适配层漏了闸门，
        这里就成了无确认直通路径。只有权威来源才显式传 `bypass_confirm=True`：
        生命周期 force_sleep、定时任务、管理员命令、以及已完成二次确认的 LLM 请求。

        目标裁剪规则（纵深防御 + 优雅降级）：
        - 不在动作白名单内的目标：剔除
        - 受保护且未开启 allow_self_restart 的目标：剔除（通常是 astrbot 本体）
        - 若裁剪后为空：返回明确错误，并提示如何开启本体睡眠
        """
        cfg = self.config
        if not bypass_confirm:
            targets_txt = ", ".join(cfg.lifecycle.sleep_targets or []) or "(未配置)"
            msg = (f"需要二次确认：这是一次真睡眠（将重启 {targets_txt}，"
                   "服务会短暂中断）。请确认后以 confirm=true 重试。")
            self.audit.append(kind="sleep.confirm_required", target=targets_txt,
                              detail=msg, actor=actor, trigger=trigger,
                              suppressed=True, reason="confirm_required")
            return False, msg

        # ---- ④ 作息窗口闸门（M4） -------------------------------------- #
        # 只有**自主**来源才受窗口约束：等级推进与硬上限顺延。人（管理员命令）
        # 与定时任务（cron）是显式意图，不该被"现在是凌晨"驳回。
        #
        # 判据顺序不能换：
        #   1. 在窗口内且最近没人说话  -> 直接放行
        #   2. 顺延已超 max_defer_hours -> 无视窗口放行（30 天保证）
        #   3. 其余                    -> 推迟
        #
        # 第 2 步必须排在 `recent_talk` 之前。否则"整夜都有人在聊天"会让
        # recent_talk 永远为真，顺延上限被无限推后，"30 天必定真睡一次"就
        # 被聊穿了——这正是 max_defer_hours 存在的理由。
        if actor in ("lifecycle",) or trigger in ("level", "deadline"):
            now = time.time()
            in_win = self.in_asleep_window(now)
            recent_talk = self._recent_talk(now)
            deferred_too_long = self._deferred_too_long(now)

            if not in_win or recent_talk:
                if not deferred_too_long:
                    detail = self._defer_sleep_reason(
                        now, in_win=in_win, recent_talk=recent_talk)
                    self.audit.append(
                        kind="sleep.deferred", target=",".join(
                            cfg.lifecycle.sleep_targets or []),
                        detail=detail, actor=actor, trigger=trigger,
                        suppressed=True,
                        reason="recent_talk" if recent_talk else "outside_window")
                    await self.notifier.push(
                        f"[作息] {detail}", severity=Severity.INFO,
                        target="lifecycle", status="deferred")
                    return False, detail
                # 顺延超限：放行，但把这件事说清楚，别让它悄悄发生。
                logger.warning(
                    f"[guardian] 睡眠顺延已超过 {self._max_defer_h():.0f} 小时，"
                    f"无视作息窗口执行睡眠（{reason}）")
                self.audit.append(
                    kind="sleep.defer_override",
                    target=",".join(cfg.lifecycle.sleep_targets or []),
                    detail=f"顺延超过上限，无视窗口执行：{reason}",
                    actor=actor, trigger=trigger)

        allow = cfg.action_allowlist
        raw_targets = list(cfg.lifecycle.sleep_targets or [])
        targets: list[str] = []
        skipped: list[str] = []
        for t in raw_targets:
            if t not in allow:
                skipped.append(f"{t}(非白名单)")
                continue
            if cfg.is_protected(t) and not cfg.safety.allow_self_restart:
                skipped.append(f"{t}(受保护)")
                continue
            targets.append(t)

        if not targets:
            msg = ("睡眠目标全部不可用"
                   f"（已跳过：{', '.join(skipped) or '无'}）。"
                   "若需要让 AstrBot 本体参与睡眠，请设置 "
                   "safety.allow_self_restart=true 并部署看门狗；"
                   "或把 lifecycle.sleep_targets 改为非受保护容器。")
            self.audit.append(kind="sleep.denied", target=",".join(raw_targets),
                              detail=msg, actor=actor, trigger=trigger,
                              suppressed=True, reason="no_usable_target")
            return False, msg

        spec = ActionSpec(
            kind="sleep", target=targets[0],
            params={"targets": targets, "duration_min": duration_min,
                    "reason": reason, "skipped": skipped},
            actor=actor, trigger=trigger, reason=reason)

        decision = self.safety.authorize(spec)
        if not decision.allowed:
            self.audit.append(kind="sleep.denied", target=",".join(targets),
                              detail=decision.reason, actor=actor, trigger=trigger,
                              suppressed=True, reason=decision.code)
            return False, f"睡眠被拒绝：{decision.reason}"
        spec.dry_run = decision.dry_run

        action_cls = get_action("sleep")
        action = action_cls(self.config, self.deps)
        result = await action.execute(spec)
        if not spec.params.get("_watchdog_committed"):
            degraded = self.safety.commit(spec, result.ok)
            await self._note_degradation(degraded, trigger)
        self.state.set("lifecycle.state", self.lifecycle_state.to_dict())
        self.audit.append(kind="sleep.execute", target=",".join(targets),
                          detail=result.detail, actor=actor, trigger=trigger,
                          ok=result.ok, elapsed_ms=result.elapsed_ms)
        detail = result.detail
        if skipped:
            detail += f"（已跳过：{', '.join(skipped)}）"
        return result.ok, detail

    # ================================================================== #
    # 记录 / 展示
    # ================================================================== #
    def _record_incident(self, inc: Incident) -> None:
        self._incidents.append(inc)
        if len(self._incidents) > MAX_INCIDENTS_KEPT:
            self._incidents = self._incidents[-MAX_INCIDENTS_KEPT:]
        self.audit.append(incident=inc)

    # ------------------------------------------------------------------ #
    # 主动睡眠窗口（B15 生命周期睡眠 × 巡检互斥）
    # ------------------------------------------------------------------ #
    #: 睡眠窗口在 `wake_at` 之后自动失效的额外宽限（秒）：覆盖停止/启动/复核开销。
    SLEEP_WINDOW_SLACK_SEC = 120.0

    def mark_sleeping(self, targets, duration_min: int) -> None:
        """把目标登记进"主动睡眠窗口"，让巡检不再把它当成故障。

        2026-10-16 实机 C16-K3 发现的真问题：内核自己的巡检（`check_interval`）
        会把**故意睡下**的容器读成 `exited`/CRIT，然后 `start_container` 把睡眠
        打断——实测 60s 的睡眠在 ~24s 处被巡检唤醒；更糟的是，睡眠末尾的
        `StartedAt` 复核是被**巡检的重启**喂过的，等于假阳性通过，直接架空了
        M5/D2-b"内核亲眼看到自己动作生效"的核心主张。
        """
        window = dict(self.state.get("sleep.window") or {})
        wake = (time.time() + max(0, int(duration_min)) * 60
                + self.SLEEP_WINDOW_SLACK_SEC)
        for target in targets:
            window[str(target)] = wake
        self.state.set("sleep.window", window)

    def clear_sleeping(self, targets) -> None:
        window = dict(self.state.get("sleep.window") or {})
        for target in targets:
            window.pop(str(target), None)
        self.state.set("sleep.window", window or None)

    def sleeping_targets(self, now: float | None = None) -> set[str]:
        """仍在睡眠窗口内的目标集合（`wake_at` 过后自动失效，进程崩溃也安全）。"""
        window = self.state.get("sleep.window")
        if not isinstance(window, dict):
            return set()
        now = time.time() if now is None else now
        out: set[str] = set()
        for target, wake in window.items():
            try:
                if float(wake) > now:
                    out.add(str(target))
            except (TypeError, ValueError):
                continue
        return out

    def recent_incidents(self, limit: int = 10) -> list[Incident]:
        return list(reversed(self._incidents[-limit:]))

    def _persist_tz_offset(self) -> None:
        """把本地时区偏移落盘到 `env.tz_offset_h`。

        **为什么必须落盘**：`llm_injection()` 是每轮对话都走的最热路径，不能在那里
        调探针（它有 5 分钟缓存，但仍然要读 `/proc`、`platform`、`zoneinfo`）。
        而作息窗口判定（`core/timewindow.py`）和"现在几点"的描述（`core/timefmt.py`）
        都需要这个值，且每轮都要。

        探针没跑过时（例如只有 QQ 局部巡检）保留上一次的值，不覆盖成 0——
        `0` 是"UTC"这个**有意义**的合法值，不能拿它兼任"未知"。
        """
        res = self._last_results.get("env_platform")
        metrics = getattr(res, "metrics", None) if res is not None else None
        if not isinstance(metrics, dict):
            # 正式键是 `<probe>:<target>`，探针的 target 恒为 "env"；
            # 但"按 probe 名直接取"在别处（如 `_lifecycle_tick` 对
            # `uptime:system`）也是惯例，两种都认，避免键名一变就静默失效。
            for key, candidate in self._last_results.items():
                if not key.startswith("env_platform"):
                    continue
                candidate_metrics = getattr(candidate, "metrics", None)
                if isinstance(candidate_metrics, dict) and candidate_metrics.get("tz_offset_h") is not None:
                    metrics = candidate_metrics
                    break
        if not isinstance(metrics, dict):
            return
        value = metrics.get("tz_offset_h")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        try:
            offset = float(value)
        except (TypeError, ValueError):
            return
        if not -14.0 <= offset <= 14.0:
            return
        if self.state.get("env.tz_offset_h") != offset:
            self.state.set("env.tz_offset_h", offset)

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.state.get("mode") or self.config.mode,
            "profile": self.state.get("profile") or self.config.profile,
            "last_run_ts": self._last_run_ts,
            "last_trigger": self._last_trigger,
            "results": [r.to_dict() for r in self._last_results.values()],
            "lifecycle": self.lifecycle_state.to_dict(),
            "tripped": self.safety.tripped,
            "incident_count": len(self._incidents),
        }

    def env_info(self) -> dict[str, Any]:
        """当前巡检采集到的运行环境指纹（`env_platform` 探针的 metrics）。

        没有就跑一次采集：状态快照/诊断报告不应该因为"还没巡检过"而缺一块
        —— 这正是前端不再需要用户手填 `os_hint` 的前提。
        """
        res = self._last_results.get("env_platform")
        if res is not None and res.metrics:
            return dict(res.metrics)
        try:
            from ..probes.env_platform import collect_env

            return collect_env()
        except Exception:  # noqa: BLE001 —— 环境信息缺失不能拖垮状态输出
            logger.debug("[guardian] 环境指纹采集失败", exc_info=True)
            return {}

    def status_text(self, scheduler=None) -> str:
        cfg = self.config
        lines: list[str] = []
        lines.append("== 守护系统健康快照 ==")
        lines.append(
            f"模式: {self.state.get('mode') or cfg.mode} | "
            f"档位: {self.state.get('profile') or cfg.profile}")
        lines.append(
            f"生命周期: {self.lifecycle_policy.describe(self.lifecycle_state)}")
        if self.safety.tripped:
            lines.append("!! 熔断已触发：当前仅告警，不执行动作")

        if not self._last_results:
            lines.append("(尚未执行过巡检，可发送 /guardian check)")
        else:
            lines.append(f"最近巡检: {fmt_ts(self._last_run_ts)}（{self._last_trigger}）")
            for key in sorted(self._last_results):
                res = self._last_results[key]
                lines.append(
                    f"  {status_icon(res.status)} {res.target:<16} {res.message}")
            overall = worst_status([r.status for r in self._last_results.values()])
            lines.append(f"总体: {overall.value.upper()}")

        mem_lines = []
        for res in self._last_results.values():
            if res.probe == "container_memory" and "mem_mb" in res.metrics:
                mem_lines.append(
                    f"  {res.target}: {res.metrics['mem_mb']:.0f}MB "
                    f"(增长 {res.metrics.get('growth_mb_per_hour', 0):.1f}MB/h)")
        if mem_lines:
            lines.append("内存:")
            lines.extend(mem_lines)

        if scheduler is not None:
            lines.append("下次任务:")
            lines.append(scheduler.next_runs_text())

        recent = self.recent_incidents(3)
        if recent:
            lines.append("最近事件:")
            for inc in recent:
                lines.append(f"  {fmt_ts(inc.ts)} {inc.one_line()}")
        return "\n".join(lines)

    def lifecycle_text(self) -> str:
        cooldown_left = max(
            0,
            int(self.config.lifecycle.inject_cooldown_sec
                - (time.time() - self.lifecycle_state.last_inject_at)),
        )
        last = self.lifecycle_state.last_sleep_at
        since = human_duration(time.time() - last) if last else "-"
        return (
            self.lifecycle_policy.describe(self.lifecycle_state)
            + "\n"
            + f"下次注入冷却剩余: {cooldown_left}s\n"
            + f"最近睡眠: {fmt_ts(last)}（距今 {since}）"
        )
