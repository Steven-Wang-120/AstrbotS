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
# --------------------------------------------------------------------------- #
# 等级模板键（`template_list` 的条目类型标识）
# --------------------------------------------------------------------------- #
# AstrBot 的 `template_list` 里，每个条目都必须带一个 `__template_key`，指回
# `_conf_schema.json` 中 `templates.<key>` 的定义。前端靠它：
#   - 决定标题显示哪个模板名（`templateLabel(entry.__template_key)`）
#   - 决定这一条渲染哪些字段（`getTemplate(entry)` 取不到就整条报警）
# 后端靠它：保存时逐条校验（`config_service._validate_template_list`，缺了直接
# 拒绝保存）。它**不是**我们的业务字段，而是一个面向 WebUI 的寄宿标记；我们的
# 运行时代码从不读它（`validate()` 重建等级对象时会丢掉它）。
#
# 少了它就会出现"未指定模板 / 找不到对应模板，请删除后重新添加"——条目数据
# 其实完好无损，只是前端不知道该怎么渲染。
LEVEL_TEMPLATE_KEY = "level"


def _with_template_key(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给等级列表补上 `__template_key`（已存在的不动，保持幂等）。"""
    out: list[dict[str, Any]] = []
    for lv in levels:
        if not isinstance(lv, dict):
            continue
        entry = dict(lv)
        entry.setdefault("__template_key", LEVEL_TEMPLATE_KEY)
        out.append(entry)
    return out


DEFAULT_LEVELS: list[dict[str, Any]] = _with_template_key([
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
])

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "server",
    "profile": "normal",
    "check_interval": 60,
    "docker": {
        # 空值 = 自动探测：`DOCKER_HOST` 优先，否则依次试常见 socket 路径。
        # 写死 `/var/run/docker.sock` 会让"挂载点不同/走 TCP"的部署永远连不上。
        "socket_path": "",
        # 空值 = 启动时问 daemon 要真实版本（见 DockerClient.negotiate）。
        "api_version": "",
        "use_cli_fallback": False,
        "cli_path": "docker",
        "timeout": 10,
    },
    # 容器名（Docker `container_name`，**不是** compose 服务名）。
    # 这里是全部"目标名"的唯一事实来源：下面的 `@self` / `@napcat` 别名、
    # 本体保护判定、巡检/内存探针都从这两个值派生（B12 §容器名一致性）。
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
        # B17 §5.1：浏览器侧地址（只用于拼直达链接，绝不参与 API 调用）
        "webui_public_base": "",
        # B17 §5.1：账密登录凭据。`account` 为空即退化为 v0.5.0（只重启不登录）。
        # `password` 填 32 位小写 hex 视为 md5 直接使用；否则按明文在内存里求 md5。
        "account": "",
        "password": "",
        # B17 §5.2：`password_is_md5` 是**派生键**，不写 schema，只在运行时视图里存在。
        "password_is_md5": False,
        "restart_grace_sec": 3600,
        "login_ready_timeout": 90,
        "login_retry_interval": 3,
        "password_fail_hold_sec": 21600,
        "relogin_method": "restart_container",
        "relogin_cooldown": 300,
        "max_relogin_per_hour": 4,
        "offline_confirm_rounds": 2,
        "request_timeout": 8,
        # F11：把"最近有消息事件"作为 QQ 在线的**弱正据**。在本部署（NapCat
        # 未开 OneBot 11 HTTP）里，协议端活跃度是唯一真正可用的证据链；窗口内
        # 有消息 = 协议端活着。设为 0 关闭该证据链。
        "activity_evidence_sec": 1800,
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
        # 前端只暴露这两个"人能读"的字段；cron 由 `_sync_daily_restart_cron()`
        # 派生。5 字段 cron 对新手是纯门槛（`0 4 * * *` 到底几点？），而本插件
        # 的定时重启只可能是"每天 HH:MM"，根本用不上 dom/month/dow。
        "daily_restart_time": "04:00",
        "daily_restart_weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        # 派生/兼容字段：不进前端。老配置里手写的 cron 仍被尊重（见 sync 逻辑）。
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
        # 只列**额外**受保护的容器。本体容器（`containers.astrbot`）由
        # `is_protected()` 无条件保护，不依赖这个列表——所以改名时忘了同步这里，
        # 也不会让本体失去保护（B12 §容器名一致性）。
        "protected_containers": [],
        "action_cooldown_sec": 600,
        "max_actions_per_hour": 6,
        "circuit_breaker_window_sec": 1800,
        "allow_self_restart": False,
        "self_restart_strategy": "watchdog",
        "watchdog_request_path": "/AstrBot/data/guardian/restart_request.json",
        "watchdog_result_path": "",  # 空值：请求目录下的 restart_result.json
        # 插件启动时尝试在**宿主机**上安装看门狗（Linux 宿主可全自动；
        # Windows 宿主只能提示手动执行，因为 Linux 容器跑不了 powershell）。
        # 关掉它就完全不做任何尝试，只做检查与告警。
        "auto_install_watchdog": True,
        # 宿主机上的 compose 目录。留空 = 从容器标签
        # `com.docker.compose.project.working_dir` 自动读取（推荐留空）。
        "watchdog_compose_dir": "",
        "max_consecutive_failures": 3,
    },
    "lifecycle": {
        "enabled": True,
        "max_uptime_hours": 720,
        "force_sleep_on_deadline": True,
        # `@self` / `@napcat` 是**符号别名**，分别解析为 `containers.astrbot` /
        # `containers.napcat`。用户只需在前端填一次容器名，改名时这两个列表不必跟着改
        # （B12 §容器名一致性）。直接写真实容器名同样有效。
        "sleep_targets": ["@self", "@napcat"],
        "uptime_targets": ["@self", "@napcat"],
        "inject_mode": "extra_user_content",
        "inject_cooldown_sec": 900,
        "sleep_duration_min": 0,
        "post_sleep_notify": True,
        "levels": DEFAULT_LEVELS,
    },
    # Bot 的作息（**不是用户的作息**）。窗口只做闸门，不做触发器：
    # 困意（uptime → Ln）决定"该不该睡"，这里决定"能不能睡"。
    "circadian": {
        "enabled": True,
        # asleep 窗口：Bot 表现为"困"、且允许重启本体的时段。
        # 左闭右开；支持跨零点（如 23:00–07:00）。
        "asleep_start": "02:00",
        "asleep_end": "07:00",
        # 顺延上限：超过这个时长无视窗口强制执行，防止死锁。
        # 没有它的话"深夜一直有人聊天"会让睡眠永远排不上队，
        # `lifecycle.max_uptime_hours` 的保证就被架空了。
        "max_defer_hours": 12,
        # 窗口内最近多少分钟有人说话就再等等
        "quiet_minutes": 10,
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
        # M4：旧数据目录（AstrBot 插件数据目录）。空 -> 由 shared_dir 推断；
        # 再空则不迁移。迁移幂等、只复制不删除（B16 §7）。
        "legacy_data_dir": "",
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
VALID_RELOGIN_METHODS = {"restart_container", "login_only", "disabled"}
#: B17 §5.3：`webui_quick_login` 是历史值，代码实际打的 `/api/QQLogin/QuickLogin`
#: 在 NapCat 4.18.14 上是 404，策略形同虚设。归一化时迁移为 `login_only` 并记 issue。
RELOGIN_METHOD_ALIASES = {"webui_quick_login": "login_only"}
VALID_KERNEL_MODES = {"inproc", "ipc", "sidecar"}

#: 已退役的配置键 -> 退役说明。`validate()` 遇到就删键并记一条 issue：
#: 留着不删会让 WebUI 下次保存把旧键写回，删了不报则运维看不见"开关消失了"。
#: 注意：能直接删的才放这里。`safety.allowed_containers` 需要在删之前把值
#: **迁移**进 `containers.managed`，因此单独处理（见 `_migrate_allowed_containers`）。
RETIRED_KEYS: tuple[tuple[str, str], ...] = (
    ("os_hint", "系统信息改由 env_platform 探针从宿主机侧采集，无需再手填"),
    ("dry_run", "整插件级空跑已取消，配置面不再有该开关"),
)

#: 与 `containers.managed` 语义完全重复的旧白名单键（`safety.allowed_containers`）。
#: 保留常量只为迁移逻辑可读；它本身不再参与任何授权判定。
LEGACY_ALLOWED_CONTAINERS = ("safety", "allowed_containers")

#: 星期缩写 -> cron 的 day-of-week（0=周日）。
#: 顺序即前端展示顺序（周一到周日），与 cron 数值解耦。
_WEEKDAY_INDEX: dict[str, int] = {
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0,
}


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    """解析 `HH:MM` / `H:MM`，返回 (hour, minute)；非法返回 None。"""
    text = (value or "").strip()
    if ":" not in text:
        return None
    hh, _, mm = text.partition(":")
    try:
        hour, minute = int(hh), int(mm.strip())
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _hhmm_to_minutes(value: str) -> int | None:
    """`HH:MM` -> 当日分钟数；非法返回 None。

    与 `core/timewindow.py` 的 `parse_hhmm` 语义一致（同样拒绝 `24:00`），
    但这里只服务于配置校验，不引入 `timewindow` 以保持 config 无依赖。
    """
    if not isinstance(value, str):
        return None
    parsed = _parse_hhmm(value)
    return None if parsed is None else parsed[0] * 60 + parsed[1]

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

#: 目标列表里可用的**符号别名**。用户只在前端填一次容器名，所有策略列表引用槽位，
#: 改名时不必到处同步（B12 §容器名一致性）。未识别的值按真实容器名原样保留。
TARGET_ALIASES: dict[str, str] = {
    "@self": "astrbot",     # -> containers.astrbot（本体容器）
    "@napcat": "napcat",    # -> containers.napcat（协议端容器）
}

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


def _is_md5_hex(value: str) -> bool:
    """32 位小写 hex 视为"用户已提供 md5"（B17 §5.1）。

    只认**小写**：大写 hex 仍按明文处理，避免同一份密码在两种大小写下产生
    两个不同的 md5 解释；这条规则由 `password_is_md5` 显式呈现，不做静默转换。
    """
    if len(value) != 32:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


# --------------------------------------------------------------------------- #
# 配置视图
# --------------------------------------------------------------------------- #
class GuardianConfig:
    """AstrBotConfig -> 只读结构化视图。"""

    def __init__(self, raw: dict | None = None, *, data_dir: str = "") -> None:
        provided = raw or {}
        # `deep_merge` 会把 DEFAULTS 里的键全部补齐，之后就分不清"用户填了"
        # 和"默认值兜底"了。退役键迁移与定时重启的 legacy-cron 兼容都必须
        # 看**用户原本给了什么**，所以在这里先把原始 section 的键记下来。
        sched_in = provided.get("schedule")
        self._provided_schedule_keys: set[str] = (
            set(sched_in.keys()) if isinstance(sched_in, dict) else set())
        self.raw: dict[str, Any] = deep_merge(DEFAULTS, provided)
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
    def _names(self, section: str, key: str) -> list[str]:
        """从某个配置段取字符串列表；段被写成非对象时按空处理（validate 永不抛）。"""
        block = self.raw.get(section)
        listed = block.get(key) if isinstance(block, dict) else None
        return [str(x) for x in (listed or [])]

    @property
    def action_allowlist(self) -> set[str]:
        """允许被动作作用的容器白名单（`containers.managed` 是唯一来源）。

        曾被拆成 `containers.managed` ∪ `safety.allowed_containers` 两份列表，
        但两者语义完全相同（都是"允许被守护/重启的容器名"），前端因此出现两个
        必须同时维护、漏填任何一个都静默失效的输入框。现统一收敛到
        `containers.managed`；`safety.allowed_containers` 已退役，由
        `validate()` 迁移进 `containers.managed` 并给出可见告警。
        """
        managed = self._names("containers", "managed")
        return {x.strip() for x in managed if x.strip()}

    @property
    def self_container(self) -> str:
        """本体容器名（`containers.astrbot`）。全部"本体"语义的唯一取值处。"""
        return self._container_name("astrbot")

    @property
    def napcat_container(self) -> str:
        """协议端容器名（`containers.napcat`）。"""
        return self._container_name("napcat")

    def _container_name(self, key: str) -> str:
        block = self.raw.get("containers")
        if not isinstance(block, dict):
            return key
        return str(block.get(key) or key)

    def resolve_targets(self, targets) -> list[str]:
        """把目标列表里的符号别名解析成真实容器名（去重、保序、丢空值）。

        `@self` -> `containers.astrbot`，`@napcat` -> `containers.napcat`；
        其它值按真实容器名原样保留，所以老配置里写死的名字继续可用。
        """
        mapping = {"@self": self.self_container, "@napcat": self.napcat_container}
        out: list[str] = []
        for item in targets or []:
            name = mapping.get(str(item).strip(), str(item).strip())
            if name and name not in out:
                out.append(name)
        return out

    @property
    def protected_containers(self) -> set[str]:
        """**额外**受保护的容器（本体容器不在此列，见 `is_protected()`）。"""
        safety = self.raw.get("safety")
        listed = safety.get("protected_containers") if isinstance(safety, dict) else None
        return {str(x).strip() for x in (listed or []) if str(x).strip()}

    @property
    def all_containers(self) -> list[str]:
        return list(dict.fromkeys([self.self_container, self.napcat_container]))

    @property
    def issues(self) -> list[str]:
        return list(self._issues)

    def is_self(self, target: str) -> bool:
        return target == self.self_container

    def is_protected(self, target: str) -> bool:
        """本体容器**永远**受保护，与 `protected_containers` 配置无关。

        这样即使用户改了容器名却忘了同步 `protected_containers`，本体也不会
        悄悄失去保护（B12 §容器名一致性）——把"靠配置正确"变成"结构上不可能错"。
        """
        return target == self.self_container or target in self.protected_containers

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
    def _migrate_allowed_containers(self, issues: list[str]) -> None:
        """把已退役的 `safety.allowed_containers` 合并进 `containers.managed`。

        两份列表的语义都是"允许被动作作用的容器名"，历史上 `action_allowlist`
        取二者并集，于是运维必须同时维护两个输入框、漏填一个就静默丢失白名单。
        现在只认 `containers.managed`：

        - 旧键有值 → 去重合并进 `containers.managed`，**不丢任何名字**；
        - 合并完删掉旧键，否则 WebUI 下次保存会把复选框/列表再写回来；
        - 记一条 issue，让"列表搬家了"这件事在日志里可见。
        """
        section, key = LEGACY_ALLOWED_CONTAINERS
        block = self.raw.get(section)
        if not isinstance(block, dict) or key not in block:
            return
        legacy = block.pop(key, None)
        names = [str(x).strip() for x in (legacy or []) if str(x).strip()]
        if not names:
            issues.append(
                "safety.allowed_containers 已退役并移除（与 containers.managed "
                "语义重复）；该键为空，无需迁移")
            return
        containers = self.raw.get("containers")
        if not isinstance(containers, dict):
            containers = {}
            self.raw["containers"] = containers
        merged = [x.strip() for x in (containers.get("managed") or [])
                  if str(x).strip()]
        added = [n for n in names if n not in merged]
        containers["managed"] = list(dict.fromkeys(merged + added))
        issues.append(
            "safety.allowed_containers 已退役并移除（与 containers.managed 语义"
            f"重复）；原值 {names} 已并入 containers.managed"
            + (f"（新增 {added}）" if added else "（无新增）"))

    def _sync_daily_restart_cron(self, sched: dict, issues: list[str]) -> None:
        """把前端友好的 `daily_restart_time` + `daily_restart_weekdays` 编译成 cron。

        前端不再要求用户写 5 字段 cron（那是本插件用不到的通用语法：定时重启
        只可能是"每周某几天 HH:MM"）。规则：

        - 时刻：`HH:MM`，非法则回退默认 04:00 并记 issue。
        - 星期：`mon`..`sun` 子集，空 = 每天；未知值剔除。
        - **兼容老配置**：只有当两个新字段都还不存在、而 `daily_restart_cron`
          是手写的非默认值时，才原样尊重它（避免升级把用户的 `30 3 * * 1`
          悄悄改回每天 4 点）。一旦新字段出现，就以新字段为准。
        """
        default_cron = DEFAULTS["schedule"]["daily_restart_cron"]
        has_time = "daily_restart_time" in self._provided_schedule_keys
        has_days = "daily_restart_weekdays" in self._provided_schedule_keys
        legacy_cron = str(sched.get("daily_restart_cron") or "").strip()
        # 注意判断依据是"用户**原始**配置里有没有这两个键"，不是 sched 里有没有——
        # sched 已被 DEFAULTS 补齐，新字段永远存在。
        if not has_time and not has_days and legacy_cron and legacy_cron != default_cron:
            issues.append(
                f"schedule.daily_restart_cron={legacy_cron!r} 沿用旧值；"
                "如需改用「每天几点 + 星期几」方式填写，在 WebUI 里改一次"
                "定时重启时间即可自动接管")
            return

        raw_time = str(sched.get("daily_restart_time") or "").strip()
        hour, minute = 4, 0
        if raw_time:
            parsed = _parse_hhmm(raw_time)
            if parsed is None:
                issues.append(
                    f"schedule.daily_restart_time={raw_time!r} 不是合法 HH:MM，回退 04:00")
            else:
                hour, minute = parsed
        sched["daily_restart_time"] = f"{hour:02d}:{minute:02d}"

        days_raw = sched.get("daily_restart_weekdays")
        days: list[str] = []
        if isinstance(days_raw, list):
            for item in days_raw:
                key = str(item).strip().lower()[:3]
                if key in _WEEKDAY_INDEX:
                    if key not in days:
                        days.append(key)
                elif str(item).strip():
                    issues.append(
                        f"schedule.daily_restart_weekdays 含未知星期 {item!r}，已忽略")
        if not days:
            days = list(_WEEKDAY_INDEX)
        if len(days) == 7:
            dow = "*"
        else:
            dow = ",".join(str(_WEEKDAY_INDEX[d]) for d in
                           sorted(days, key=lambda d: _WEEKDAY_INDEX[d]))
        sched["daily_restart_weekdays"] = [d for d in _WEEKDAY_INDEX if d in days]
        sched["daily_restart_cron"] = f"{minute} {hour} * * {dow}"

    def validate(self) -> list[str]:
        """校验并就地修正非法配置。返回问题列表（空表示全部合法）。"""
        issues: list[str] = []
        raw = self.raw

        # ---- 退役键 ---------------------------------------------------- #
        # `os_hint` 曾出现在前端配置面：环境改由 `env_platform` 探针从宿主机侧
        # 采集后，手填值只会与真实镜像基底冲突，误导诊断。
        # `dry_run` 曾是"整插件级空跑"开关：它把一条真实的短路逻辑塞进授权链，
        # 让"能不能执行"取决于一个可被误开误关的全局位。配置面退役后，
        # `dry_run` 只作为 `ActionSpec` / `Decision` 上的**逐次**标记存在
        # （动作级语义，由 `SafetyGuard` 当次判定给出），不再有全局开关。
        # 必须**删键**（而不是留着不用），否则 WebUI 下次保存会把它写回来；
        # 同时给出可见告警，避免"开关消失了但没人知道"。
        for key, note in RETIRED_KEYS:
            if key in raw:
                raw.pop(key, None)
                issues.append(f"{key} 已退役并移除（{note}）")

        # `safety.allowed_containers` 与 `containers.managed` 语义重复，前后端都
        # 只保留后者。迁移必须先于白名单校验：否则老部署升级后 `managed` 为空，
        # 会瞬间落进"动作白名单为空 → 所有自动重启被拒"的降级态。
        self._migrate_allowed_containers(issues)

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

        # B17 §5.3：历史值先迁移再校验，否则老配置会被"非法值回退"覆盖成默认值，
        # 用户看不到"其实是改名了"这一层信息。
        alias = RELOGIN_METHOD_ALIASES.get(str(qq.get("relogin_method") or "").strip())
        if alias:
            issues.append(
                f"qq.relogin_method=webui_quick_login 已更名 {alias}，自动迁移"
                "（原策略调用的 /api/QQLogin/QuickLogin 在 NapCat 4.18 上返回 404）")
            qq["relogin_method"] = alias
        if qq.get("relogin_method") not in VALID_RELOGIN_METHODS:
            issues.append(f"qq.relogin_method={qq.get('relogin_method')!r} 非法，回退 restart_container")
            qq["relogin_method"] = "restart_container"

        # B17 §5.2：账密登录凭据。账号非法一律清空（宁可退回"只重启"，也不拿
        # 一个畸形账号去反复试错锁号）。
        acc = str(qq.get("account") or "").strip()
        if acc and not (acc.isdigit() and 5 <= len(acc) <= 12):
            issues.append("qq.account 非纯数字或长度异常，已清空")
            acc = ""
        qq["account"] = acc

        pwd = str(qq.get("password") or "")
        qq["password"] = pwd
        qq["password_is_md5"] = bool(pwd) and _is_md5_hex(pwd)
        if pwd and not qq["password_is_md5"] and len(pwd) > 64:
            issues.append("qq.password 超过 64 字符，已清空")
            qq["password"] = ""
            qq["password_is_md5"] = False
        if pwd and acc and not qq["password"]:
            issues.append("qq.password 已清空，账密阶段将被跳过")

        public = str(qq.get("webui_public_base") or "").strip().rstrip("/")
        if public and not public.startswith(("http://", "https://")):
            issues.append("qq.webui_public_base 必须以 http(s):// 开头，已清空")
            public = ""
        qq["webui_public_base"] = public

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
        bounded(qq, "activity_evidence_sec", 1800, 0, 86400)

        # B17 §5.2：登录阶梯的时间参数用**钳制**而非"回退默认值"——
        # 用户把超时填大/填小是明确的意图，把它夹到边界比丢掉意图更符合预期
        # （C17 T-01：restart_grace_sec=-5 → 0，login_ready_timeout=99999 → 600）。
        def clamped(section, key, default, low, high, prefix="qq"):
            value = section.get(key, default)
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = None
            if parsed is None or not math.isfinite(parsed):
                issues.append(f"{prefix}.{key}={value!r} 非法，回退 {default}")
                section[key] = default
                return
            fixed = min(max(int(parsed), low), high)
            if fixed != int(parsed):
                issues.append(f"{prefix}.{key}={int(parsed)} 超出 [{low}, {high}]，已钳制为 {fixed}")
            section[key] = fixed

        clamped(qq, "restart_grace_sec", 3600, 0, 86400)
        clamped(qq, "login_ready_timeout", 90, 10, 600)
        clamped(qq, "login_retry_interval", 3, 1, 30)
        clamped(qq, "password_fail_hold_sec", 21600, 600, 604800)

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

        # 旧版本的 `protected_containers` 默认值是写死的 ["astrbot"]。用户改了本体容器名
        # 之后，那个字面量既不是本体、也不是任何真实容器，纯属历史残留 → 视为未配置。
        # 注意：这**不会**削弱本体的保护（`is_protected()` 已无条件保护本体）。
        if (list(safety.get("protected_containers") or []) == ["astrbot"]
                and self.self_container != "astrbot"):
            safety["protected_containers"] = []
            issues.append(
                "safety.protected_containers 仍是旧默认值 ['astrbot']，已视为空"
                f"（本体容器 {self.self_container} 始终受保护，无需在此重复）")

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
        # M5/D2-b：`direct_actions` 是否**真正生效**由 mode 决定（同进程内核直连
        # 重启 astrbot = 自杀 S1）。这里不做归一化——默认组合 inproc+true 是合法的
        # "尚未迁移"状态，不该刷 issue；生效判定放在动作层 `direct_actions_enabled()`。
        kern["legacy_data_dir"] = str(kern.get("legacy_data_dir") or "").strip()

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
            # `__template_key` 必须**原样带过去**。下面这个 dict 是白名单式重建，
            # 一旦不带上它，用户在 WebUI 里保存一次，磁盘上的等级就会全部变成
            # "找不到对应模板，请删除后重新添加"——数据还在，但前端再也渲染不出来。
            # 缺失时（老版本写下的配置）在这里补齐，等于自动修复，用户不必手工
            # 删条目重加。沿用条目自带的键，兼容以后新增模板类型。
            cleaned.append({
                "__template_key": str(
                    lv.get("__template_key") or lv.get("template") or LEVEL_TEMPLATE_KEY),
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
        # `@self` / `@napcat` 在这里解析成真实容器名；之后全流程只见真实名字。
        sleep_targets = self.resolve_targets(lc.get("sleep_targets"))
        bad = [t for t in sleep_targets if t not in allow]
        if bad:
            issues.append(
                "lifecycle.sleep_targets 含非白名单目标 "
                f"{bad}，已剔除（睡眠将仅作用于白名单内目标）")
            sleep_targets = [t for t in sleep_targets if t in allow]
        lc["sleep_targets"] = sleep_targets
        if not sleep_targets:
            issues.append("lifecycle.sleep_targets 为空，睡眠功能不可用（仅告警）")
        elif (self.is_self(self.self_container)
              and not safety.get("allow_self_restart")
              and self.self_container in sleep_targets):
            issues.append(
                f"sleep_targets 含本体容器 {self.self_container} 但 "
                "safety.allow_self_restart=false，睡眠时会自动跳过本体"
                "（仅睡眠其他目标）。如需本体参与睡眠，请开启 "
                "allow_self_restart 并部署看门狗。")

        up_targets = self.resolve_targets(lc.get("uptime_targets"))
        if not up_targets:
            up_targets = self.all_containers
            issues.append("lifecycle.uptime_targets 为空，回退为全部容器")
        lc["uptime_targets"] = up_targets

        # 定时重启目标同样支持 `@self` / `@napcat`（`schedule` 可能被用户写成非对象，
        # 这里只做防御性取值，不新增崩溃点——validate() 承诺永不抛异常）。
        sched = raw.get("schedule")
        if isinstance(sched, dict):
            sched["daily_restart_targets"] = self.resolve_targets(
                sched.get("daily_restart_targets"))
            self._sync_daily_restart_cron(sched, issues)

        if not allow:
            issues.append(
                "动作白名单为空（containers.managed 未填），"
                f"所有自动重启将被拒绝；本部署需要放行的容器是 {self.all_containers}，"
                "请把它们填入 containers.managed")
        else:
            # 白名单非空、但漏了本部署实际要守护的容器 —— 点名说清楚，别让用户
            # 只看到一条"某个动作被拒绝"的运行时日志（B12 §容器名一致性）。
            missing = [name for name in self.all_containers if name not in allow]
            if missing:
                issues.append(
                    f"动作白名单未包含容器 {missing}（本部署的容器名来自 "
                    "containers.astrbot / containers.napcat）；这些容器上的自动重启"
                    "与睡眠会被拒绝。如非有意，请把它们加入 containers.managed")

        # ---- circadian（作息窗口） ------------------------------------ #
        # 窗口本身**不是触发器**：它只回答"现在允不允许睡"，"该不该睡"仍由
        # 困意值（uptime → L0..L4）决定（B15 §6）。因此这里的所有兜底都偏向
        # "更不容易睡"——配置写错最多让它不睡，绝不能让它错睡。
        circ = raw.get("circadian")
        if not isinstance(circ, dict):
            circ = dict(DEFAULTS.get("circadian") or {})
            raw["circadian"] = circ
            issues.append("circadian 配置段非法，已回退默认")

        for key, default in (("asleep_start", "02:00"), ("asleep_end", "07:00")):
            value = circ.get(key, default)
            parsed = _parse_hhmm(value) if isinstance(value, str) else None
            if parsed is None:
                issues.append(
                    f"circadian.{key}={value!r} 不是合法 HH:MM，回退 {default}")
                circ[key] = default
            else:
                # 归一化成零填充的 `HH:MM`，运行时不必再处理 `2:0` 这类输入。
                circ[key] = f"{parsed[0]:02d}:{parsed[1]:02d}"

        # 顺延上限必须存在：没有它，持续的夜间对话会让本体的睡眠无限期推后，
        # 30 天一次的真睡保证就被聊穿了（施工计划 M4 / D1）。
        # 用**钳制**而非回退默认：填 999 的人想表达"尽量别睡"，夹到 72 比
        # 悄悄改回 12 更贴近意图。
        clamped(circ, "max_defer_hours", 12, 1, 72, prefix="circadian")
        clamped(circ, "quiet_minutes", 10, 0, 120, prefix="circadian")

        start_min = _hhmm_to_minutes(circ["asleep_start"])
        end_min = _hhmm_to_minutes(circ["asleep_end"])
        if start_min is not None and start_min == end_min:
            # 零长度窗口 = 关闭。这样用户想临时停用它只需把两端填成一样，
            # 不必再去动 enabled（也避免"enabled=true 但其实从不生效"的困惑）。
            circ["enabled"] = False
            issues.append(
                "circadian.asleep_start 与 asleep_end 相同，作息窗口长度为 0，"
                "已视为禁用（如需停用请直接把 circadian.enabled 设为 false）")
        elif start_min is not None and end_min is not None:
            length = (end_min - start_min) % (24 * 60)
            if length < 120:
                issues.append(
                    f"circadian 作息窗口仅 {length} 分钟（建议不少于 120 分钟）；"
                    "窗口过短时本体可能来不及睡完就被唤醒，睡眠时长会被 "
                    "lifecycle.sleep_duration_min 截断")

        # 窗口开着但睡眠功能整体关着 —— 用户大概率是想开却没发现少了一步。
        if circ.get("enabled") and not raw.get("lifecycle", {}).get("enabled"):
            issues.append(
                "circadian.enabled=true 但 lifecycle.enabled=false，"
                "作息窗口不会生效（困意值不运行就没有睡眠意图）")

        self._issues = issues
        self._d = _to_attr(self.raw)
        return issues


def build_config(raw: dict | None, data_dir: str = "") -> GuardianConfig:
    cfg = GuardianConfig(raw, data_dir=data_dir)
    cfg.validate()
    return cfg
