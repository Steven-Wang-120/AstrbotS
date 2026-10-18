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

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

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
from .core.timefmt import clamp_tz_offset, describe, describe_uptime
from .notify.notifier import Notifier
from .utils.dockerclient import DockerClient
from .utils.migration import migrate_legacy_data, resolve_legacy_data_dir
from .utils.napcat import NapCatClient, NapCatError
from .utils.ros2_runner import Ros2Runner
from .utils.textutil import fmt_ts

PLUGIN_NAME = "astrbot_plugin_guardian"

#: 内核版本（随 IPC `ping` / `kernel_ready` 事件回传，便于两端版本对齐排查）
KERNEL_VERSION = "0.5.0"

#: 内核默认数据目录（域 A 自己的数据目录，B16 §7）。
#: 适配层可以覆盖（传入 AstrBot 规范目录），独立进程则可用环境变量指定。
DEFAULT_DATA_DIR_ENV = "GUARDIAN_DATA_DIR"

Sender = Callable[[str, str], Awaitable[None]]
ActionCaller = Callable[[str], Awaitable[Any]]
#: 把"事实"说成人话。由适配层注入（内部调 LLM），内核自己绝不 import astrbot。
#: 入参是 `engine.sleepy_facts()` / `resume_facts()` 的返回字典。
Composer = Callable[[dict[str, Any]], Awaitable[str]]


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
        #: 主动消息的文本生成器（见 `set_announce_composer`）。`None` = 无生成
        #: 能力，睡前报备退化为"只报事实"的兜底文本。
        self._announce_composer: Composer | None = None

        # 数据目录迁移（B16 §7 / §9 M4）：内核独立后，业务状态要从 AstrBot 的插件
        # 数据目录搬到内核可见的共享卷。幂等、只复制不删除、留 `.migrated` 标记。
        # 必须**先于** StateStore/AuditLog 构造，否则会先读到空状态。
        self.migration = migrate_legacy_data(
            self.data_dir, resolve_legacy_data_dir(self.config, self.data_dir))

        self.state = StateStore(self.data_dir)
        self.state.load()
        # 配置中的 mode/profile 作为初始值（运行时可覆盖并持久化）
        self.state.set("mode", self.state.get("mode") or self.config.mode)
        self.state.set("profile", self.state.get("profile") or self.config.profile)

        self.audit = AuditLog(self.data_dir)
        self.bus = EventBus()

        if self.migration.get("migrated"):
            detail = (f"数据目录迁移：{self.migration['reason']}，"
                      f"已复制 {', '.join(self.migration['copied'])}")
            logger.info(f"[guardian] {detail}（源目录保留为只读副本，可回滚）")
            self.audit.append(kind="data.migrated", detail=detail, ok=True)
        elif str(self.migration.get("reason", "")).startswith("io-error"):
            logger.warning(
                f"[guardian] 数据目录迁移失败（不阻断启动）：{self.migration['reason']}")
            self.audit.append(kind="data.migrate_failed",
                              detail=str(self.migration.get("reason")), ok=False)

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
            # B17 §6.3：`webui_quick_login` 需要账号才能打 `SetQuickLogin`
            account=self.config.qq.account,
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
            f"档位={self.state.get('profile')} "
            f"kernel.mode={self.config.kernel.mode} "
            f"direct_actions={self.direct_actions} "
            f"数据目录={self.data_dir}")
        for w in self._safety_warnings:
            logger.warning(f"[guardian] 启动告警：{w}")
        # 自动安装宿主看门狗（必须在自检**之前**：装成功了自检就该看到健康账本，
        # 而不是先报一次"没装"再装好）。
        install_note = await self._maybe_auto_install_watchdog()
        # 宿主看门狗自检（M0）：请求投递链断了的话，面板会显示"已提交"但其实
        # 容器没睡。这一条必须显式告警，否则失败完全是静默的。
        watchdog_warning = self._check_watchdog()
        if watchdog_warning:
            logger.warning(f"[guardian] 启动告警：{watchdog_warning}")
            self._safety_warnings.append(watchdog_warning)
            self.audit.append(kind="watchdog.unhealthy", detail=watchdog_warning, ok=False)
        else:
            self.audit.append(kind="watchdog.healthy", detail="宿主看门狗就绪", ok=True)
        if self.config.notify.umo_list:
            await self.notifier.push(
                f"[守护] 守护系统已启动（模式 {self.state.get('mode')}，"
                f"档位 {self.state.get('profile')}）",
                severity=Severity.INFO, force=True)
        if watchdog_warning and self.config.notify.umo_list:
            await self.notifier.push(
                f"[守护] {watchdog_warning}", severity=Severity.WARN, force=True)
        if install_note and self.config.notify.umo_list:
            # 只在"动手装过"时推：已装跳过与关闭开关都不刷屏。
            await self.notifier.push(
                f"[守护] {install_note}", severity=Severity.INFO, force=True)

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

    async def request_qq_login(self, *, actor: str = "command",
                               trigger: str = "command",
                               reason: str = "") -> tuple[bool, str]:
        """触发一次 QQ 登录恢复阶梯（B17 §8.1）。

        `target="qq"` 是**逻辑目标**：`safety._physical_targets` 会按
        `qq.relogin_method` 把它映射到 napcat 容器，冷却/归属判定沿用 B12。
        """
        spec = ActionSpec(kind="qq_relogin", target="qq", actor=actor,
                          trigger=trigger,
                          reason=reason or "手动触发 QQ 登录恢复")
        return await self._dispatch(spec)

    async def qq_login_status(self) -> dict[str, Any]:
        """登录状态页的数据源（只读，B17 §10.3）。

        刻意做成内核方法而不是"适配层自己读 state"：I1 规定状态只由内核写，
        `kernel.mode=ipc` 下适配层根本没有那份 state。这里只暴露判定结论，
        **不**暴露任何凭据字段。
        """
        status: dict[str, Any] = {
            "isLogin": None, "isOffline": None, "qrcodeurl": "",
            "loginError": "", "available": False,
            "last_failure": self.state.get("qq.last_failure") or {},
            "auth_hold_until": float(self.state.get("qq.auth_hold_until") or 0),
            "last_success_at": float(self.state.get("qq.last_success_at") or 0),
            "webui_public_base": str(self.config.qq.webui_public_base or ""),
            "webui_base": str(self.config.qq.webui_base or ""),
            "account": str(self.config.qq.account or ""),
            "relogin_method": str(self.config.qq.relogin_method or ""),
            "has_credential": bool(self.config.qq.account and self.config.qq.password),
            "now": time.time(),
        }
        try:
            status.update(await self.napcat.check_login_status())
            status["available"] = True
        except NapCatError as exc:
            status["loginError"] = str(exc)
        return status

    async def request_sleep(self, reason: str, duration_min: int = 0, *,
                            actor: str = "llm", trigger: str = "llm",
                            bypass_confirm: bool = False) -> tuple[bool, str]:
        # ---- 睡前报备 -------------------------------------------------- #
        # 只在**自主来源**上做（生命周期等级推进 / 硬上限）：只有这两种情况
        # 用户事先不知道、且会突然发现 bot 不见了。管理员手动 sleep 有命令
        # 回执、cron 维护重启写在配置里，都不需要再报备一句。
        #
        # 为什么必须在**内核这一层**做：真正决定"要睡了"的是这一次 tick，
        # 适配层的 120 秒轮询可能刚好错过窗口。放在这里才能保证"说了就睡"。
        #
        # 整体吞异常：报备是礼貌，不是前置条件。让一次 LLM 超时挡住 30 天
        # 硬上限的强制睡眠，比"没说一声"严重得多。
        if actor == "lifecycle" and trigger in ("level", "deadline"):
            try:
                await self._announce_before_sleep(reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[guardian] 睡前报备失败（不影响睡眠）：{exc}")

        return await self.engine.request_sleep(
            reason, duration_min, actor=actor, trigger=trigger,
            bypass_confirm=bypass_confirm)

    def set_announce_composer(self, composer: Composer | None) -> None:
        """注入"把事实说成人话"的能力（由适配层提供，内部调 LLM）。

        为什么用注入而不是让内核自己调模型：

        - **I6**：`core/` 与内核进程不得 `import astrbot`，provider 凭据只在
          适配层手里；
        - **可用性**：内核若直连模型，一次网络抖动会把巡检循环和睡眠路径
          一起卡住；注入的函数由适配层控制超时与降级；
        - **回滚**：`kernel.mode=ipc` 时这里天然是 `None`，行为退化为
          "不主动说话"，而不是崩掉。

        与 `set_sender` 同样是延迟绑定：`inproc` 模式下适配层构造完内核再注入。
        """
        self._announce_composer = composer

    async def _announce_before_sleep(self, reason: str) -> None:
        """在真睡眠之前让 bot 自己开口说一声。"""
        facts = self.sleepy_facts()
        if not facts:
            return
        key = str(facts.get("event_key") or "")
        if key and key == self.lifecycle_state.announced_key:
            return
        umo = self._announce_umo()
        if not umo:
            logger.info("[guardian] 睡前报备跳过：没有可用的会话地址")
            return

        # 先占坑再生成：这一段在**睡眠之前**执行，重复说一遍比"生成成功但
        # 并发重入又发一次"更糟；而且这里是单次机会，不存在"失败了下一轮
        # 再试"——下一轮 bot 已经睡下了。
        if key and not self.engine.claim_announce(key):
            return

        facts["umo"] = umo
        text = ""
        composer = self._announce_composer
        if composer is not None:
            text = str(await composer(facts) or "")
        if not text:
            # 没有生成能力（ipc 模式）或生成失败：退回到"只有事实"的一句
            # 兜底。这不是话术设计，是**告知义务的下限**——总比一声不响消失好。
            text = self._facts_only_text(facts)

        try:
            await self._send(umo, text)
            self.audit.append(kind="announce.sent", target=str(facts.get("kind") or ""),
                              detail=f"{key} | {text[:120]}", ok=True,
                              actor="lifecycle", trigger="sleep")
            logger.info(f"[guardian] 睡前报备已发送（{umo}）：{text}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] 睡前报备发送失败：{exc}")

    def _facts_only_text(self, facts: dict[str, Any]) -> str:
        """无 LLM 时的最小事实兜底。

        刻意写得像"电报"而不是像"人话"：它只在模型不可用时出现，读者
        应该一眼看出这是降级输出，而不是以为 bot 突然换了说话方式。
        """
        parts = [f"{facts.get('now_text') or ''}"]
        if facts.get("kind") == "resume":
            parts.append(f"刚睡了 {facts.get('slept_minutes', 0)} 分钟，回来了。")
        else:
            parts.append(f"连续在线 {facts.get('uptime_hours', 0):.0f} 小时，"
                         f"{facts.get('level_id', '')} {facts.get('level_name', '')}，"
                         f"现在要睡一下。")
            dur = int(facts.get("duration_min") or 0)
            if dur:
                parts.append(f"约 {dur} 分钟。")
        return "".join(p for p in parts if p).strip()

    def _announce_umo(self) -> str:
        """报备收件人：管理员 > 最近说话的人 > notify.umo_list[0]。"""
        for key in ("announce.admin_umo", "announce.last_umo"):
            value = self.state.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for t in (self.config.notify.umo_list or []):
            if isinstance(t, str) and t.strip():
                return t.strip()
        return ""


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

        必须由内核回答：适配层若拿自己那份配置副本判断，两份配置一旦漂移，
        就会出现"适配层以为要确认、内核其实已放行"的无确认破坏性动作。
        """
        return {
            "enabled": bool(self.config.enabled),
            "lifecycle_enabled": bool(self.config.lifecycle.enabled),
            "require_admin": bool(self.config.llm_tools.require_admin),
            "sleep_targets": list(self.config.lifecycle.sleep_targets or []),
            "direct_actions": bool(self.direct_actions),
        }

    @property
    def direct_actions(self) -> bool:
        """M5/D2-b：本内核是否直连 docker.sock 执行动作（生效值，非配置原值）。"""
        from .actions.base import direct_actions_enabled
        return direct_actions_enabled(self.config)

    def sleep_armed(self) -> bool:
        return bool(self.engine.lifecycle_state.sleep_armed)

    # ------------------------------------------------------------------ #
    # 主动消息（转交 engine；内核对外只暴露这一层，适配层不碰 engine）
    # ------------------------------------------------------------------ #
    def announce_config(self) -> dict[str, Any]:
        return self.engine.announce_config()

    def sleepy_facts(self, now: float | None = None) -> dict[str, Any] | None:
        return self.engine.sleepy_facts(now=now)

    def resume_facts(self, now: float | None = None) -> dict[str, Any] | None:
        return self.engine.resume_facts(now=now)

    def preview_facts(self, kind: str = "sleep",
                      now: float | None = None) -> dict[str, Any] | None:
        """预览专用：假设等级已到，返回能渲染的事实（只读、不发送）。"""
        return self.engine.preview_facts(kind=kind, now=now)

    def drowsy_report(self, now: float | None = None) -> dict[str, Any] | None:
        """"我困不困"的结构化事实（只读、不带闸门、不调 LLM）。"""
        return self.engine.drowsy_report(now=now)

    def peer_stats(self) -> dict[str, Any]:
        return self.engine.peer_stats()

    def announce_state(self) -> dict[str, Any]:
        return self.engine.announce_state()

    def claim_announce(self, key: str, now: float | None = None) -> bool:
        return self.engine.claim_announce(key, now=now)

    def note_unanswered(self, answered: bool = False) -> int:
        return self.engine.note_unanswered(answered=answered)

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

    def _tz_offset_h(self) -> float:
        """本地时区偏移（小时，东正西负）。

        只读 `state["env.tz_offset_h"]`——由 `env_platform` 探针算好后落盘
        （`engine._persist_tz_offset()`）。**不要**在这里调探针：`llm_injection`
        是每轮对话都走的最热路径，而探针要读 `/proc`、跑 `platform`、查 `zoneinfo`。

        缺失时兜底 `+8.0`：这个项目的部署目标是中国时区（compose 里
        `TZ=${TZ:-Asia/Shanghai}`），而且"稍微偏几小时"远好过"每秒重算一次"。
        """
        value = self.state.get("env.tz_offset_h")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 8.0
        return clamp_tz_offset(float(value))

    def llm_injection(self) -> dict[str, Any]:
        """一次调用返回 LLM 注入所需的全部素材。

        注入发生在**每次** LLM 请求上，是最热路径；拆成多次 IPC 往返会让每次
        对话都多付几次往返延迟。所以这里合成一次调用。

        块顺序固定为 `<guardian_time>` → `<lifecycle_guard>` → `<guardian_status>`：
        先给"现在是什么时候"这个客观锚点，再给"我现在状态如何"，最后才是系统健康。
        顺序反了模型容易把"运行了 8 天"读成"现在很晚"。
        """
        blocks: list[str] = []
        level = ""
        period = ""

        if self.config.lifecycle.enabled:
            tz_offset = self._tz_offset_h()
            text, period = describe(time.time(), tz_offset)
            uptime = self.engine.lifecycle_state.uptime_hours
            lines = ["<guardian_time>", f"现在：{text}"]
            if isinstance(uptime, (int, float)) and uptime > 0:
                lines.append(f"已连续运行：{describe_uptime(float(uptime))}")
            lines.append("</guardian_time>")
            blocks.append("\n".join(lines))

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
            # M3 用它判断"在不在窗口内"；这里只透传，判定在 engine 里做
            "period": period,
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

    def env_info(self) -> dict[str, Any]:
        """运行环境指纹（系统 / 内核 / 架构 / 内存 / 时区）。

        取代前端手填的 `os_hint`：值来自宿主机侧采集，而不是运维的输入。
        """
        return self.engine.env_info()

    def health_report(self, scope: str = "all") -> str:
        snap = self.engine.snapshot()
        lines = [
            f"模式: {snap['mode']} | 档位: {snap['profile']}",
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

    def note_event(self, umo: str = "", is_admin: bool = False,
                   place: str = "") -> None:
        """由适配层在收到任意消息时调用（供 QQ 活跃度证据）。

        **只记"谁在哪个会话发过言"，从不接触消息内容**（P1 / B15 §6）。
        `umo` 是 AstrBot 的会话标识，`is_admin` 由适配层按管理员名单判定
        ——适配层本来就持有那份名单，内核不该再维护一份。

        为什么要记地址：`notify.umo_list` 在很多部署里是空的，主动消息就
        没有收件人（历史行为是退化成一行 `notify.no_target` 日志）。用最近
        一次真实会话当收件人比让用户手填 UMO 可靠得多，也不需要读内容。

        管理员的消息**绕过合并窗口**：见 `adapter_runtime.note_event`。
        """
        self.state.set("last_event_ts", time.time())
        self.state.set("last_event_age", 0.0)
        if isinstance(umo, str) and umo.strip():
            self.state.set("announce.last_umo", umo.strip()[:200])
            if isinstance(place, str) and place.strip():
                self.state.set("announce.last_place", place.strip()[:16])
            if is_admin:
                # 管理员地址单独存一份且不覆盖：报备消息优先发给管理员，
                # 不能被随后进来的普通群友覆盖掉。
                self.state.set("announce.admin_umo", umo.strip()[:200])
                if isinstance(place, str) and place.strip():
                    self.state.set("announce.admin_place", place.strip()[:16])

    # ================================================================== #
    # 宿主看门狗自检 / 自动安装
    # ================================================================== #
    WATCHDOG_STALE_SEC = 300.0        # 账本 5 分钟没动过 = 看门狗没在跑

    def _watchdog_state_path(self) -> Path | None:
        """看门狗账本在插件可见范围内的路径。

        请求/结果文件与账本同目录（见 `deploy/guardian-watchdog.py` 的
        `Config.state`），所以由 `watchdog_request_path` 推出来即可，不需要
        再加一个配置项。
        """
        request = (self.config.safety.watchdog_request_path or "").strip()
        if not request:
            return None
        explicit = (self.config.safety.watchdog_result_path or "").strip()
        base = Path(explicit).parent if explicit else Path(request).parent
        return base / "watchdog_state.json"

    def _host_compose_dir(self) -> str:
        """宿主机上的 compose 目录（安装脚本要用它反推 data 挂载点）。

        来源优先级：
          1. 显式配置 `safety.watchdog_compose_dir`（留空则自动探测）
          2. 本容器的 `com.docker.compose.project.working_dir` 标签 ——
             compose 自己写进去的，比任何猜测都准
          3. 放弃（返回空串），让安装脚本自己向上找
        """
        explicit = str(
            getattr(self.config.safety, "watchdog_compose_dir", "") or "").strip()
        if explicit:
            return explicit
        return str(self.state.get("host.compose_dir") or "").strip()

    async def _probe_host_compose_dir(self) -> str:
        """读本容器的 compose 标签，拿到宿主机上的 project working_dir。

        `com.docker.compose.project.working_dir` 是 compose 自己写进容器的，
        比按挂载路径反推可靠得多（反推容易在 `./data` 这种相对路径上出错）。
        拿不到就返回空串，让安装脚本自己去向上找 compose 文件。
        """
        name = self.config.self_container
        try:
            info = await self.docker.inspect(name)
        except Exception:  # noqa: BLE001 - 探测失败不该影响启动
            return ""
        if not isinstance(info, dict):
            return ""
        labels = ((info.get("Config") or {}).get("Labels") or {})
        if not isinstance(labels, dict):
            return ""
        workdir = str(
            labels.get("com.docker.compose.project.working_dir") or "").strip()
        if workdir:
            # 落库，方便运维在 /guardian status 与日志里核对
            self.state.set("host.compose_dir", workdir)
        return workdir

    def _deploy_script_path(self) -> Path:
        """插件自带的安装脚本（随插件分发）。"""
        return Path(__file__).resolve().parent / "deploy" / "install-watchdog.sh"

    async def _maybe_auto_install_watchdog(self) -> str:
        """启动时尝试自动安装宿主看门狗。返回给用户看的一句话（空=无需提示）。

        **为什么放在容器里做这件事**：看门狗必须跑在宿主机（它要操作
        compose），而插件在容器里。两者之间隔着容器边界。能通行的唯一路径是
        Docker socket —— 这个部署本来就把 socket 挂进来了（插件用它探测
        容器状态），所以"顺手装个看门狗"是既有能力的延伸，不是新增权限。

        能力边界（必须如实告知，不能假装做到）：
          * Linux 宿主：能全自动（临时容器挂 `/` 到 `/host`，跑安装脚本）
          * Windows 宿主：**不能**。Linux 容器里跑不了 `powershell.exe`，
            注册不了计划任务。此时只给出可复制的手动命令。

        本方法**不抛异常**、**不阻断启动**：装不上最多是"下次还得手动"。
        """
        safety = self.config.safety
        if not bool(getattr(safety, "auto_install_watchdog", False)):
            return ""
        if str(getattr(safety, "self_restart_strategy", "") or "") != "watchdog":
            # 没选 watchdog 策略就不该往宿主机装任何东西。
            return ""
        ledger = self._watchdog_state_path()
        if ledger is None:
            return ""
        try:
            from .core.watchdog_install import already_installed, install
        except ImportError:
            return ""
        if already_installed(str(ledger)):
            return ""

        script = self._deploy_script_path()
        if not script.exists():
            msg = (f"未能自动安装宿主看门狗：插件内缺少 {script.name}"
                   f"（deploy/ 未随插件打包？）")
            logger.warning(f"[guardian] {msg}")
            self.audit.append(kind="watchdog.install", detail=msg, ok=False)
            return msg

        compose_dir = await self._probe_host_compose_dir() or self._host_compose_dir()
        # 安装脚本在容器里看不到宿主路径，所以 compose_dir 只用于**日志**与
        # 传给脚本做参考；真正让脚本工作的是临时容器里的 /host 挂载。
        logger.info(f"[guardian] 尝试自动安装宿主看门狗 | compose_dir={compose_dir or '(自动探测)'}")
        res = await asyncio.to_thread(
            install,
            deploy_script=str(script),
            ledger_path=str(ledger),
            compose_dir_host=compose_dir or "/opt/astrbot-stack",
            targets=list(self.config.lifecycle.sleep_targets or []),
        )
        if res.get("skipped"):
            self.audit.append(kind="watchdog.install", detail=res.get("reason", ""),
                              ok=True, suppressed=True)
            return ""
        if res.get("ok"):
            logger.info(f"[guardian] 宿主看门狗自动安装成功：{res.get('reason')}")
            self.audit.append(kind="watchdog.install", detail=res.get("reason", ""),
                              ok=True)
            return f"已在宿主机自动安装看门狗（{res.get('reason')}）"

        # 失败：审计 + 返回可见提示（含可复制的手动命令）
        reason = str(res.get("reason") or "未知原因")
        output = str(res.get("output") or "").strip()
        detail = reason + (f" | 输出：{output[-500:]}" if output else "")
        logger.warning(f"[guardian] 自动安装宿主看门狗失败：{detail}")
        self.audit.append(kind="watchdog.install", detail=detail, ok=False)
        hint = str(res.get("hint") or "").strip()
        return f"未能自动安装宿主看门狗（{reason}）" + (f"\n{hint}" if hint else "")

    def _check_watchdog(self, now: float | None = None) -> str:
        """检查宿主看门狗是否真的在跑；返回告警文本（空串表示健康）。

        **为什么必须自检**：插件把请求文件写出去之后只能"相信"宿主会消费它。
        如果看门狗没装、路径对不上、或者容器名不在白名单里，请求会被静默丢弃，
        面板却显示"已提交睡眠"——这是这个项目里最容易骗过自己的失败模式。
        所以启动时做一次廉价的存在性 + 新鲜度检查。

        判定：
          * 不是 watchdog 策略 → 不检查（返回空串）
          * 账本缺失 / 超过 5 分钟没更新 → 视为未运行，**一律报警**
          * 但 `allow_self_restart=false` 时改措辞：本体本就不参与睡眠，
            缺看门狗不影响睡眠语义，属于"配置说得像有、实际没有"的卫生问题

        早期实现是"`allow_self_restart=false` 就直接返回空串"。那会掩盖
        一个真实且常见的情形：策略填了 `watchdog`、宿主机却没装看门狗。
        本体此刻确实不睡，所以不算故障；但用户一旦把 `allow_self_restart`
        打开，睡眠就会静默失败，而此前没有任何提示。宁可现在多说一句。

        本方法**不抛异常**，也**不阻断启动**：看门狗缺失只影响"真睡眠"，
        不影响巡检与告警。
        """
        safety = self.config.safety
        if str(getattr(safety, "self_restart_strategy", "") or "") != "watchdog":
            return ""

        allow_self = bool(getattr(safety, "allow_self_restart", False))

        path = self._watchdog_state_path()
        if path is None:
            # 没配请求路径时，即使本体不睡也该提一句：策略选了 watchdog
            # 却没有任何投递位置，这个组合本身就不成立。
            return ("safety.self_restart_strategy=watchdog 但未配置 "
                    "watchdog_request_path，本体睡眠请求无处投递")

        now = time.time() if now is None else now
        try:
            mtime = path.stat().st_mtime
        except OSError:
            if allow_self:
                return (f"未检测到宿主看门狗（{path} 不存在），本体睡眠将无法执行；"
                        f"请运行 deploy/install-watchdog.sh 或 install-watchdog.ps1")
            return (f"safety.self_restart_strategy=watchdog，但宿主机上没有看门狗"
                    f"（{path} 不存在）。当前 allow_self_restart=false，本体不参与"
                    f"睡眠，因此暂不影响运行；但若将来开启 allow_self_restart，"
                    f"睡眠会静默失败。可执行 deploy/install-watchdog.ps1 安装")
        age = now - mtime
        if age > self.WATCHDOG_STALE_SEC or age < -self.WATCHDOG_STALE_SEC:
            if allow_self:
                return (f"宿主看门狗疑似已停止（{path} 已 {age / 60:.0f} 分钟未更新），"
                        f"本体睡眠将无法执行")
            return (f"宿主看门狗账本已 {age / 60:.0f} 分钟未更新（{path}），"
                    f"看门狗可能已停止。当前 allow_self_restart=false，本体不参与"
                    f"睡眠，因此暂不影响运行")
        return ""


# 兼容别名：旧代码/测试按 GuardianRuntime 引用内核本体时仍可用。
KernelRuntime = GuardianKernel
