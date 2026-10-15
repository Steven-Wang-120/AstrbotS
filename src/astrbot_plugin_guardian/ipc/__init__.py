"""Guardian 守护系统 —— 内核 IPC 子包（B16 §6）。

三块内容：

- `protocol`：JSON-RPC 2.0 换行分帧、错误码、编解码（纯 stdlib）
- `methods`：内核方法分发表——**本地后端与 IPC 服务端共用同一张表**，
  因此 `kernel.mode=inproc` 与 `kernel.mode=ipc` 的行为差异只能是"传输"，
  不可能是"逻辑"。
- `server` / `client`：内核服务端与适配层客户端（含 D4-a+ 有界自修复阶梯）

**本子包及其依赖不得 `import astrbot`**（不变量 I6）。
"""

from __future__ import annotations

from .protocol import (  # noqa: F401
    E_BAD_REQUEST,
    E_INTERNAL,
    E_KERNEL_BUSY,
    E_KERNEL_ERROR,
    E_KERNEL_TIMEOUT,
    E_KERNEL_UNAVAILABLE,
    E_SAFETY_DENIED,
    E_UNAUTHORIZED,
    E_UNKNOWN_METHOD,
    IpcError,
    LineDecoder,
    make_error,
    make_event,
    make_request,
    make_response,
)

__all__ = [
    "IpcError",
    "LineDecoder",
    "make_request",
    "make_response",
    "make_error",
    "make_event",
    "E_KERNEL_UNAVAILABLE",
    "E_KERNEL_TIMEOUT",
    "E_KERNEL_BUSY",
    "E_KERNEL_ERROR",
    "E_SAFETY_DENIED",
    "E_BAD_REQUEST",
    "E_UNKNOWN_METHOD",
    "E_UNAUTHORIZED",
    "E_INTERNAL",
]
