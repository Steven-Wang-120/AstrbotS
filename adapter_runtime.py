"""Guardian 守护系统 —— AstrBot 适配层运行时（域 B）。

对应文档：B16 §5.2/§5.3、§6.3、§12.3.3

职责边界（**不变量 I2**）：
- 只做"对接 AstrBot 面"：解析数据目录、把通知文本包装成消息链发送、向已运行的
  反向 WebSocket 发只读 OneBot 查询、把 LLM 工具/命令的调用转交内核。
- **无状态、无策略、无 docker 权限**，不跑任何巡检。

本模块通过 `kernel.mode` 选择内核访问后端（`ipc/backend.py`）：

- `inproc`（默认）：同进程直连内核 —— M1 旧形态，也是 M2 的**回滚路径**（I5）；
- `ipc`：走 Unix socket / TCP 到独立内核进程。

两种模式下**对外方法签名完全一致**，因此 `main.py` 与 `tools/llm_tools.py`
不需要知道内核跑在哪里。业务逻辑一律在 `kernel_runtime.py`（域 A）。

除 `main.py` 与本文件外，插件内任何模块都不得 `import astrbot`。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any

from .core.config import build_config
from .core.log import logger
from .ipc.backend import IpcBackend, LocalBackend
from .ipc.client import KernelIpcClient
from .ipc.protocol import EV_NOTIFY, IpcError
from .kernel_runtime import PLUGIN_NAME, GuardianKernel, default_data_dir

#: `note_event` 的合并窗口：每条用户消息都发一次 IPC 太贵，10s 内合并成一次。
NOTE_EVENT_COALESCE_SEC = 10.0

#: L4 掉线告警的轮询间隔
OUTAGE_POLL_SEC = 15.0

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
    只读属性会让这些既有用法抛 `AttributeError: ... has no setter`。

    `kernel.mode=ipc` 时 `self.kernel` 为 None，此时访问这些属性会给出**可读的
    边界错误**而不是 `AttributeError: 'NoneType'`——因为这通常意味着有代码
    越过了 I2 边界去直接读内核业务状态。
    """
    def getter(self):
        kernel = self.__dict__.get("kernel")
        if kernel is None:
            raise AttributeError(
                f"适配层在 kernel.mode=ipc 下不持有内核业务状态（{name}）。"
                "请改用适配层的 async 方法（如 await rt.status_text()），"
                "不要在适配层直接读内核状态（B16 不变量 I2）。")
        return getattr(kernel, name)

    def setter(self, value):
        kernel = self.__dict__.get("kernel")
        if kernel is None:
            raise AttributeError(
                f"kernel.mode=ipc 下无法替换内核属性 {name}（I2）")
        setattr(kernel, name, value)

    return property(getter, setter)


class GuardianAdapter:
    """域 B 装配：把 AstrBot 的能力注入内核，并把业务调用转交内核。

    `inproc` 模式下对外表面与拆分前的 `GuardianRuntime` **完全一致**
    （config / state / engine / audit / lifecycle_policy / …），
    因此既有测试与诊断脚本无需改动。
    """

    def __init__(self, context, raw_config: dict | None) -> None:
        self.context = context
        self.raw_config = dict(raw_config or {})
        self.data_dir = resolve_data_dir()

        # 只为"决定用哪个后端"而构建一次配置视图；内核实例自己会再构建一份。
        self._cfg_view = build_config(self.raw_config, data_dir=self.data_dir)
        kc = self._cfg_view.kernel
        self.kernel_mode = str(kc.mode or "inproc")
        self.shared_dir = str(kc.shared_dir or "") or self.data_dir

        self.kernel: GuardianKernel | None = None
        self.client: KernelIpcClient | None = None
        self._backend: LocalBackend | IpcBackend
        self._outage_task: asyncio.Task | None = None
        self._last_note_ts = 0.0

        if self.kernel_mode == "ipc":
            self._backend = IpcBackend(self._build_client(kc))
        else:
            self.kernel = GuardianKernel(
                self.raw_config,
                data_dir=self.data_dir,
                sender=self._send,
                action_caller=self._call_onebot,
            )
            self._backend = LocalBackend(self.kernel)

    # ------------------------------------------------------------------ #
    def _build_client(self, kc) -> KernelIpcClient:
        client = KernelIpcClient(
            transport=str(kc.transport or "unix"),
            socket_path=str(kc.socket_path or ""),
            tcp_host=str(kc.tcp_host or "127.0.0.1"),
            tcp_port=int(kc.tcp_port or 6198),
            token_file=str(kc.token_file or ""),
            shared_dir=self.shared_dir,
            request_timeout=float(kc.request_timeout or 10),
            max_auto_reloads=int(kc.max_auto_reloads or 0),
            reload_cooldown=float(kc.reload_cooldown or 0),
            reload_grace_sec=float(kc.reload_grace_sec or 0),
            reload_reset_sec=float(kc.reload_reset_sec or 0),
            reload_request_path=str(kc.reload_request_path or ""),
            health_path=str(kc.health_path or ""),
            adapter_events_path=str(kc.adapter_events_path or ""),
            unreachable_notify_after=float(kc.unreachable_notify_after or 0),
        )
        client.on_event(self._on_kernel_event)
        client.on_state(self._on_kernel_state)
        self.client = client
        return client

    # ---- 业务属性透传到内核（仅 inproc 模式可用；ipc 模式访问即报错）---- #
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

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    async def start(self) -> None:
        if self.kernel_mode == "ipc":
            try:
                await self._backend.start()
                logger.info(f"[guardian] 适配层已连接内核 {self._endpoint}")
            except IpcError as exc:
                # 起不来不能把插件启动搞崩：工具面会在每次调用时给出显式错误（I4）
                logger.error(f"[guardian] 适配层连接内核失败：{exc.user_text()}")
            self._start_outage_watch()
            return
        assert self.kernel is not None
        await self.kernel.start()

    async def stop(self) -> None:
        if self._outage_task is not None:
            self._outage_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._outage_task
            self._outage_task = None
        if self.kernel_mode == "ipc":
            await self._backend.stop()
            return
        assert self.kernel is not None
        await self.kernel.stop()

    @property
    def _endpoint(self) -> str:
        backend = self._backend
        if isinstance(backend, IpcBackend):
            return backend.client.endpoint
        return "inproc"

    # ================================================================== #
    # 统一调用入口（两种后端共用）
    # ================================================================== #
    async def _call(self, method: str, params: dict | None = None,
                    *, timeout: float | None = None) -> Any:
        return await self._backend.call(method, params, timeout=timeout)

    # ================================================================== #
    # 有副作用能力
    # ================================================================== #
    async def run_check(self, scope: str = "all") -> dict[str, Any]:
        return await self._call("run_check", {"scope": scope})

    #: 容器停止/启动是**秒级**动作（`docker stop -t N` 的 HTTP 往返 ≥ N 秒），
    #: 默认 10s 请求超时会把它误判成"内核无响应"。给足余量。
    CONTAINER_ACTION_TIMEOUT = 90.0

    async def request_restart(self, container: str, **kw) -> tuple[bool, str]:
        return _unpair(await self._call(
            "restart_container", {"container": container, **kw},
            timeout=self.CONTAINER_ACTION_TIMEOUT))

    async def request_start(self, container: str, **kw) -> tuple[bool, str]:
        return _unpair(await self._call(
            "start_container", {"container": container, **kw},
            timeout=self.CONTAINER_ACTION_TIMEOUT))

    #: 登录恢复阶梯是**分钟级**长任务：有条件重启(≤15s) + 就绪等待(≤90s)
    #: + 账密登录(≤30s) + 三轮复核(≈9s)。用默认 10s 超时必然把"正在登录"
    #: 报成"内核无响应"（同 request_sleep 的教训）。
    QQ_LOGIN_TIMEOUT = 240.0

    async def request_qq_login(self, reason: str = "") -> tuple[bool, str]:
        """透传给内核执行一次登录恢复阶梯（B17 §8.3）。"""
        return _unpair(await self._call(
            "request_qq_login",
            {"reason": reason, "actor": "command", "trigger": "command"},
            timeout=self.QQ_LOGIN_TIMEOUT))

    async def qq_login_status(self) -> dict[str, Any]:
        """登录状态页数据（只读，经内核取，适配层不自己读 state）。"""
        return dict(await self._call("qq_login_status") or {})

    def webui_links(self) -> str:
        """NapCat WebUI 与登录状态页的直达链接（B17 §9.3）。

        ⚠️ token 会出现在命令回执里 = 出现在 QQ 聊天记录里。这是用户显式请求
        `/guardian webui` 的行为；审计里只记 `kind=qq.webui_link`，**不记该文本**。
        """
        cfg = self._cfg_view.qq
        public = str(cfg.webui_public_base or "").rstrip("/")
        token = str(cfg.webui_token or "")
        if not public:
            return ("未配置 qq.webui_public_base，无法生成浏览器侧链接。"
                    "（容器内地址 qq.webui_base 只对插件自身可用）")
        lines = [f"NapCat WebUI：{public}/webui?token={token}" if token else
                 f"NapCat WebUI：{public}/webui"]
        lines.append("登录状态页：<AstrBot 地址>/api/plug/astrbot_plugin_guardian/login")
        lines.append("（登录状态页走 dashboard 鉴权，需要在浏览器里已登录 AstrBot WebUI）")
        return "\n".join(lines)

    async def request_sleep(self, reason: str, duration_min: int = 0,
                            **kw) -> tuple[bool, str]:
        # 真睡眠是**分钟级**长任务：内核会一直阻塞到 stop → 等待 → start →
        # 复核 `StartedAt` 全部完成，所以一次 IPC 往返 ≈ duration_min*60 秒。
        # 用默认 10s 请求超时必然超时，会把"睡眠成功"报成"内核无响应"
        # （2026-10-16 实机 C16-K3）。按睡眠时长放大，另加 60s 余量给
        # 停止/启动/复核本身。
        wait = max(0, int(duration_min)) * 60 + 60
        return _unpair(await self._call(
            "request_sleep",
            {"reason": reason, "duration_min": duration_min, **kw},
            timeout=wait))

    async def set_mode(self, mode: str) -> tuple[bool, str]:
        return _unpair(await self._call("set_mode", {"mode": mode}))

    async def set_profile(self, profile: str) -> tuple[bool, str]:
        return _unpair(await self._call("set_profile", {"profile": profile}))

    async def note_event(self) -> None:
        """记录一次用户消息活跃度（供 QQ 证据链）。

        合并窗口：`observe_message` 对**每条**消息都会调用，若每条都发一次 IPC
        往返，成本会随聊天频率线性上升。这里 10s 内合并成一次；窗口外的第一条
        立即发送，保证"最近活跃时间"不会被拖太久。
        """
        now = time.time()
        if now - self._last_note_ts < NOTE_EVENT_COALESCE_SEC:
            return
        self._last_note_ts = now
        with contextlib.suppress(IpcError):
            await self._call("note_event", {})

    async def audit_append(self, *, kind: str, target: str = "",
                           detail: str = "", ok: bool = True,
                           actor: str = "llm", trigger: str = "llm") -> None:
        """适配层代内核记审计（I1：审计仍是内核单写者）。"""
        if self.kernel_mode != "ipc":
            assert self.kernel is not None
            self.kernel.audit_append(kind=kind, target=target, detail=detail,
                                     ok=ok, actor=actor, trigger=trigger)
            return
        with contextlib.suppress(IpcError):
            await self._call("audit_append", {
                "kind": kind, "target": target, "detail": detail, "ok": ok,
                "actor": actor, "trigger": trigger})

    async def reset_kernel_health(self) -> str:
        """`/guardian reset`：清零内核自修复额度。"""
        backend = self._backend
        if isinstance(backend, IpcBackend):
            backend.reset()
            return "已复位内核自修复额度（kernel_health.json）。"
        return "当前为 inproc 模式，无自修复额度需要复位。"

    # ================================================================== #
    # 只读查询
    # ================================================================== #
    async def status_text(self) -> str:
        return str(await self._call("status_text"))

    async def lifecycle_text(self) -> str:
        return str(await self._call("lifecycle_text"))

    async def env_info(self) -> dict[str, Any]:
        """运行环境指纹（系统 / 内核 / 架构 / 内存 / 时区），经内核取。

        前端不再有 `os_hint` 输入框：环境由宿主机侧采集，适配层只展示。
        """
        return dict(await self._call("env_info") or {})

    async def lifecycle_status_text(self) -> str:
        return str(await self._call("lifecycle_status"))

    async def health_report(self, scope: str = "all") -> str:
        return str(await self._call("health_report", {"scope": scope}))

    async def incidents_text(self, limit: int = 10) -> str:
        return str(await self._call("list_incidents", {"limit": limit}))

    async def diagnose(self, target: str = "all") -> str:
        return str(await self._call("diagnose", {"target": target}))

    async def allowlist(self) -> list[str]:
        return [str(x) for x in (await self._call("allowlist")) or []]

    async def safety_warnings(self) -> list[str]:
        return [str(x) for x in (await self._call("safety_warnings")) or []]

    async def jobs_text(self) -> str:
        return str(await self._call("jobs_text"))

    async def status_context(self) -> str:
        return str(await self._call("status_context"))

    async def llm_injection(self) -> dict[str, Any]:
        return dict(await self._call("injection") or {})

    async def runtime_flags(self) -> dict[str, Any]:
        return dict(await self._call("runtime_flags") or {})

    async def sleep_armed(self) -> bool:
        return bool(await self._call("sleep_armed"))

    # ================================================================== #
    # IPC 事件 / 状态
    # ================================================================== #
    async def _on_kernel_event(self, method: str, params: dict) -> None:
        if method != EV_NOTIFY:
            return
        umo = str(params.get("umo") or "")
        text = str(params.get("text") or "")
        if not umo or not text:
            return
        try:
            await self.context.send_message(umo, build_chain(text))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] 内核通知投递失败: {exc}")

    async def _on_kernel_state(self, state: str, extra: dict) -> None:
        if state == "disconnected":
            logger.warning("[guardian] 与内核的 IPC 连接断开，开始自修复")
        elif state == "reconnected":
            logger.info(f"[guardian] 与内核的 IPC 连接已恢复（stage="
                        f"{extra.get('stage')}）")
        elif state == "degraded":
            logger.error(f"[guardian] 内核进入降级状态：{extra.get('reason')}")

    def _start_outage_watch(self) -> None:
        """L4：内核掉线超过阈值时，由**适配层**主动推唯一一条"内核不可用"告警。

        这条告警只能由适配层产生——内核就是挂掉的那个，它无法产生"我挂了"
        （B16 §12.3.3 的递归陷阱）。
        """
        if self._outage_task is not None:
            return
        self._outage_task = asyncio.create_task(self._outage_loop())

    async def _outage_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(OUTAGE_POLL_SEC)
                backend = self._backend
                if not isinstance(backend, IpcBackend):
                    continue
                if not backend.should_notify_outage():
                    continue
                await self._notify_kernel_unavailable(backend)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[guardian] 掉线巡检异常: {exc}")

    async def _notify_kernel_unavailable(self, backend: IpcBackend) -> None:
        exc = backend.client._last_error or IpcError(
            "kernel_unavailable", "IPC 未连接",
            diagnostics=backend.diagnostics())
        text = f"[守护] {exc.user_text()}\n请检查内核容器状态；若持续失败需人工介入。"
        umos = list(getattr(self._cfg_view.notify, "umo_list", []) or [])
        sent = 0
        for umo in umos:
            try:
                await self.context.send_message(umo, build_chain(text))
                sent += 1
            except Exception as exc2:  # noqa: BLE001
                logger.warning(f"[guardian] 内核不可用告警投递失败: {exc2}")
        backend.audit_event("kernel_unavailable_notified",
                            exc.user_text(), sent=sent)
        logger.error(f"[guardian] 已推送内核不可用告警（{sent} 个目标）："
                     f"{exc.user_text()}")

    # ================================================================== #
    # 兜底透传（仅 inproc 模式）
    # ================================================================== #
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
            raise AttributeError(
                f"kernel.mode=ipc 下适配层不持有内核对象，无法访问 {item}（I2）")
        return getattr(kernel, item)

    # ================================================================== #
    # 注入给内核的两项能力（仅 inproc 模式需要）
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
        cfg = self._cfg_view
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


def _unpair(result: Any) -> tuple[bool, str]:
    """把 IPC 的 `{"ok":…, "detail":…}` 还原成内核的 `(ok, detail)` 元组。"""
    if isinstance(result, dict):
        return bool(result.get("ok")), str(result.get("detail") or "")
    if isinstance(result, (list, tuple)) and len(result) == 2:
        return bool(result[0]), str(result[1])
    return bool(result), ""


# 兼容别名：`main.py` 通过 runtime.py 引用 GuardianRuntime，这里保留同名导出。
GuardianRuntime = GuardianAdapter
