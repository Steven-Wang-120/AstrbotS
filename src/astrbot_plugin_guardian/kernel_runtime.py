"""Guardian 守护系统 —— 内核运行时（域 A：决策 + 观察 + 记账）。

对应文档：B16（独立内核服务架构）、B16 §5.3（装配层如何拆）

**本模块不得 `import astrbot`**（由 `tests/test_kernel_decoupling.py` 静态守卫）。
它是"大脑"，必须在 AstrBot 进程之外也能构造并跑完一轮 patrol——否则一旦它需要
重启 `astrbot` 本体，就会把自己一起杀死（B16 §1 自杀悖论 S1）。

内核需要的两件"外部能力"一律**注入**，不由内核自己去摸 AstrBot：

- `sender(umo, text)`：把通知文本投递出去（域 B 负责包装成 AstrBot 消息链）；
- `action_caller(action)`：向已运行的反向 WebSocket 发只读 OneBot 查询（域 B 持有
  真实 `Context`）。

AstrBot 适配层见 `adapter_runtime.py`；`runtime.py` 是兼容壳。
"""

from __future__ import annotations

import os
import time
from typing import Any, Awaitable, Callable

# 触发探针/动作注册（import 副作用）
from . import actions as _actions  # noqa: F401
from . import probes as _probes  # noqa: F401
from .core.audit import AuditLog
from .core.bus import EventBus
from .core.config import GuardianConfig, build_config
from .core.engine import HealthEngine
from .core.lifecycle import build_policy
from .core.log import logger
from .core.models import ActionSpec, Mode, Profile, Severity
from .core.policy import PolicyEngine
from .core.safety import SafetyGuard
from .core.scheduler import Scheduler
from .core.state import StateStore
from .notify.notifier import Notifier
from .utils.dockerclient import DockerClient
from .utils.napcat import NapCatClient
from .utils.ros2_runner import Ros2Runner
from .utils.textutil import fmt_ts

PLUGIN_NAME = "astrbot_plugin_guardian"

#: 内核版本（随 IPC `ping` / `kernel_ready` 事件回传，便于两端版本对齐排查）
KERNEL_VERSION = "0.3.0"

#: 内核默认数据目录（域 A 自己的数据目录，B16 §7）。
#: 适配层可以覆盖（传入 AstrBot 规范目录），独立进程则可用环境变量指定。
DEFAULT_DATA_DIR_ENV = "GUARDIAN_DATA_DIR"

Sender = Callable[[str, str], Awaitable[None]]
ActionCaller = Callable[[str], Awaitable[Any]]


def default_data_dir() -> str:
    """内核自有的数据目录（不依赖 AstrBot）。"""
    explicit = (os.environ.get(DEFAULT_DATA_DIR_ENV) or "").strip()
    if explicit:
        return explicit
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(base), "data")


class GuardianKernel:
    """守护内核（域 A）。无 AstrBot 依赖，外部能力全部注入。"""

    def __init__(self, raw_config: dict | None, *,
                 data_dir: str = "",
                 sender: Sender | None = None,
                 action_caller: ActionCaller | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.version = KERNEL_VERSION
        self.config: GuardianConfig = build_config(raw_config, data_dir=self.data_dir)

        self._sender = sender
        self._action_caller = action_caller

        self.state = StateStore(self.data_dir)
        self.state.load()
        # 配置中的 mode/profile 作为初始值（运行时可覆盖并持久化）
        self.state.set("mode", self.state.get("mode") or self.config.mode)
        self.state.set("profile", self.state.get("profile") or self.config.profile)

        self.audit = AuditLog(self.data_dir)
        self.bus = EventBus()

        self.docker = DockerClient(
            socket_path=self.config.docker.socket_path,
            api_version=self.config.docker.api_version,
            timeout=float(self.config.docker.timeout or 10),
            use_cli_fallback=bool(self.config.docker.use_cli_fallback),
            cli_path=self.config.docker.cli_path,
        )
        self.napcat = NapCatClient(
            base=self.config.qq.onebot_http_base,
            token=self.config.qq.access_token,
            timeout=float(self.config.qq.request_timeout or 8),
            webui_base=self.config.qq.webui_base,
            webui_token=self.config.qq.webui_token or self.config.qq.access_token,
            action_caller=(self._call_onebot
                           if self.config.qq.transport == "reverse_ws" else None),
        )
        self.ros2 = Ros2Runner(self.config, self.docker)

        self.notifier = Notifier(self.config, self.state, self.audit, self._send)
        self.safety = SafetyGuard(self.config, self.state, self.audit)
        self.policy = PolicyEngine(self.config)
        self.lifecycle_policy = build_policy(self.config)

        deps: dict[str, Any] = {
            "config": self.config,
            "state": self.state,
            "audit": self.audit,
            "bus": self.bus,
            "docker": self.docker,
            "napcat": self.napcat,
            "ros2": self.ros2,
        }
        self.deps = deps
        self.engine = HealthEngine(
            self.config, self.state, self.audit, self.bus, self.notifier,
            self.safety, self.policy, self.lifecycle_policy, deps)
        self.scheduler = Scheduler()

        self._started = False
        self._safety_warnings: list[str] = []
        self.bus.subscribe("profile.changed", self._on_profile_changed)

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    async def start(self) -> None:
        if self._started:
            return
        if not self.config.enabled:
            logger.info("[guardian] 插件总开关已关闭，不启动巡检与调度")
            return
        # 连续确认只对当前运行时有效，不能继承旧版本误报/上次会话的计数。
        self.state.set("qq_fail_streak", 0)
        await self.engine.start()
        from .utils.watchdog import reconcile
        await reconcile(self.engine)
        self._started = True
        self._safety_warnings = self.safety.self_check()
        self._register_jobs()
        self.scheduler.start()
        self.audit.append(kind="runtime.start", detail="守护运行时启动", ok=True)
        logger.info(
            f"[guardian] 守护系统已启动 | 模式={self.state.get('mode')} "
            f"档位={self.state.get('profile')} dry_run={self.config.dry_run} "
            f"数据目录={self.data_dir}")
        for w in self._safety_warnings:
            logger.warning(f"[guardian] 启动告警：{w}")
        if self.config.notify.umo_list:
            await self.notifier.push(
                f"[守护] 守护系统已启动（模式 {self.state.get('mode')}，"
                f"档位 {self.state.get('profile')}，dry-run "
                f"{'开' if self.config.dry_run else '关'}）",
                severity=Severity.INFO, force=True)

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        await self.scheduler.shutdown()
        await self.engine.close()
        await self.state.flush(force=True)
        await self.docker.close()
        await self.napcat.close()
        self.bus.clear()
        self.audit.append(kind="runtime.stop", detail="守护运行时停止", ok=True)
        logger.info("[guardian] 守护系统已停止")

    # ================================================================== #
    # 调度
    # ================================================================== #
    def _register_jobs(self) -> None:
        self.scheduler.clear()
        self._schedule_patrol()
        # QQ 登录态独立巡检（默认 20 分钟）
        qq_interval = max(30, int(self.config.qq.check_interval or 1200))
        self.scheduler.add_interval_job(
            "guardian:qq", self._qq_tick, interval=qq_interval, jitter=30)
        # 定时重启
        if self.config.schedule.daily_restart_enabled:
            try:
                self.scheduler.add_cron_job(
                    "guardian:daily_restart", self._daily_restart,
                    self.config.schedule.daily_restart_cron)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[guardian] daily_restart cron 非法，已跳过: {exc}")
        self._schedule_night_maintenance()

    def _schedule_patrol(self) -> None:
        profile = self.state.get("profile") or self.config.profile
        interval = self.config.profile_interval(profile)
        self.scheduler.cancel("guardian:patrol")
        if interval <= 0:
            logger.info("[guardian] maintenance 档位：周期巡检已暂停")
            return
        self.scheduler.add_interval_job(
            "guardian:patrol", self._patrol, interval=interval, jitter=10)

    def _schedule_night_maintenance(self) -> None:
        profile = self.state.get("profile") or self.config.profile
        self.scheduler.cancel("guardian:night_maintenance")
        if profile != "night":
            return
        cron = self.config.night_profile.maintenance_restart_cron
        try:
            self.scheduler.add_cron_job("guardian:night_maintenance",
                                        self._night_maintenance, cron)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] night maintenance cron 非法: {exc}")

    async def _on_profile_changed(self, topic: str, payload: dict) -> None:
        self._schedule_patrol()
        self._schedule_night_maintenance()

    async def _patrol(self) -> None:
        # 更新 AstrBot 侧活跃度（供 QQ 证据链 3 使用）
        ts = self.state.get("last_event_ts")
        if ts:
            self.state.set("last_event_age", time.time() - float(ts))
        await self.engine.run_once(trigger="interval", scope="all")

    async def _qq_tick(self) -> None:
        if self.state.get("profile") == "maintenance":
            return
        await self.engine.run_qq_check()

    async def _daily_restart(self) -> None:
        targets = list(self.config.schedule.daily_restart_targets or [])
        if not targets:
            logger.info("[guardian] 定时重启已触发，但未配置目标，跳过")
            return
        for name in targets:
            ok, detail = await self.request_restart(name, actor="system",
                                                    trigger="cron",
                                                    reason="定时维护重启")
            logger.info(f"[guardian] 定时重启 {name}: ok={ok} {detail}")

    async def _night_maintenance(self) -> None:
        await self.request_sleep(
            reason="夜间档位维护重启（night_profile.maintenance_restart_cron）",
            duration_min=0, actor="system", trigger="cron", bypass_confirm=True)

    # ================================================================== #
    # 对外能力（命令 / LLM 工具共用）
    # ================================================================== #
    async def run_check(self, scope: str = "all") -> dict[str, Any]:
        return await self.engine.run_once(trigger="command", scope=scope)

    async def request_restart(self, container: str, *, actor: str = "command",
                              trigger: str = "command",
                              reason: str = "") -> tuple[bool, str]:
        spec = ActionSpec(kind="restart_container", target=container,
                          actor=actor, trigger=trigger,
                          reason=reason or f"手动重启 {container}")
        return await self._dispatch(spec)

    async def request_start(self, container: str, *, actor: str = "command",
                            trigger: str = "command") -> tuple[bool, str]:
        spec = ActionSpec(kind="start_container", target=container,
                          actor=actor, trigger=trigger)
        return await self._dispatch(spec)

    async def request_sleep(self, reason: str, duration_min: int = 0, *,
                            actor: str = "llm", trigger: str = "llm",
                            bypass_confirm: bool = False) -> tuple[bool, str]:
        return await self.engine.request_sleep(
            reason, duration_min, actor=actor, trigger=trigger,
            bypass_confirm=bypass_confirm)

    async def _dispatch(self, spec: ActionSpec) -> tuple[bool, str]:
        decision = self.safety.authorize(spec)
        if not decision.allowed:
            self.audit.append(kind="action.denied", target=spec.target,
                              detail=decision.reason, actor=spec.actor,
                              trigger=spec.trigger, suppressed=True,
                              reason=decision.code)
            return False, decision.reason
        spec.dry_run = decision.dry_run
        from .actions.base import get_action
        cls = get_action(spec.kind)
        if cls is None:
            return False, f"未注册的动作类型 {spec.kind}"
        action = cls(self.config, self.deps)
        result = await action.execute(spec)
        if not spec.params.get("_watchdog_committed"):
            self.safety.commit(spec, result.ok)
        self.audit.append(kind="action.execute", target=spec.target,
                          detail=result.detail, actor=spec.actor,
                          trigger=spec.trigger, ok=result.ok,
                          elapsed_ms=result.elapsed_ms, params=spec.params)
        return result.ok, result.detail

    async def set_mode(self, mode: str) -> tuple[bool, str]:
        if mode not in {m.value for m in Mode}:
            return False, f"非法模式 {mode}，可选 server / ros2"
        await self.engine.apply_mode(mode, reason="手动切换")
        return True, f"模式已切换为 {mode}"

    async def set_profile(self, profile: str) -> tuple[bool, str]:
        if profile not in {p.value for p in Profile}:
            return False, f"非法档位 {profile}"
        await self.engine.apply_profile(profile, reason="手动切换")
        return True, f"档位已切换为 {profile}"

    # ================================================================== #
    # 域 B 适配所需的能力（B16 §10 契约补充）
    # ================================================================== #
    def set_sender(self, sender: Sender | None) -> None:
        """注入/替换通知发送器。

        独立内核进程启动时 `sender=None`，随后由 `KernelIpcServer` 把通知改道成
        IPC 事件广播。`Notifier` 持有的是 `self._send` 这个**延迟绑定**的包装，
        所以替换后立刻生效，无需重建 Notifier。
        """
        self._sender = sender

    def runtime_flags(self) -> dict[str, Any]:
        """适配层做 L-D 二次确认所需的**权威**开关。

        必须由内核回答：适配层若拿自己那份配置副本判断 `dry_run`，两份配置一旦
        漂移，就会出现"适配层以为在 dry-run、内核其实已关"的无确认真睡眠。
        """
        return {
            "enabled": bool(self.config.enabled),
            "dry_run": bool(self.config.dry_run),
            "lifecycle_enabled": bool(self.config.lifecycle.enabled),
            "require_admin": bool(self.config.llm_tools.require_admin),
            "sleep_targets": list(self.config.lifecycle.sleep_targets or []),
        }

    def sleep_armed(self) -> bool:
        return bool(self.engine.lifecycle_state.sleep_armed)

    def level_prompt(self) -> str:
        """当前等级注入提示词（`on_llm_request` 注入源）。"""
        return str(self.state.get("lifecycle.inject_prompt") or "")

    def status_context(self) -> str:
        """`<guardian_status>` 状态块（原 `tools/llm_tools.build_status_context`）。

        搬到内核是为了让适配层在 `kernel.mode=ipc` 下不必去读 `state`/`engine`
        ——那会直接违反 I2（适配层无状态、不读内核业务状态）。
        """
        st = self.engine.lifecycle_state
        return (
            "<guardian_status>\n"
            f"守护模式: {self.state.get('mode')}\n"
            f"运行档位: {self.state.get('profile')}\n"
            f"系统已连续运行: {st.uptime_hours:.1f} 小时\n"
            f"生命周期等级: {st.level_id} {st.level_name}\n"
            "（这是系统内部状态，供你判断是否需要安排休息，不必逐字复述。）\n"
            "</guardian_status>"
        )

    def llm_injection(self) -> dict[str, Any]:
        """一次调用返回 LLM 注入所需的全部素材。

        注入发生在**每次** LLM 请求上，是最热路径；拆成多次 IPC 往返会让每次
        对话都多付几次往返延迟。所以这里合成一次调用。
        """
        blocks: list[str] = []
        level = ""
        prompt = self.level_prompt()
        if self.config.lifecycle.enabled and prompt:
            level = str(self.state.get("lifecycle.inject_level") or "")
            blocks.append(
                f'<lifecycle_guard level="{level}">\n{prompt}\n'
                "（这是系统内部状态提示，请自然融入你的行为，不要逐字复述。）\n"
                "</lifecycle_guard>")
        if self.config.llm_tools.inject_status_context:
            blocks.append(self.status_context())
        return {
            "text": "\n".join(blocks),
            "mode": str(self.config.lifecycle.inject_mode or "extra_user_content"),
            "level": level,
        }

    def lifecycle_status_text(self) -> str:
        """生命周期状态文本（原 `tools/llm_tools.lifecycle_status`）。"""
        st = self.engine.lifecycle_state
        lc = self.config.lifecycle
        lines = self.engine.lifecycle_text().splitlines()
        return (
            "生命周期状态：\n"
            f"- {self.lifecycle_policy.describe(st)}\n"
            f"- 硬上限: {float(lc.max_uptime_hours):.0f}h"
            f"（{float(lc.max_uptime_hours) / 24:.0f} 天），"
            f"到达强制睡眠: {'是' if lc.force_sleep_on_deadline else '否'}\n"
            f"- 当前注入等级: {self.state.get('lifecycle.inject_level') or '(无)'}\n"
            f"- 睡眠目标: {', '.join(lc.sleep_targets) or '(未配置)'}\n"
            f"- 最近睡眠: {lines[-1] if lines else '(无记录)'}"
        )

    def audit_append(self, *, kind: str, target: str = "", detail: str = "",
                     ok: bool = True, actor: str = "llm",
                     trigger: str = "llm", **extra: Any) -> None:
        """适配层代内核记一条审计（I1：`audit.jsonl` 仍是内核单写者）。"""
        self.audit.append(kind=kind, target=target, detail=detail, ok=ok,
                          actor=actor, trigger=trigger, **extra)

    # ================================================================== #
    # 只读查询
    # ================================================================== #
    def status_text(self) -> str:
        return self.engine.status_text(self.scheduler)

    def lifecycle_text(self) -> str:
        return self.engine.lifecycle_text()

    def health_report(self, scope: str = "all") -> str:
        snap = self.engine.snapshot()
        lines = [
            f"模式: {snap['mode']} | 档位: {snap['profile']} | "
            f"dry-run: {'开' if snap['dry_run'] else '关'}",
        ]
        if scope in ("all", "containers"):
            lines.append("容器:")
            for r in snap["results"]:
                if r["probe"] in ("container_health", "container_memory",
                                  "docker_daemon", "self_health"):
                    lines.append(f"  [{r['status']}] {r['target']}: {r['message']}")
        if scope in ("all", "qq"):
            lines.append("QQ:")
            for r in snap["results"]:
                if r["probe"] == "qq_login":
                    lines.append(f"  [{r['status']}] {r['message']}")
        if scope in ("all", "ros2"):
            lines.append("ROS2:")
            for r in snap["results"]:
                if r["probe"].startswith("ros2"):
                    lines.append(f"  [{r['status']}] {r['message']}")
        lines.append(f"生命周期: {self.lifecycle_policy.describe(self.engine.lifecycle_state)}")
        return "\n".join(lines)

    def incidents_text(self, limit: int = 10) -> str:
        limit = max(1, min(int(limit or 10), 50))
        recent = self.engine.recent_incidents(limit)
        if not recent:
            return "暂无事件记录。"
        lines = [f"最近 {len(recent)} 条事件:"]
        for inc in recent:
            lines.append(f"  {fmt_ts(inc.ts)} {inc.one_line()}")
        return "\n".join(lines)

    def diagnose(self, target: str) -> str:
        target = (target or "all").strip()
        snap = self.engine.snapshot()
        matched = [r for r in snap["results"]
                   if target in ("all", r["target"], r["probe"])]
        if not matched:
            return (f"未找到目标 {target} 的巡检数据。可先执行一次巡检"
                    "（/guardian check），或检查目标名是否正确。")
        lines = [f"诊断 {target}:"]
        for r in matched:
            lines.append(f"- 探针 {r['probe']} / 目标 {r['target']}: "
                         f"{r['status']} — {r['message']}")
            for k, v in (r.get("metrics") or {}).items():
                lines.append(f"    {k}: {v}")
            if r.get("error"):
                lines.append(f"    error: {r['error']}")
        return "\n".join(lines)

    def allowlist(self) -> list[str]:
        return sorted(self.config.action_allowlist)

    def safety_warnings(self) -> list[str]:
        return list(self._safety_warnings)

    def jobs_text(self) -> str:
        return self.scheduler.next_runs_text()

    # ================================================================== #
    # 内部
    # ================================================================== #
    async def _call_onebot(self, action: str) -> dict:
        """只读 OneBot 查询；真实实现由域 B 注入（它才持有 AstrBot Context）。"""
        if self._action_caller is None:
            from .utils.napcat import NapCatUnavailable
            raise NapCatUnavailable(
                "未注入 OneBot 调用器（内核独立运行时无反向 WS 直连能力）")
        if action not in {"get_status", "get_login_info"}:
            from .utils.napcat import NapCatUnavailable
            raise NapCatUnavailable("Guardian 仅允许只读登录态查询")
        return await self._action_caller(action)

    async def _send(self, umo: str, text: str) -> None:
        if self._sender is None:
            raise RuntimeError("未注入通知发送器（域 B 负责投递）")
        await self._sender(umo, text)

    def note_event(self) -> None:
        """由适配层在收到任意消息时调用（供 QQ 活跃度证据）。"""
        self.state.set("last_event_ts", time.time())
        self.state.set("last_event_age", 0.0)


# 兼容别名：旧代码/测试按 GuardianRuntime 引用内核本体时仍可用。
KernelRuntime = GuardianKernel
