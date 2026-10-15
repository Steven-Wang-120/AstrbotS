"""Guardian 守护系统 —— 内核 IPC 协议层（B16 §6.2 / §6.3 / §12.3.4）。

换行分隔的 JSON-RPC 2.0：每行一条完整 JSON 消息，双向。

```jsonc
// 请求（适配层 → 内核）
{"jsonrpc":"2.0","id":7,"method":"health_report","params":{"scope":"all"},"auth":"<token>"}
// 响应（内核 → 适配层）
{"jsonrpc":"2.0","id":7,"result":{"text":"...","structured":{...}}}
// 错误
{"jsonrpc":"2.0","id":7,"error":{"code":"safety_denied","message":"目标不在白名单"}}
// 事件（内核 → 适配层，无 id）
{"jsonrpc":"2.0","method":"notify","params":{"text":"[睡眠] 即将执行...","severity":"warn"}}
```

本模块**纯 stdlib**，不得 `import astrbot`（I6），也不得 import 插件内任何
会拉起 docker/aiohttp 的重模块——协议层必须能被任何一端单独引用。
"""

from __future__ import annotations

import json
from typing import Any

JSONRPC = "2.0"
PROTOCOL_VERSION = "1.0"

# --------------------------------------------------------------------------- #
# 错误码（B16 §12.3.4 的 L1 表 + 业务错误）
# --------------------------------------------------------------------------- #
#: 内核不可用：socket 不存在 / ECONNREFUSED（根因 R1/R3/R4/R5）。**可自愈**（R4 除外）
E_KERNEL_UNAVAILABLE = "kernel_unavailable"
#: 连上但超时无响应（R2/R6）。**可自愈**
E_KERNEL_TIMEOUT = "kernel_timeout"
#: 有响应但显式拒绝（R6 过载）。可自愈
E_KERNEL_BUSY = "kernel_busy"
#: 有响应但内部错误（R6）
E_KERNEL_ERROR = "kernel_error"

#: 业务错误：安全层拒绝（**正常业务结果**，与 D4 可用性问题区分开）
E_SAFETY_DENIED = "safety_denied"
E_BAD_REQUEST = "bad_request"
E_UNKNOWN_METHOD = "unknown_method"
E_UNAUTHORIZED = "unauthorized"
E_INTERNAL = "internal"

#: 触发 D4-a+ 自修复阶梯的错误码（"连不上 / 没响应"）
SELF_HEAL_CODES = frozenset({E_KERNEL_UNAVAILABLE, E_KERNEL_TIMEOUT})

#: 适配层进入 kernel_degraded 后对外使用的错误码
E_KERNEL_DEGRADED = "kernel_degraded"

#: 内核 → 适配层事件名
EV_NOTIFY = "notify"
EV_KERNEL_READY = "kernel_ready"

#: 事件方法名集合（无 id 的单向消息）
EVENT_METHODS = frozenset({EV_NOTIFY, EV_KERNEL_READY})

#: L1 用户可见文案模板（B16 §12.3.4）。`{...}` 由调用方填充诊断线索。
USER_TEXT = {
    E_KERNEL_UNAVAILABLE: "守护内核不可用：IPC 通道未建立",
    E_KERNEL_TIMEOUT: "守护内核无响应，可能正在重启或被阻塞",
    E_KERNEL_BUSY: "守护内核繁忙，请稍后重试",
    E_KERNEL_ERROR: "守护内核内部错误",
    E_KERNEL_DEGRADED: "守护内核已进入降级状态（自修复额度已耗尽，需人工介入或 /guardian reset）",
}


class IpcError(Exception):
    """IPC 层的唯一异常类型。

    `code` 用上面的常量；`diagnostics` 携带**可诊断线索**（socket 路径、errno、
    最后心跳时间戳）——这是 B16 §12.3.1 对 R4（唯一不会自愈的根因）的硬要求：
    只丢一句"不可用"会让运维无法区分"内核死了在等看门狗"和"通道配错了"。
    """

    def __init__(self, code: str, message: str = "", *,
                 diagnostics: dict[str, Any] | None = None,
                 self_healable: bool | None = None) -> None:
        self.code = code
        self.message = message or USER_TEXT.get(code, code)
        self.diagnostics = dict(diagnostics or {})
        if self_healable is None:
            self_healable = code in SELF_HEAL_CODES
        self.self_healable = bool(self_healable)
        super().__init__(self.user_text())

    # ------------------------------------------------------------------ #
    def user_text(self) -> str:
        """L1 对话面文案：一句人话 + 括号里的诊断线索。"""
        base = USER_TEXT.get(self.code, self.message or self.code)
        if self.message and self.code in SELF_HEAL_CODES:
            base = self.message
        parts = [f"{k}={v}" for k, v in sorted(self.diagnostics.items())
                 if v not in (None, "")]
        if parts:
            return f"{base}（{', '.join(parts)}）"
        return base

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "diagnostics": self.diagnostics}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"IpcError({self.code!r}, {self.message!r})"


# --------------------------------------------------------------------------- #
# 消息构造
# --------------------------------------------------------------------------- #
def make_request(req_id: Any, method: str, params: dict | None = None,
                 auth: str = "") -> dict[str, Any]:
    # 刻意**不**做 `dict(params or {})` 强转：调用方传了非映射（list/str/数字）时，
    # 强转会在这里抛 ValueError，把一个"参数形状错误"变成客户端崩溃，
    # 而正确行为是让**服务端**用 bad_request 明确拒绝。
    msg: dict[str, Any] = {"jsonrpc": JSONRPC, "id": req_id, "method": method,
                           "params": {} if params is None else params}
    if auth:
        msg["auth"] = auth
    return msg


def make_response(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "id": req_id, "result": result}


def make_error(req_id: Any, code: str, message: str = "",
               diagnostics: dict | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message or code}
    if diagnostics:
        err["diagnostics"] = dict(diagnostics)
    return {"jsonrpc": JSONRPC, "id": req_id, "error": err}


def make_event(method: str, params: dict | None = None) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "method": method, "params": dict(params or {})}


# --------------------------------------------------------------------------- #
# 编解码
# --------------------------------------------------------------------------- #
def encode(msg: dict[str, Any]) -> bytes:
    """编码为一行（含换行）。`ensure_ascii=False` 保留中文，UTF-8 传输。"""
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
            + "\n").encode("utf-8")


def decode_line(line: bytes | str) -> dict[str, Any]:
    """解码一行。非法行抛 `IpcError(bad_request)`，绝不返回半成品。"""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IpcError(E_BAD_REQUEST, f"非法 UTF-8 分帧: {exc}") from exc
    line = line.strip()
    if not line:
        raise IpcError(E_BAD_REQUEST, "空消息行")
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as exc:
        raise IpcError(E_BAD_REQUEST, f"非法 JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise IpcError(E_BAD_REQUEST, "消息必须是 JSON 对象")
    return msg


class LineDecoder:
    """增量分帧器：喂字节，吐出完整消息。

    不用 `readline()`：asyncio 的 `StreamReader.readline()` 有 64KiB 上限，
    超长行会抛 `LimitOverrunError`/`ValueError` 并**污染缓冲区**，之后所有消息
    都错位。这里自己管缓冲，并给单行设硬上限（超限直接判 bad_request，
    避免恶意/损坏的一端把内存打爆）。
    """

    __slots__ = ("_buf", "_max")

    def __init__(self, max_line: int = 4 * 1024 * 1024) -> None:
        self._buf = bytearray()
        self._max = max_line

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if chunk:
            self._buf.extend(chunk)
        out: list[dict[str, Any]] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx])
            del self._buf[:idx + 1]
            if not raw.strip():
                continue          # 容忍空行（心跳/分隔）
            out.append(decode_line(raw))
        if len(self._buf) > self._max:
            self._buf.clear()
            raise IpcError(E_BAD_REQUEST,
                           f"单行消息超过上限 {self._max} 字节，已丢弃缓冲")
        return out

    def pending(self) -> int:
        return len(self._buf)
