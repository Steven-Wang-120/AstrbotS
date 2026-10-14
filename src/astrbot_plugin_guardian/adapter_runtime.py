"""Guardian 守护系统 —— AstrBot 适配层运行时（域 B）。

对应文档：B16 §5.2/§5.3、B16 §6.3

职责边界（**不变量 I2**）：
- 只做"对接 AstrBot 面"：解析数据目录、把通知文本包装成消息链发送、向已运行的
  反向 WebSocket 发只读 OneBot 查询、把 LLM 工具/命令的调用转交内核。
- **无状态、无策略、无 docker 权限**，不跑任何巡检。

业务逻辑一律在 `kernel_runtime.py`（域 A）。本模块是 AstrBot 与内核之间唯一的
"脏"边界；除 `main.py` 与本文件外，插件内任何模块都不得 `import astrbot`。
"""

from __future__ import annotations

import os
from typing import Any

from .kernel_runtime import PLUGIN_NAME, GuardianKernel, default_data_dir  # noqa: F401

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
    # 3) 最后的兜底：内核自有的默认数据目录（仅开发态可用）
    return default_data_dir()


def _forward(name: str) -> property:
    """生成一对"读写都落到内核"的转发属性。

    必须可写：拆分前的 `GuardianRuntime` 是普通对象，测试与诊断脚本会直接
    `rt.state = fake` / `rt.lifecycle_policy = build_policy(...)` 来替换依赖。
    只读属性会让这些既有用法抛 `AttributeError: ... has no setter`，
    那就不算"对外表面不变"。
    """
    def getter(self):
        return getattr(self.kernel, name)

    def setter(self, value):
        setattr(self.kernel, name, value)

    return property(getter, setter)


class GuardianAdapter:
    """域 B 装配：把 AstrBot 的能力注入内核，并把业务属性透传给调用方。

    对外表面与拆分前的 `GuardianRuntime` **完全一致**（config / state / engine /
    audit / lifecycle_policy / status_text() / request_sleep() …），因此 `main.py`
    与 `tools/llm_tools.py` 无需改动。
    """

    def __init__(self, context, raw_config: dict | None) -> None:
        self.context = context
        self.data_dir = resolve_data_dir()
        self.kernel = GuardianKernel(
            raw_config,
            data_dir=self.data_dir,
            sender=self._send,
            action_caller=self._call_onebot,
        )

    # ---- 业务属性透传到内核（显式列出，避免 __getattr__ 的隐式与递归风险）---- #
    config = _forward("config")
    state = _forward("state")
    audit = _forward("audit")
    bus = _forward("bus")
    engine = _forward("engine")
    safety = _forward("safety")
    policy = _forward("policy")
    lifecycle_policy = _forward("lifecycle_policy")
    notifier = _forward("notifier")
    scheduler = _forward("scheduler")
    docker = _forward("docker")
    napcat = _forward("napcat")
    ros2 = _forward("ros2")
    deps = _forward("deps")

    async def start(self) -> None:
        await self.kernel.start()

    async def stop(self) -> None:
        await self.kernel.stop()

    async def run_check(self, scope: str = "all") -> dict[str, Any]:
        return await self.kernel.run_check(scope)

    async def request_restart(self, container: str, **kw) -> tuple[bool, str]:
        return await self.kernel.request_restart(container, **kw)

    async def request_start(self, container: str, **kw) -> tuple[bool, str]:
        return await self.kernel.request_start(container, **kw)

    async def request_sleep(self, reason: str, duration_min: int = 0,
                            **kw) -> tuple[bool, str]:
        return await self.kernel.request_sleep(reason, duration_min, **kw)

    async def set_mode(self, mode: str) -> tuple[bool, str]:
        return await self.kernel.set_mode(mode)

    async def set_profile(self, profile: str) -> tuple[bool, str]:
        return await self.kernel.set_profile(profile)

    def status_text(self) -> str:
        return self.kernel.status_text()

    def lifecycle_text(self) -> str:
        return self.kernel.lifecycle_text()

    def health_report(self, scope: str = "all") -> str:
        return self.kernel.health_report(scope)

    def incidents_text(self, limit: int = 10) -> str:
        return self.kernel.incidents_text(limit)

    def diagnose(self, target: str) -> str:
        return self.kernel.diagnose(target)

    def allowlist(self) -> list[str]:
        return self.kernel.allowlist()

    def safety_warnings(self) -> list[str]:
        return self.kernel.safety_warnings()

    def jobs_text(self) -> str:
        return self.kernel.jobs_text()

    def note_event(self) -> None:
        self.kernel.note_event()

    def __getattr__(self, item: str):
        """兜底透传：未显式声明的属性一律落到内核。

        拆分前 `main.py` 与测试直接读 `runtime._started` / `runtime.deps` 之类的
        内部属性；显式属性列表只能覆盖常用面，兜底可以保证"拆分不改变对外表面"。
        这里绝不能写 `self.kernel`（那会在内核尚未赋值时无限递归），
        必须走 `self.__dict__.get`。
        """
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        kernel = self.__dict__.get("kernel")
        if kernel is None:
            raise AttributeError(item)
        return getattr(kernel, item)

    # ================================================================== #
    # 注入给内核的两项能力
    # ================================================================== #
    async def _send(self, umo: str, text: str) -> None:
        await self.context.send_message(umo, build_chain(text))

    async def _call_onebot(self, action: str) -> dict:
        """通过真实 Context 获取已运行的 OneBot 反向 WS，绝不另起协议服务。

        多适配器时必须指定 platform_id，不能猜测账号。每个实例可通过
        qq.self_id 显式路由多个反向连接；默认单账号由 aiocqhttp 处理。
        """
        from .utils.napcat import NapCatUnavailable
        if action not in {"get_status", "get_login_info"}:
            raise NapCatUnavailable("Guardian 仅允许只读登录态查询")
        cfg = self.kernel.config
        platform_id = str(cfg.qq.platform_id or "").strip()
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
        if cfg.qq.self_id:
            params["self_id"] = str(cfg.qq.self_id)
        return await client.call_action(action, **params)


# 兼容别名：`main.py` 通过 runtime.py 引用 GuardianRuntime，这里保留同名导出。
GuardianRuntime = GuardianAdapter
