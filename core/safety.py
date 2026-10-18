"""Guardian 守护系统 —— 安全护栏。

对应文档：B12

四重约束：白名单 / 冷却 / 每小时熔断 / 参数规范化。
外加：受保护容器、连续失败降级、启动自检。
所有破坏性动作必须经 `authorize()` 才能执行。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .log import logger
from .models import ActionSpec

# 允许被动作作用的 kind
CONTAINER_KINDS = {"restart_container", "start_container", "sleep"}
READONLY_KINDS = {"notify", "set_profile", "set_mode", "diagnose", "report"}

# 目标名不是容器、而是逻辑目标的动作 kind。
# 这类动作不能拿容器名白名单（containers.managed）去卡，
# 否则 Default 配置下会被判 "not_allowlisted" 而永远无法执行——QQ 自动重登正是如此。
#   qq_relogin  -> target 为 "qq"，实际作用对象由 qq.relogin_method 决定
#   ros2_restart -> target 为 ROS2 节点名，实际作用命令由 ros2.node_restart_commands 决定
LOGICAL_TARGET_KINDS = {"qq_relogin", "ros2_restart"}

# 容器名/目标名合法性
TARGET_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
# ROS2 节点名：规范上以 / 开头，允许 / . 与字母数字下划线连字符
ROS2_NODE_RE = re.compile(r"^/[A-Za-z0-9_./-]{1,62}$")
FORBIDDEN_CHARS = set(" \t\n\r;|&`$(){}[]<>'\"\\*?!")

# 恢复命令允许的前缀（ROS2 等）
ALLOWED_COMMAND_PREFIXES = ("systemctl", "docker", "docker-compose", "ros2", "pkill")


@dataclass
class Decision:
    allowed: bool
    dry_run: bool = False
    reason: str = ""
    code: str = "ok"

    def __bool__(self) -> bool:  # 便于 `if decision:`
        return self.allowed


def validate_target(target: str, *, kind: str = "") -> tuple[bool, str]:
    """校验目标名（容器名 / ROS2 节点名）。

    容器名：`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$`（Docker 命名规范）。
    ROS2 节点名：规范上以 `/` 开头（如 `/camera_node`），允许 `/` 与 `.`，
    因此必须放行前导斜杠——否则 ROS2 模式下的所有节点恢复动作都会被判"目标非法"。
    """
    if not isinstance(target, str) or not target:
        return False, "目标名为空"
    if len(target) > 63:
        return False, "目标名过长（>63）"
    if any(ch in FORBIDDEN_CHARS for ch in target):
        return False, "目标名含非法字符"
    if kind in LOGICAL_TARGET_KINDS and target.startswith("/"):
        if not ROS2_NODE_RE.match(target):
            return False, "ROS2 节点名不符合命名规范"
        return True, ""
    if not TARGET_RE.match(target):
        return False, "目标名不符合命名规范"
    return True, ""


def validate_command(cmd: str) -> tuple[bool, str]:
    """校验恢复命令是否来自允许的前缀集合（防御性二次校验）。"""
    if not isinstance(cmd, str) or not cmd.strip():
        return False, "命令为空"
    if any(ch in cmd for ch in (";", "|", "&", "`", "$(", "\n", ">", "<")):
        return False, "命令含 shell 元字符"
    first = cmd.strip().split()[0]
    if first not in ALLOWED_COMMAND_PREFIXES:
        return False, f"命令前缀 {first!r} 不在允许集合 {ALLOWED_COMMAND_PREFIXES}"
    return True, ""


class SafetyGuard:
    def __init__(self, config, state, audit) -> None:
        self.config = config
        self.state = state
        self.audit = audit

    # ------------------------------------------------------------------ #
    @property
    def tripped(self) -> bool:
        return bool(self.state.get("tripped", False))

    def trip(self, reason: str) -> None:
        self.state.update({"tripped": True, "tripped_at": time.time()})
        logger.warning(f"[guardian] 熔断触发：{reason}")
        self.audit.append(kind="safety.trip", detail=reason, ok=False)

    def reset_trip(self) -> None:
        self.state.update({"tripped": False, "tripped_at": 0})
        self.audit.append(kind="safety.reset", detail="手动解除熔断", ok=True)

    def self_check(self) -> list[str]:
        """启动自检，返回告警清单。"""
        warns: list[str] = []
        # 配置层在归一化过程中收集的 issues（非法值回退、阈值被抬高、参数被夹取…）
        # 必须在这里浮出来。否则 `max_uptime_hours` 被静默抬高、或非法值被静默回退，
        # 运维在 WebUI 与日志里都看不到，"30 天必定真睡一次"这条承诺就被悄悄破坏了。
        for issue in getattr(self.config, "issues", []) or []:
            warns.append(f"配置归一化：{issue}")
        allow = self.config.action_allowlist
        if not allow:
            warns.append("动作白名单为空：所有自动重启都会被拒绝")
        if self.config.safety.allow_self_restart and not self.config.is_self(
                self.config.containers.astrbot):
            warns.append("allow_self_restart 已开启，请确认看门狗策略可用")
        if self.config.safety.self_restart_strategy == "watchdog":
            path = self.config.safety.watchdog_request_path
            if not path:
                warns.append("watchdog_request_path 为空，看门狗策略不可用")
        if not self.config.lifecycle.sleep_targets:
            warns.append("lifecycle.sleep_targets 为空：睡眠功能不可用")
        for w in warns:
            logger.warning(f"[guardian] 安全自检：{w}")
        return warns

    # ------------------------------------------------------------------ #
    def _physical_targets(self, action: ActionSpec) -> list[str]:
        """逻辑目标动作实际会触碰的物理容器名。

        `qq_relogin` 的逻辑目标恒为 "qq"，但按 `qq.relogin_method` 可能真的去重启
        NapCat 容器。宿主机看门狗归属与冷却必须按**物理目标**判定，否则会出现
        "看门狗正持有 NapCat（正在睡眠/重启中），插件同时又去重启 NapCat" 的冲突。

        - restart_container：触碰 `containers.napcat`
        - webui_quick_login / disabled：不触碰容器
        - ros2_restart：作用于 ROS2 节点而非容器，返回空（回落为按节点名记账）
        """
        cfg = self.config
        if action.kind == "qq_relogin":
            method = str(cfg.qq.relogin_method or "").strip()
            if method != "restart_container":
                return []
            napcat = str(cfg.napcat_container or "").strip()
            return [napcat] if napcat else []
        if action.kind in LOGICAL_TARGET_KINDS:
            return []
        return [action.target]

    def _authorize_logical_target(self, action: ActionSpec) -> Decision | None:
        """逻辑目标动作（qq_relogin / ros2_restart）的独立授权校验。

        返回 None = 该校验项通过，继续走后面的冷却/熔断等通用检查；
        返回 Decision = 直接拒绝。
        """
        cfg = self.config
        if action.kind == "qq_relogin":
            method = str(cfg.qq.relogin_method or "").strip()
            if method == "disabled":
                return Decision(False, False,
                                "QQ 重登方式为 disabled（仅告警不动作）",
                                "relogin_disabled")
            if method == "webui_quick_login" and not str(cfg.qq.webui_base or "").strip():
                return Decision(False, False,
                                "未配置 NapCat WebUI 地址，无法快速登录",
                                "relogin_no_webui")
            return None

        if action.kind == "ros2_restart":
            known = set((cfg.ros2.node_restart_commands or {}).keys())
            if action.target not in known:
                return Decision(
                    False, False,
                    f"ROS2 节点 {action.target} 未配置恢复命令"
                    "（ros2.node_restart_commands）",
                    "ros2_no_command")
            return None

        return None

    def authorize(self, action: ActionSpec, now: float | None = None) -> Decision:
        if action.kind != "sleep":
            return self._authorize_single(action, now)
        targets = action.params.get("targets", self.config.lifecycle.sleep_targets)
        duration = action.params.get("duration_min", self.config.lifecycle.sleep_duration_min)
        if (not isinstance(targets, list) or not 1 <= len(targets) <= 10
                or any(not isinstance(t, str) for t in targets)
                or len(set(targets)) != len(targets) or action.target not in targets):
            return Decision(False, False, "睡眠目标必须为 1-10 个不重复容器", "bad_targets")
        if type(duration) is not int or not 0 <= duration <= 120:
            return Decision(False, False, "睡眠时长必须为 [0,120] 内的整数分钟", "bad_duration")
        from dataclasses import replace
        for target in targets:
            decision = self._authorize_single(replace(action, target=target), now)
            if not decision.allowed:
                return decision
        if any(self.config.is_self(t) for t in targets):
            if not self.config.safety.allow_self_restart:
                return Decision(False, False, "本体睡眠未开启 allow_self_restart", "protected")
            if self.config.safety.self_restart_strategy != "watchdog":
                return Decision(False, False, "包含本体的睡眠必须由宿主机看门狗接管全部目标", "watchdog_required")
            if not self.config.safety.watchdog_request_path:
                return Decision(False, False, "看门狗请求路径未配置", "watchdog_unconfigured")
        return Decision(True, False, "全部睡眠目标通过授权", "ok")

    def _authorize_single(self, action: ActionSpec, now: float | None = None) -> Decision:
        now = time.time() if now is None else now
        cfg = self.config
        kind = action.kind

        # 只读动作直接放行
        if kind in READONLY_KINDS:
            return Decision(True, False, "只读动作", "readonly")

        # 熔断
        if self.tripped:
            return Decision(False, False,
                            "熔断已触发（动作次数超限或连续失败），仅告警模式",
                            "circuit_open")

        # 归属判定按物理目标：qq_relogin 物理上会重启 NapCat，若宿主机看门狗正持有
        # NapCat，就必须拒绝，不能因为逻辑目标叫 "qq" 而放行。
        physical = self._physical_targets(action)
        pending = self.state.get("watchdog.pending") or {}
        if set(physical) & set(pending.get("targets") or []):
            return Decision(False, False, "目标仍由宿主机看门狗持有，等待执行结果", "watchdog_pending")

        # 参数规范化
        ok, msg = validate_target(action.target, kind=kind)
        if not ok:
            return Decision(False, False, f"目标非法：{msg}", "bad_target")

        # 白名单：容器名动作 vs 逻辑目标动作走两套校验
        if kind in LOGICAL_TARGET_KINDS:
            d = self._authorize_logical_target(action)
            if d is not None:
                return d
        else:
            allow = cfg.action_allowlist
            if action.target not in allow:
                return Decision(
                    False, False,
                    f"目标 {action.target} 不在动作白名单内（containers.managed）",
                    "not_allowlisted")

            # 受保护容器
            if cfg.is_protected(action.target):
                if kind == "sleep" and cfg.is_self(action.target) and not cfg.safety.allow_self_restart:
                    return Decision(False, False,
                                    "本体容器受保护且未开启 allow_self_restart", "protected")
                if kind != "sleep" and not cfg.safety.allow_self_restart:
                    return Decision(False, False,
                                    f"容器 {action.target} 受保护，需开启 allow_self_restart",
                                    "protected")

        # 冷却（last == 0 表示从未动作过，不参与冷却判断）
        # 同样按物理目标取冷却：qq_relogin 重启的是 NapCat，就该受 NapCat 的冷却约束，
        # 否则"刚重启完 NapCat"和"QQ 掉线重登再重启 NapCat"会互相绕过。
        cooldown = float(cfg.safety.action_cooldown_sec or 0)
        if action.params.get("cooldown_key") == "memory_leak":
            cooldown = max(cooldown, float(cfg.memory.leak_cooldown or 0))
        cool_targets = physical or [action.target]
        last = max((self.state.cooldown_of(t) for t in cool_targets), default=0.0)
        if cooldown > 0 and last > 0 and (now - last) < cooldown:
            remain = int(cooldown - (now - last))
            return Decision(False, False,
                            f"目标 {action.target} 处于冷却中，剩余 {remain}s",
                            "cooldown")

        # 每小时上限 / 熔断
        window = int(cfg.safety.circuit_breaker_window_sec or 1800)
        limit = int(cfg.safety.max_actions_per_hour or 6)
        if self.state.actions_in_window(window) >= limit:
            self.trip(f"{window}s 内动作次数达到上限 {limit}")
            return Decision(False, False, "动作次数超限，已熔断", "circuit_open")

        # 连续失败降级
        fs = int((self.state.get("fail_streak") or {}).get(action.target, 0))
        if fs >= int(cfg.safety.max_consecutive_failures or 3):
            return Decision(False, False,
                            f"目标 {action.target} 连续失败 {fs} 次，降级为只告警",
                            "fail_streak")

        return Decision(True, False, "通过", "ok")

    # ------------------------------------------------------------------ #
    def commit(self, action: ActionSpec, ok: bool) -> list[tuple[str, int]]:
        """动作执行后登记：冷却时间戳、动作计数、失败连击。

        注意：dry-run 动作（`ActionSpec.dry_run`，由 `authorize()` 给出）不产生
        真实副作用，因此**不消耗**冷却与配额，否则空跑会把后续真实动作全部挡掉。

        返回值：本轮**首次**跨过连续失败阈值的目标列表 ``[(target, streak), ...]``。
        调用方（引擎）据此执行 B00 §10.6 的"降级为只告警 + 进入 maintenance 档位"。
        之所以不在这里直接改档位：safety 层只做授权与记账，不持有调度/档位状态；
        由引擎统一处理可避免同一失败在多条路径（巡检 / 睡眠 / 看门狗对账）里
        被重复降级、重复通知。
        """
        if action.dry_run:
            return []
        if action.kind == "sleep":
            targets = list(action.params.get("targets", [action.target]))
        else:
            # 逻辑目标动作按物理容器记账（qq_relogin -> napcat）：否则冷却与失败连击
            # 会记在 "qq" 上，与容器级动作互相看不见，形成绕过通道。
            targets = self._physical_targets(action) or [action.target]
        threshold = int(self.config.safety.max_consecutive_failures or 3)
        degraded: list[tuple[str, int]] = []
        self.state.record_action()  # One authorized group request consumes one action quota.
        for target in targets:
            self.state.bump_cooldown(target)
            if ok:
                self.state.reset_fail_streak(target)
            else:
                streak = self.state.bump_fail_streak(target)
                # 只在"恰好跨过阈值"的那一刻上报一次，避免持续失败时反复触发降级/通知。
                if streak == threshold:
                    logger.warning(
                        f"[guardian] 目标 {target} 连续失败 {streak} 次，自动降级为只告警")
                    degraded.append((target, streak))
        return degraded
