"""Guardian 守护系统 —— 内核访问后端抽象（B16 §9 M2 的"回退开关"）。

适配层不直接摸内核，而是对着一个后端说话：

- `LocalBackend`（`kernel.mode=inproc`）：**同进程**直接调用内核。这是 M1 的旧形态，
  保留它是 M2 的**回滚路径**（I5 可回滚）。
- `IpcBackend`（`kernel.mode=ipc`）：走 Unix socket / TCP。

两者共用 `ipc/methods.build_handlers` 的**同一张方法表**，所以 inproc → ipc 的
切换只改变"传输"，不改变"逻辑"。契约测试正是靠这一点，对同一 method 在两个
后端上断言同一结果。

约束：不得 `import astrbot`（I6）。
"""

from __future__ import annotations

from typing import Any

from .client import KernelIpcClient
from .methods import build_handlers
from .protocol import E_UNKNOWN_METHOD, IpcError


class LocalBackend:
    """同进程后端：把方法表直接接到内核实例上。"""

    mode = "inproc"

    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self._handlers = build_handlers(kernel)
        self._started = False

    @property
    def connected(self) -> bool:
        return self._started

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def call(self, method: str, params: dict | None = None, **_: Any) -> Any:
        handler = self._handlers.get(method)
        if handler is None:
            raise IpcError(E_UNKNOWN_METHOD, f"未知 method: {method}")
        return await handler(dict(params or {}))

    # 与 IpcBackend 对齐的运维面（本地模式下没有"自愈"可言）
    def reset(self) -> None:
        return None

    def note_healthy(self) -> None:
        return None

    def diagnostics(self, errno: Any = None) -> dict[str, Any]:
        return {"endpoint": "inproc", "errno": errno, "last_heartbeat": "(inproc)"}

    def health_dict(self) -> dict[str, Any]:
        return {"mode": "inproc"}


class IpcBackend:
    """跨进程后端：包装 `KernelIpcClient`，对外只暴露一个 `call`。"""

    mode = "ipc"

    def __init__(self, client: KernelIpcClient) -> None:
        self.client = client

    @property
    def connected(self) -> bool:
        return self.client.connected

    async def start(self) -> None:
        await self.client.connect()

    async def stop(self) -> None:
        await self.client.close()

    async def call(self, method: str, params: dict | None = None, **kw: Any) -> Any:
        result = await self.client.call_with_recovery(method, params, **kw)
        self.client.note_healthy()
        return result

    def reset(self) -> None:
        self.client.reset()

    def note_healthy(self) -> None:
        self.client.note_healthy()

    def diagnostics(self, errno: Any = None) -> dict[str, Any]:
        return self.client.diagnostics(errno)

    def health_dict(self) -> dict[str, Any]:
        return {"mode": "ipc", "endpoint": self.client.endpoint,
                **self.client.health.as_dict()}

    def should_notify_outage(self) -> bool:
        return self.client.should_notify_outage()

    def audit_event(self, kind: str, detail: str = "", **extra: Any) -> None:
        self.client.audit_event(kind, detail, **extra)

    def on_event(self, cb) -> None:
        self.client.on_event(cb)

    def on_state(self, cb) -> None:
        self.client.on_state(cb)


__all__ = ["IpcBackend", "LocalBackend"]
