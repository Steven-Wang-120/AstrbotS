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
from .audit import AuditLog
from .bus import EventBus
from .config import GuardianConfig
from .lifecycle import LifecyclePolicy, LifecycleState
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
