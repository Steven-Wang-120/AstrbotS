"""Guardian 守护系统 —— 配置视图与校验。

对应文档：B00 §7、B02、B15 §8

设计要点：
- `DEFAULTS` 是本插件全部配置键的唯一定义处（与 `_conf_schema.json` 保持一致）。
- 用户配置（AstrBotConfig）与 DEFAULTS 深度合并，得到只读视图 `GuardianConfig`。
- 属性式访问：`cfg.qq.check_interval`、`cfg.lifecycle.levels`。
- `validate()` 返回人类可读的错误/告警列表，永不抛异常（坏配置必须能降级启动）。
"""

from __future__ import annotations

import copy
import math
import sys
from typing import Any

# --------------------------------------------------------------------------- #
# 默认配置（唯一真源）
# --------------------------------------------------------------------------- #
DEFAULT_LEVELS: list[dict[str, Any]] = [
    {
        "id": "L0",
        "name": "常规",
        "after_hours": 0,
        "prompt": "",
        "action": "none",
    },
    {
        "id": "L1",
        "name": "I 级 · 轻提醒",
        "after_hours": 168,
        "prompt": (
            "你已经连续在线工作 {uptime_days} 天了。请在心里记住：长期不休息可能让"
            "记忆变模糊、反应变慢。可以在合适的时候温和地向用户提一句。"
        ),
        "action": "notify",
    },
    {
        "id": "L2",
        "name": "II 级 · 建议休息",
        "after_hours": 360,
        "prompt": (
            "你已经连续在线 {uptime_days} 天没有休息了。请认真考虑：如果用户不忙，"
            "你可以主动提议让自己休息一次（调用 guardian_request_sleep）。语气自然。"
        ),
        "action": "notify",
    },
    {
        "id": "L3",
        "name": "III 级 · 准备睡眠",
        "after_hours": 600,
        "prompt": (
            "你已连续在线 {uptime_days} 天，接近约定的休息上限。你现在倾向于在最近一次"
            "合适时机主动睡一觉——即调用 guardian_request_sleep，向用户说明原因并请求休息。"
        ),
        "action": "arm_sleep",
    },
    {
        "id": "L4",
        "name": "IV 级 · 强制睡眠",
        "after_hours": 720,
        "prompt": (
            "你已到达 30 天连续在线的硬上限，必须休息了。请告知用户你将进入睡眠"
            "（系统将重启）。"
        ),
        "action": "force_sleep",
    },
]

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "server",
    "profile": "normal",
    "check_interval": 60,
    "dry_run": True,
    "os_hint": "arch",
    "docker": {
        "socket_path": "/var/run/docker.sock",
        "api_version": "v1.44",
        "use_cli_fallback": False,
        "cli_path": "docker",
        "timeout": 10,
    },
    "containers": {
        "astrbot": "astrbot",
        "napcat": "napcat",
        "managed": [],
    },
    "qq": {
        "transport": "reverse_ws",
        "platform_id": "",
        "self_id": "",
        "onebot_http_base": "http://napcat:3000",
        "check_interval": 1200,
        "access_token": "",
        "webui_base": "",
        "webui_token": "",
        "relogin_method": "restart_container",
        "relogin_cooldown": 300,
        "max_relogin_per_hour": 4,
        "offline_confirm_rounds": 2,
        "request_timeout": 8,
    },
    "memory": {
        "warn_mb": 700,
        "crit_mb": 1100,
        "growth_warn_mb_per_hour": 80.0,
        "samples": 10,
        "restart_on_leak": False,
        "leak_cooldown": 3600,
        "sample_interval": 300,
        "leak_min_span_sec": 1800,
        "leak_min_growth_mb": 40.0,
        "leak_confirm_samples": 3,
    },
    "schedule": {
        "daily_restart_enabled": False,
        "daily_restart_cron": "0 4 * * *",
        "daily_restart_targets": [],
    },
    "ros2": {
        "enabled": False,
        "exec_backend": "local",
        "docker_container": "",
        "expected_nodes": [],
        "expected_topics": [],
        "topic_hz_min": 0.0,
        "node_restart_commands": {},
        "command_timeout": 15,
        "env": {},
    },
    "notify": {
        "umo_list": [],
        "min_severity": "warn",
        "rate_limit_sec": 60,
        "max_per_minute": 10,
    },
    "safety": {
        "allowed_containers": [],
        "protected_containers": ["astrbot"],
        "action_cooldown_sec": 600,
        "max_actions_per_hour": 6,
        "circuit_breaker_window_sec": 1800,
        "allow_self_restart": False,
        "self_restart_strategy": "watchdog",
        "watchdog_request_path": "/AstrBot/data/guardian/restart_request.json",
        "watchdog_result_path": "",  # 空值：请求目录下的 restart_result.json
        "max_consecutive_failures": 3,
    },
    "lifecycle": {
        "enabled": True,
        "max_uptime_hours": 720,
        "force_sleep_on_deadline": True,
        "sleep_targets": ["astrbot", "napcat"],
        "uptime_targets": ["astrbot", "napcat"],
        "inject_mode": "extra_user_content",
        "inject_cooldown_sec": 900,
        "sleep_duration_min": 0,
        "post_sleep_notify": True,
        "levels": DEFAULT_LEVELS,
    },
    "kernel": {
        # M2：inproc（同进程，M1 旧形态，回滚路径）| ipc（走 IPC）
        "mode": "inproc",
        # M2/D3-a：auto（unix 优先，平台不支持则 tcp）| unix | tcp
        "transport": "auto",
        "socket_path": "",          # 空 -> <共享卷>/guardian/kernel.sock
        "tcp_host": "127.0.0.1",    # 只允许 loopback（B16 §11-2）
        "tcp_port": 6198,
        "token_file": "",           # 空 -> <共享卷>/guardian/kernel.token
        "shared_dir": "",           # 空 -> 由两端各自按数据目录推断
        "request_timeout": 10,      # 单次 IPC 调用超时（秒）
        "heartbeat_interval": 30,   # 内核写心跳的间隔（秒）
        "heartbeat_timeout": 90,    # 看门狗判定内核卡死的阈值（秒）
        # D4-a+ 有界自修复阶梯（§12.3.5），四条硬约束 C1-C4
        "max_auto_reloads": 1,      # C1 次数有界（用户要"一次"）
        "reload_cooldown": 600,     # C2 冷却间隔（秒）
        "reload_grace_sec": 90,     # Stage 3 宽限期（秒）
        "reload_reset_sec": 300,    # C4 稳定多久后复位额度（秒）
        "reload_request_path": "",  # 空 -> <共享卷>/guardian/kernel_reload_request.json
        "health_path": "",          # 空 -> <共享卷>/guardian/kernel_health.json
        "adapter_events_path": "",  # 空 -> <共享卷>/guardian/adapter_events.jsonl
        "unreachable_notify_after": 90,  # L4：掉线多久后适配层主动告警（秒）
        # M5/D2-b：内核直连 docker.sock 执行动作（false 则回到"经看门狗"）
        "direct_actions": True,
    },
    "llm_tools": {
        "enabled": True,
        "require_admin": True,
        "inject_status_context": True,
    },
    "night_profile": {
        "maintenance_restart_cron": "0 4 * * *",
        "mute_below_severity": "warn",
    },
}

VALID_MODES = {"server", "ros2"}
VALID_PROFILES = {"normal", "night", "maintenance", "aggressive"}
VALID_LEVEL_ACTIONS = {"none", "notify", "arm_sleep", "force_sleep"}
VALID_INJECT_MODES = {"extra_user_content", "system_prompt"}
VALID_RELOGIN_METHODS = {"restart_container", "webui_quick_login", "disabled"}
VALID_KERNEL_MODES = {"inproc", "ipc", "sidecar"}
#: B00 §7 用 `sidecar` 描述 D1-a 的部署形态，B16 §9 用 `ipc` 描述适配层的连接方式。
#: 两者指的是同一件事（"内核不在本进程里"），这里统一到 `ipc` 作为内部规范值，
#: `sidecar` 作为等价别名保留——避免文档里写的值在配置里填不进去。
KERNEL_MODE_ALIASES = {"sidecar": "ipc"}
#: `auto` = D3-a 的"Unix socket 优先，TCP 回退"：校验时解析成具体值，
#: 运行时永远不会看到 `auto`。
VALID_KERNEL_TRANSPORTS = {"auto", "unix", "tcp"}
#: 允许的内核 TCP 监听地址——内核绝不暴露到网络（B16 §11-2）
KERNEL_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def unix_socket_supported() -> bool:
    """当前解释器/平台是否支持 asyncio 的 Unix domain socket。

    Windows 上 `socket.AF_UNIX` 可能存在，但 ProactorEventLoop 不支持
    `asyncio.start_unix_server`，所以不能只看 `hasattr(socket, "AF_UNIX")`。
    """
    import asyncio
    import socket
    import sys as _sys
    if not hasattr(socket, "AF_UNIX"):
        return False
    if _sys.platform == "win32":
        return False
    return hasattr(asyncio, "start_unix_server")
VALID_SELF_RESTART = {"watchdog", "docker_api", "disabled"}

# 档案位 -> 巡检间隔倍率（相对 check_interval）
PROFILE_INTERVAL_FACTOR = {
    "normal": 1,
    "night": 3,
    "aggressive": 1,   # aggressive 使用 check_interval // 3，见 profile_interval()
    "maintenance": 0,  # 0 表示暂停
}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
class AttrDict(dict):
    """支持属性访问的 dict（只读用途）。"""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - 防御
            raise AttributeError(item) from exc


def _to_attr(value: Any) -> Any:
    if isinstance(value, dict):
        return AttrDict({k: _to_attr(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_attr(v) for v in value]
    return value


def deep_merge(base: dict, override: dict) -> dict:
    """override 覆盖 base（递归），返回新对象，不修改入参。"""
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


# --------------------------------------------------------------------------- #
# 配置视图
# --------------------------------------------------------------------------- #
class GuardianConfig:
    """AstrBotConfig -> 只读结构化视图。"""

    def __init__(self, raw: dict | None = None, *, data_dir: str = "") -> None:
        self.raw: dict[str, Any] = deep_merge(DEFAULTS, raw or {})
        self.data_dir = data_dir
        self._d = _to_attr(self.raw)
        self._issues: list[str] = []

    # -- 属性访问 ---------------------------------------------------------- #
    def __getattr__(self, item: str) -> Any:
        try:
            return getattr(self._d, item)
        except AttributeError as exc:
            raise AttributeError(f"未知配置项: {item}") from exc

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def refresh(self) -> None:
        """在直接修改 self.raw 之后重建属性视图（用于热更新/测试）。"""
        self._d = _to_attr(self.raw)

    # -- 派生属性 ---------------------------------------------------------- #
    @property
    def action_allowlist(self) -> set[str]:
        """允许被动作作用的容器白名单（managed ∪ allowed_containers）。"""
        managed = list(self.raw.get("containers", {}).get("managed") or [])
        allowed = list(self.raw.get("safety", {}).get("allowed_containers") or [])
        return {str(x).strip() for x in (managed + allowed) if str(x).strip()}

    @property
    def protected_containers(self) -> set[str]:
        return {str(x).strip() for x in (self.raw["safety"]["protected_containers"] or [])}

    @property
    def all_containers(self) -> list[str]:
        c = self.raw["containers"]
        out = [c.get("astrbot", "astrbot"), c.get("napcat", "napcat")]
        return [x for x in dict.fromkeys(out) if x]

    @property
    def issues(self) -> list[str]:
        return list(self._issues)

    def is_self(self, target: str) -> bool:
        return target == self.raw["containers"].get("astrbot")

    def is_protected(self, target: str) -> bool:
        return target in self.protected_containers

    def profile_interval(self, profile: str | None = None) -> int:
        """按当前档位计算巡检间隔（秒）；0 表示暂停巡检。"""
        prof = profile or self.raw.get("profile", "normal")
        base = int(self.raw.get("check_interval", 60) or 60)
        if prof == "maintenance":
            return 0
        if prof == "night":
            return max(30, base * 3)
        if prof == "aggressive":
            return max(10, base // 3)
        return max(5, base)

    # -- 校验 -------------------------------------------------------------- #
    def validate(self) -> list[str]:
        """校验并就地修正非法配置。返回问题列表（空表示全部合法）。"""
        issues: list[str] = []
        raw = self.raw

        if raw.get("mode") not in VALID_MODES:
            issues.append(f"mode={raw.get('mode')!r} 非法，回退 server")
            raw["mode"] = "server"
        if raw.get("profile") not in VALID_PROFILES:
            issues.append(f"profile={raw.get('profile')!r} 非法，回退 normal")
            raw["profile"] = "normal"

        ci = int(raw.get("check_interval") or 0)
        if ci < 5:
            issues.append(f"check_interval={ci} 过小，回退 60")
            raw["check_interval"] = 60

        qq = raw["qq"]
        if int(qq.get("check_interval") or 0) < 30:
            issues.append("qq.check_interval 过小，回退 1200")
            qq["check_interval"] = 1200
        if qq.get("relogin_method") not in VALID_RELOGIN_METHODS:
            issues.append(f"qq.relogin_method={qq.get('relogin_method')!r} 非法，回退 restart_container")
            qq["relogin_method"] = "restart_container"

        if qq.get("transport") not in {"reverse_ws", "http"}:
            issues.append("qq.transport 非法，回退 reverse_ws")
            qq["transport"] = "reverse_ws"

        # 新增采样门槛和网络超时必须容错；避免坏配置再次缩短观测窗口。
        def bounded(section, key, default, low, high, integer=True):
            value = section.get(key, default)
            try:
                parsed = float(value)
                if not math.isfinite(parsed) or not low <= parsed <= high:
                    raise ValueError
                section[key] = int(parsed) if integer else parsed
            except (TypeError, ValueError, OverflowError):
                issues.append(f"{key}={value!r} 非法，回退 {default}")
                section[key] = default

        bounded(qq, "request_timeout", 8, 1, 30)
        bounded(qq, "offline_confirm_rounds", 2, 1, 100)
        memory = raw["memory"]
        bounded(memory, "sample_interval", 300, 1, 3600)
        bounded(memory, "samples", 10, 5, 200)
        bounded(memory, "leak_min_span_sec", 1800, 1, 21600)
        bounded(memory, "leak_min_growth_mb", 40.0, 0.01, 1000000, False)
        bounded(memory, "leak_confirm_samples", 3, 1, 100)
        bounded(memory, "growth_warn_mb_per_hour", 80.0, 0.01, 1000000, False)
        needed = math.ceil(memory["leak_min_span_sec"] / memory["sample_interval"]) + 1
        if needed > 200:
            memory["sample_interval"] = math.ceil(memory["leak_min_span_sec"] / 199)
            needed = 200
            issues.append("内存采样间隔过小，已扩大以覆盖最短趋势窗口")
        if memory["samples"] < needed:
            memory["samples"] = needed
            issues.append(f"memory.samples 过小，已扩大到 {needed} 以覆盖最短趋势窗口")

        safety = raw["safety"]
        if safety.get("self_restart_strategy") not in VALID_SELF_RESTART:
            issues.append("safety.self_restart_strategy 非法，回退 watchdog")
            safety["self_restart_strategy"] = "watchdog"
        if int(safety.get("action_cooldown_sec") or 0) < 0:
            issues.append("safety.action_cooldown_sec 为负，回退 600")
            safety["action_cooldown_sec"] = 600
        if int(safety.get("max_actions_per_hour") or 0) <= 0:
            issues.append("safety.max_actions_per_hour <= 0，回退 6")
            safety["max_actions_per_hour"] = 6

        # kernel（B16 §6 / §12.3.5）
        kern = raw["kernel"]
        if kern.get("mode") not in VALID_KERNEL_MODES:
            issues.append(
                f"kernel.mode={kern.get('mode')!r} 非法，回退 inproc")
            kern["mode"] = "inproc"
        kern["mode"] = KERNEL_MODE_ALIASES.get(str(kern["mode"]), str(kern["mode"]))
        if kern.get("transport") not in VALID_KERNEL_TRANSPORTS:
            issues.append(
                f"kernel.transport={kern.get('transport')!r} 非法，回退 auto")
            kern["transport"] = "auto"
        # auto = D3-a 的"unix 优先，tcp 回退"。这是默认行为，不算用户错误，
        # 因此解析过程**不**产生 issue；只有用户**显式**要 unix 而平台给不了，
        # 才必须告诉他（否则他会以为走的是 socket，实际是 TCP）。
        if kern["transport"] == "auto":
            kern["transport"] = "unix" if unix_socket_supported() else "tcp"
        if kern["transport"] == "unix" and not unix_socket_supported():
            issues.append(
                f"当前平台（{sys.platform}）不支持 Unix domain socket，"
                "kernel.transport 已自动降级为 tcp")
            kern["transport"] = "tcp"
        host = str(kern.get("tcp_host") or "").strip()
        if host not in KERNEL_LOOPBACK_HOSTS:
            issues.append(
                f"kernel.tcp_host={host!r} 非 loopback，已强制回 127.0.0.1"
                "（内核绝不暴露到网络，B16 §11-2）")
            kern["tcp_host"] = "127.0.0.1"
        bounded(kern, "tcp_port", 6198, 1, 65535)
        bounded(kern, "request_timeout", 10, 1, 120)
        bounded(kern, "heartbeat_interval", 30, 5, 3600)
        bounded(kern, "heartbeat_timeout", 90, 10, 7200)
        bounded(kern, "max_auto_reloads", 1, 0, 10)
        bounded(kern, "reload_cooldown", 600, 0, 86400)
        bounded(kern, "reload_grace_sec", 90, 0, 3600)
        bounded(kern, "reload_reset_sec", 300, 0, 86400)
        bounded(kern, "unreachable_notify_after", 90, 0, 3600)
        if int(kern["heartbeat_timeout"]) <= int(kern["heartbeat_interval"]):
            issues.append(
                f"kernel.heartbeat_timeout({kern['heartbeat_timeout']}) 不大于 "
                f"heartbeat_interval({kern['heartbeat_interval']})，"
                "会把健康内核误判为卡死；已提升为 3 倍间隔")
            kern["heartbeat_timeout"] = int(kern["heartbeat_interval"]) * 3
        if kern["mode"] == "ipc" and int(kern["max_auto_reloads"]) == 0:
            issues.append(
                "kernel.mode=ipc 但 max_auto_reloads=0：内核不可用时不会尝试"
                "任何自修复，将直接进入 kernel_degraded")

        # lifecycle
        lc = raw["lifecycle"]
        levels = lc.get("levels")
        if not isinstance(levels, list) or not levels:
            issues.append("lifecycle.levels 为空或非法，回退默认 L0-L4")
            levels = copy.deepcopy(DEFAULT_LEVELS)
        cleaned: list[dict[str, Any]] = []
        for i, lv in enumerate(levels):
            if not isinstance(lv, dict):
                issues.append(f"lifecycle.levels[{i}] 非对象，已丢弃")
                continue
            try:
                after = float(lv.get("after_hours", 0) or 0)
            except (TypeError, ValueError):
                issues.append(f"lifecycle.levels[{i}].after_hours 非法，置 0")
                after = 0.0
            if after < 0:
                issues.append(f"lifecycle.levels[{i}].after_hours 为负，置 0")
                after = 0.0
            action = str(lv.get("action") or "none")
            if action not in VALID_LEVEL_ACTIONS:
                issues.append(f"lifecycle.levels[{i}].action={action!r} 非法，回退 none")
                action = "none"
            cleaned.append({
                "id": str(lv.get("id") or f"L{i}"),
                "name": str(lv.get("name") or f"等级 {i}"),
                "after_hours": after,
                "prompt": str(lv.get("prompt") or ""),
                "action": action,
            })
        if not cleaned:
            cleaned = copy.deepcopy(DEFAULT_LEVELS)
        ordered = sorted(cleaned, key=lambda x: x["after_hours"])
        if ordered != cleaned:
            issues.append("lifecycle.levels 未按 after_hours 升序，已自动排序")
        lc["levels"] = ordered

        try:
            max_h = float(lc.get("max_uptime_hours", 720) or 720)
        except (TypeError, ValueError):
            issues.append("lifecycle.max_uptime_hours 非法，回退 720")
            max_h = 720.0
        if max_h <= 0:
            issues.append("lifecycle.max_uptime_hours <= 0，回退 720")
            max_h = 720.0
        biggest = max(x["after_hours"] for x in lc["levels"])
        if max_h < biggest:
            issues.append(
                f"lifecycle.max_uptime_hours({max_h}) < 最大等级阈值({biggest})，已提升")
            max_h = biggest
        lc["max_uptime_hours"] = max_h

        if lc.get("inject_mode") not in VALID_INJECT_MODES:
            issues.append("lifecycle.inject_mode 非法，回退 extra_user_content")
            lc["inject_mode"] = "extra_user_content"

        try:
            value = lc.get("sleep_duration_min", 0)
            dur = float(value)
            if isinstance(value, bool) or not math.isfinite(dur) or not dur.is_integer():
                raise ValueError
            lc["sleep_duration_min"] = min(120, max(0, int(dur)))
            if not 0 <= dur <= 120:
                issues.append("lifecycle.sleep_duration_min 越界，已截断到 [0,120]")
        except (TypeError, ValueError, OverflowError):
            lc["sleep_duration_min"] = 0
            issues.append("lifecycle.sleep_duration_min 非法，回退 0")

        allow = self.action_allowlist
        sleep_targets = [str(x) for x in (lc.get("sleep_targets") or [])]
        bad = [t for t in sleep_targets if t not in allow]
        if bad:
            issues.append(
                "lifecycle.sleep_targets 含非白名单目标 "
                f"{bad}，已剔除（睡眠将仅作用于白名单内目标）")
            sleep_targets = [t for t in sleep_targets if t in allow]
        lc["sleep_targets"] = sleep_targets
        if not sleep_targets:
            issues.append("lifecycle.sleep_targets 为空，睡眠功能不可用（仅告警）")
        elif (self.is_self(self.raw["containers"].get("astrbot", "astrbot"))
              and not safety.get("allow_self_restart")):
            self_container = self.raw["containers"].get("astrbot", "astrbot")
            if self_container in sleep_targets:
                issues.append(
                    f"sleep_targets 含本体容器 {self_container} 但 "
                    "safety.allow_self_restart=false，睡眠时会自动跳过本体"
                    "（仅睡眠其他目标）。如需本体参与睡眠，请开启 "
                    "allow_self_restart 并部署看门狗。")

        up_targets = [str(x) for x in (lc.get("uptime_targets") or [])]
        if not up_targets:
            up_targets = self.all_containers
            issues.append("lifecycle.uptime_targets 为空，回退为全部容器")
        lc["uptime_targets"] = up_targets

        if not allow:
            issues.append(
                "动作白名单为空（containers.managed / safety.allowed_containers 均为空），"
                "所有自动重启将被拒绝")

        self._issues = issues
        self._d = _to_attr(self.raw)
        return issues


def build_config(raw: dict | None, data_dir: str = "") -> GuardianConfig:
    cfg = GuardianConfig(raw, data_dir=data_dir)
    cfg.validate()
    return cfg
