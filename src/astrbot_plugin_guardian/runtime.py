"""Guardian 守护系统 —— 运行时装配层。

把配置、状态、审计、客户端、引擎、调度器装配成一个可直接使用的运行时对象。
`main.py` 只做 AstrBot 适配（命令 / LLM 工具 / 钩子），全部业务逻辑在此。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from astrbot.api import logger

# 触发探针/动作注册（import 副作用）
from . import actions as _actions  # noqa: F401
from . import probes as _probes  # noqa: F401
from .core.audit import AuditLog
from .core.bus import EventBus
from .core.config import GuardianConfig, build_config
from .core.engine import HealthEngine
from .core.lifecycle import build_policy
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

# --------------------------------------------------------------------------- #
# 消息链构造（跨版本兼容）
# --------------------------------------------------------------------------- #
def _resolve_message_chain():
    for path, attr in (
        ("astrbot.api.message_chain", "MessageChain"),
        ("astrbot.api.event", "MessageChain"),
        ("astrbot.api.message", "MessageChain"),
    ):
        try:
            mod = __import__(path, fromlist=[attr])
            return getattr(mod, attr)
        except (ImportError, AttributeError):
            continue
    return None


_MESSAGE_CHAIN = _resolve_message_chain()


def _resolve_plain():
    try:
        from astrbot.api.message_components import Plain
        return Plain
    except ImportError:
        return None


_PLAIN = _resolve_plain()


def build_chain(text: str):
    """构造可发送的消息链；全部失败时退回纯字符串。"""
    if _MESSAGE_CHAIN is not None:
        try:
            return _MESSAGE_CHAIN().message(text)
        except Exception:  # noqa: BLE001
            pass
    if _PLAIN is not None:
        try:
            return [_PLAIN(text)]
        except Exception:  # noqa: BLE001
            pass
    return text


PLUGIN_NAME = "astrbot_plugin_guardian"


def resolve_data_dir() -> str:
    """解析守护系统数据目录（B11：数据必须放 AstrBot 数据目录，不能放插件目录内）。

    注意：`StarTools.get_data_dir()` 不带参数时会回溯调用栈来推断插件名，
    只在可直接读到 main 模块元数据的场景有效；从子模块调用会抛 RuntimeError。
    因此这里显式传插件名，避免静默退化到错误路径。
    """
    # 1) 显式传名，走 AstrBot 规范的 data/plugin_data/<name>
    try:
        from astrbot.api.star import StarTools
        d = StarTools.get_data_dir(PLUGIN_NAME)
        if d:
            return str(d)
    except Exception:  # noqa: BLE001
        pass
    # 2) 退回 data/plugin_data/<name>（等价路径，兼容 StarTools 不可用）
    try:
        from astrbot.api.star import get_astrbot_data_path
        base = get_astrbot_data_path()
        if base:
            p = os.path.join(str(base), "plugin_data", PLUGIN_NAME)
            os.makedirs(p, exist_ok=True)
            return p
    except Exception:  # noqa: BLE001
        pass
    # 3) 最后的兜底：插件目录旁的 data（仅开发态可用）
    base = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(os.path.dirname(base), "data")
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        pass
    return p


class GuardianRuntime:
    """守护系统运行时。"""

    def __init__(self, context, raw_config: dict | None) -> None:
        self.context = context
        self.data_dir = resolve_data_dir()
        self.config: GuardianConfig = build_config(raw_config, data_dir=self.data_dir)

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
            action_caller=self._call_onebot if self.config.qq.transport == "reverse_ws" else None,
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
        """通过真实 Context 获取已运行的 OneBot 反向 WS，绝不另起协议服务。

        多适配器时必须指定 platform_id，不能猜测账号。每个实例可通过
        qq.self_id 显式路由多个反向连接；默认单账号由 aiocqhttp 处理。
        """
        from .utils.napcat import NapCatUnavailable
        if action not in {"get_status", "get_login_info"}:
            raise NapCatUnavailable("Guardian 仅允许只读登录态查询")
        platform_id = str(self.config.qq.platform_id or "").strip()
        if platform_id:
            getter = getattr(self.context, "get_platform_inst", None)
            platform = getter(platform_id) if callable(getter) else None
            platforms = [platform] if platform is not None else []
        else:
            manager = getattr(self.context, "platform_manager", None)
            platforms = list(getattr(manager, "platform_insts", []) or [])
        candidates = [p for p in platforms if p.meta().name == "aiocqhttp"]
        if len(candidates) != 1:
            raise NapCatUnavailable("未找到唯一 OneBot 适配器，请配置 qq.platform_id")
        client = candidates[0].get_client()
        params = {}
        if self.config.qq.self_id:
            params["self_id"] = str(self.config.qq.self_id)
        return await client.call_action(action, **params)

    async def _send(self, umo: str, text: str) -> None:
        await self.context.send_message(umo, build_chain(text))

    def note_event(self) -> None:
        """由 main.py 在收到任意消息时调用（供 QQ 活跃度证据）。"""
        self.state.set("last_event_ts", time.time())
        self.state.set("last_event_age", 0.0)
