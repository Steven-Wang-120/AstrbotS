"""Guardian 守护系统 —— 适配层 IPC 客户端 + D4-a+ 有界自修复阶梯（B16 §6.4 / §12.3.5）。

本模块是**域 B 唯一的"主动"逻辑**，而且刻意只做三件事：

1. 连接 / 重连 / 请求-响应 / 接收事件；
2. 失败时走**有界**自修复阶梯（静默重连 → 请求一次内核重载 → 宽限期 → 显式失败）；
3. 写**它自己的**两份文件：`kernel_health.json`（运维计数）与
   `adapter_events.jsonl`（断连时间线）。

**它不做**：不跑巡检、不做安全判定、不发动作、不写 `state.json` / `audit.jsonl`
（不变量 I1/I2 —— 后两者是内核单写者的领地，内核挂了适配层也写不了）。

四条硬约束（C1–C4）是本模块的核心不变量，任何改动都必须让
`tests/test_kernel_ipc.py::test_self_heal_ladder_is_bounded` 仍然通过。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import secrets
import time
from typing import Any, Awaitable, Callable

from ..core.log import logger
from .methods import MUTATING_METHODS
from .protocol import (
    E_INTERNAL,
    E_KERNEL_DEGRADED,
    E_KERNEL_TIMEOUT,
    E_KERNEL_UNAVAILABLE,
    E_UNAUTHORIZED,
    IpcError,
    LineDecoder,
    encode,
    make_request,
)
from .server import unix_socket_supported

#: Stage 1 静默重连的退避序列（合计 ~15.5s，B16 §12.3.5）
RECONNECT_BACKOFF = (0.5, 1.0, 2.0, 4.0, 8.0)

#: 会产生副作用的 method：超时后**绝不重试**（B16 §6.4）。
#: 直接引用 `methods.MUTATING_METHODS` 而不是另抄一份——两份清单必然漂移，
#: 而漂移的后果是"某个破坏性动作被静默重试一次"。
NO_RETRY_METHODS = MUTATING_METHODS

EventCallback = Callable[[str, dict], Any]
StateCallback = Callable[[str, dict], Any]


def _iso(ts: float | None) -> str:
    if not ts:
        return "(未知)"
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (ValueError, OSError, OverflowError):  # pragma: no cover
        return "(未知)"


class KernelHealth:
    """`kernel_health.json` —— **适配层单写者**的受限运维计数文件。

    B16 §12.3.5 明确修正了不变量 I2：适配层不写**内核业务状态**，但内核挂了
    它就写不了，所以"自修复计数"只能由还活着的适配层维护。字段固定：
    `attempt` / `last_reload_ts` / `last_heartbeat_seen`（+ 降级与稳定计时）。
    """

    FIELDS = ("attempt", "last_reload_ts", "last_heartbeat_seen",
              "degraded", "degraded_since", "stable_since")

    def __init__(self, path: str) -> None:
        self.path = path
        self._data: dict[str, Any] = {k: None for k in self.FIELDS}
        self._data.update({"attempt": 0, "last_reload_ts": 0.0,
                           "last_heartbeat_seen": 0.0, "degraded": False,
                           "degraded_since": 0.0, "stable_since": 0.0})
        self.load()

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                for key in self.FIELDS:
                    if key in raw:
                        self._data[key] = raw[key]
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({k: self._data.get(k) for k in self.FIELDS},
                          fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:  # pragma: no cover - 磁盘异常
            logger.warning(f"[guardian] 写入 kernel_health.json 失败: {exc}")

    # ------------------------------------------------------------------ #
    @property
    def attempt(self) -> int:
        return int(self._data.get("attempt") or 0)

    @property
    def degraded(self) -> bool:
        return bool(self._data.get("degraded"))

    def as_dict(self) -> dict[str, Any]:
        return {k: self._data.get(k) for k in self.FIELDS}

    def can_reload(self, *, max_auto_reloads: int, cooldown: float,
                   now: float | None = None) -> tuple[bool, str]:
        """C1 + C2：次数有界 且 冷却已过。"""
        now = time.time() if now is None else now
        if self.attempt >= max(0, int(max_auto_reloads)):
            return False, f"自修复次数已用尽（{self.attempt}/{max_auto_reloads}）"
        elapsed = now - float(self._data.get("last_reload_ts") or 0.0)
        if elapsed < float(cooldown or 0):
            return False, (f"自修复冷却中（剩余 "
                           f"{int(float(cooldown) - elapsed)}s）")
        return True, ""

    def note_reload(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._data["attempt"] = self.attempt + 1
        self._data["last_reload_ts"] = now
        self._data["stable_since"] = 0.0
        self.save()

    def note_heartbeat(self, ts: float) -> None:
        if ts and ts > float(self._data.get("last_heartbeat_seen") or 0.0):
            self._data["last_heartbeat_seen"] = ts
            self.save()

    def note_connected(self, *, reset_sec: float,
                       now: float | None = None) -> bool:
        """C4：稳定满 `reset_sec` 后自动清零额度。返回是否发生了复位。"""
        now = time.time() if now is None else now
        if float(self._data.get("stable_since") or 0.0) <= 0.0:
            self._data["stable_since"] = now
            self._data["degraded"] = False
            self._data["degraded_since"] = 0.0
            self.save()
            return False
        if self.attempt and (now - float(self._data["stable_since"])) >= float(reset_sec):
            self._data["attempt"] = 0
            self._data["degraded"] = False
            self._data["degraded_since"] = 0.0
            self.save()
            return True
        return False

    def mark_degraded(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if not self._data.get("degraded"):
            self._data["degraded_since"] = now
        self._data["degraded"] = True
        self._data["stable_since"] = 0.0
        self.save()

    def reset(self) -> None:
        """`/guardian reset`：人工清零额度并解除降级。"""
        self._data.update({"attempt": 0, "last_reload_ts": 0.0,
                           "degraded": False, "degraded_since": 0.0,
                           "stable_since": 0.0})
        self.save()


class KernelIpcClient:
    """适配层侧的 IPC 客户端。"""

    def __init__(self, *,
                 transport: str = "unix",
                 socket_path: str = "",
                 tcp_host: str = "127.0.0.1",
                 tcp_port: int = 6198,
                 token_file: str = "",
                 shared_dir: str = "",
                 request_timeout: float = 10.0,
                 max_auto_reloads: int = 1,
                 reload_cooldown: float = 600.0,
                 reload_grace_sec: float = 90.0,
                 reload_reset_sec: float = 300.0,
                 reload_request_path: str = "",
                 health_path: str = "",
                 adapter_events_path: str = "",
                 unreachable_notify_after: float = 90.0) -> None:
        self.transport = transport
        self.shared_dir = shared_dir or "."
        guardian_dir = os.path.join(self.shared_dir, "guardian")
        self.socket_path = socket_path or os.path.join(guardian_dir, "kernel.sock")
        self.tcp_host = tcp_host or "127.0.0.1"
        self.tcp_port = int(tcp_port or 6198)
        self.token_file = token_file or os.path.join(guardian_dir, "kernel.token")
        self.heartbeat_path = os.path.join(guardian_dir, "kernel_heartbeat.json")
        self.request_timeout = max(1.0, float(request_timeout or 10.0))
        self.max_auto_reloads = max(0, int(max_auto_reloads))
        self.reload_cooldown = max(0.0, float(reload_cooldown or 0.0))
        self.reload_grace_sec = max(0.0, float(reload_grace_sec or 0.0))
        self.reload_reset_sec = max(0.0, float(reload_reset_sec or 0.0))
        self.reload_request_path = reload_request_path or os.path.join(
            guardian_dir, "kernel_reload_request.json")
        self.health = KernelHealth(
            health_path or os.path.join(guardian_dir, "kernel_health.json"))
        self.adapter_events_path = adapter_events_path or os.path.join(
            guardian_dir, "adapter_events.jsonl")
        self.unreachable_notify_after = max(0.0, float(unreachable_notify_after))

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[Any, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._connected = False
        self._last_error: IpcError | None = None
        self._outage_started = 0.0
        self._notified_outage = False

        self._event_cbs: list[EventCallback] = []
        self._state_cbs: list[StateCallback] = []

    # ================================================================== #
    # 端点与诊断
    # ================================================================== #
    @property
    def endpoint(self) -> str:
        if self.transport == "unix":
            return f"unix://{self.socket_path}"
        return f"tcp://{self.tcp_host}:{self.tcp_port}"

    @property
    def connected(self) -> bool:
        return self._connected and self._writer is not None

    def last_heartbeat(self) -> float:
        try:
            with open(self.heartbeat_path, "r", encoding="utf-8") as fh:
                return float((json.load(fh) or {}).get("ts") or 0.0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return 0.0

    def diagnostics(self, errno: Any = None) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "errno": errno,
            "last_heartbeat": _iso(self.last_heartbeat()),
        }

    # ================================================================== #
    # 事件 / 状态回调
    # ================================================================== #
    def on_event(self, cb: EventCallback) -> None:
        self._event_cbs.append(cb)

    def on_state(self, cb: StateCallback) -> None:
        self._state_cbs.append(cb)

    async def _emit_event(self, method: str, params: dict) -> None:
        for cb in list(self._event_cbs):
            try:
                result = cb(method, params)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[guardian] IPC 事件回调异常: {exc}")

    async def _emit_state(self, state: str, extra: dict | None = None) -> None:
        for cb in list(self._state_cbs):
            try:
                result = cb(state, extra or {})
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[guardian] IPC 状态回调异常: {exc}")

    # ================================================================== #
    # 适配层自己的审计流（L3，单写者=适配层）
    # ================================================================== #
    def audit_event(self, kind: str, detail: str = "", **extra: Any) -> None:
        record = {"ts": time.time(), "kind": kind, "detail": detail}
        record.update({k: v for k, v in extra.items() if v is not None})
        try:
            os.makedirs(os.path.dirname(self.adapter_events_path), exist_ok=True)
            with open(self.adapter_events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover - 磁盘异常
            logger.warning(f"[guardian] 写 adapter_events.jsonl 失败: {exc}")

    # ================================================================== #
    # 连接
    # ================================================================== #
    def _read_token(self) -> str:
        try:
            with open(self.token_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    async def connect(self) -> bool:
        """建立连接（不重试）。成功返回 True，失败抛 `IpcError`。"""
        if self.connected:
            return True
        if self.transport == "unix" and not unix_socket_supported():
            raise IpcError(
                E_KERNEL_UNAVAILABLE,
                "当前平台不支持 Unix domain socket，请把 kernel.transport 设为 tcp",
                diagnostics=self.diagnostics(errno="EAFNOSUPPORT"),
                self_healable=False)
        try:
            if self.transport == "unix":
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(self.socket_path),
                    timeout=self.request_timeout)
            else:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.tcp_host, self.tcp_port),
                    timeout=self.request_timeout)
        except FileNotFoundError as exc:
            raise IpcError(E_KERNEL_UNAVAILABLE,
                           f"内核 socket 不存在: {self.socket_path}",
                           diagnostics=self.diagnostics(errno="ENOENT")) from exc
        except ConnectionRefusedError as exc:
            raise IpcError(E_KERNEL_UNAVAILABLE, "内核拒绝连接",
                           diagnostics=self.diagnostics(errno="ECONNREFUSED")) from exc
        except asyncio.TimeoutError as exc:
            raise IpcError(E_KERNEL_TIMEOUT, "连接内核超时",
                           diagnostics=self.diagnostics(errno="ETIMEDOUT")) from exc
        except OSError as exc:
            raise IpcError(E_KERNEL_UNAVAILABLE, f"连接内核失败: {exc}",
                           diagnostics=self.diagnostics(errno=exc.errno)) from exc
        self._reader, self._writer = reader, writer
        self._connected = True
        self._last_error = None
        self._reader_task = asyncio.create_task(self._read_loop())
        return True

    async def close(self) -> None:
        self._connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        await self._teardown_transport()
        self._fail_pending(IpcError(E_KERNEL_UNAVAILABLE, "IPC 连接已关闭",
                                    diagnostics=self.diagnostics()))

    async def _teardown_transport(self) -> None:
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is None:
            return
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    # ================================================================== #
    # 读循环
    # ================================================================== #
    async def _read_loop(self) -> None:
        decoder = LineDecoder()
        reader = self._reader
        assert reader is not None
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                for msg in decoder.feed(chunk):
                    await self._dispatch(msg)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[guardian] IPC 读循环异常: {exc}")
        finally:
            was_connected = self._connected
            self._connected = False
            await self._teardown_transport()
            self._fail_pending(IpcError(E_KERNEL_UNAVAILABLE, "IPC 连接中断",
                                        diagnostics=self.diagnostics()))
            if was_connected and not self._outage_started:
                self._outage_started = time.time()
                self.audit_event("ipc_disconnected", self.endpoint)
                logger.warning(f"[guardian] IPC 连接断开: {self.endpoint}")
                await self._emit_state("disconnected", {})

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and msg.get("id") is not None:
            future = self._pending.pop(msg.get("id"), None)
            if future is None or future.done():
                return
            if "error" in msg:
                err = msg.get("error") or {}
                future.set_exception(IpcError(
                    str(err.get("code") or E_INTERNAL),
                    str(err.get("message") or ""),
                    diagnostics=err.get("diagnostics") or {}))
            else:
                future.set_result(msg.get("result"))
            return
        method = msg.get("method")
        if isinstance(method, str):
            await self._emit_event(method, msg.get("params") or {})

    def _fail_pending(self, exc: IpcError) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # ================================================================== #
    # 调用
    # ================================================================== #
    async def call(self, method: str, params: dict | None = None, *,
                   timeout: float | None = None) -> Any:
        """一次 IPC 调用（不重试、不自愈）。失败抛 `IpcError`。"""
        if not self.connected:
            raise self._last_error or IpcError(
                E_KERNEL_UNAVAILABLE, "IPC 未连接",
                diagnostics=self.diagnostics())
        req_id = next(self._ids)
        payload = make_request(req_id, method, params,
                              auth=self._read_token() if self.transport == "tcp" else "")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        writer = self._writer
        if writer is None:
            self._pending.pop(req_id, None)
            raise IpcError(E_KERNEL_UNAVAILABLE, "IPC 未连接",
                           diagnostics=self.diagnostics())
        try:
            writer.write(encode(payload))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            self._pending.pop(req_id, None)
            raise IpcError(E_KERNEL_UNAVAILABLE, f"发送失败: {exc}",
                           diagnostics=self.diagnostics(errno=getattr(exc, "errno", None))) from exc
        try:
            return await asyncio.wait_for(
                future, timeout=timeout if timeout is not None else self.request_timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise IpcError(
                E_KERNEL_TIMEOUT,
                f"内核无响应（超时 {timeout or self.request_timeout:.0f}s），"
                "可能正在重启或被阻塞",
                diagnostics=self.diagnostics(errno="ETIMEDOUT")) from exc

    # ================================================================== #
    # D4-a+ 有界自修复阶梯
    # ================================================================== #
    async def call_with_recovery(self, method: str, params: dict | None = None,
                                 *, timeout: float | None = None) -> Any:
        """带自愈的调用。阶梯见 B16 §12.3.5。

        返回值/异常语义与 `call` 一致，但：
        - 只对**可用性错误**（`kernel_unavailable` / `kernel_timeout`）触发自愈；
        - **破坏性 method 绝不自动重试**（避免重复执行，§6.4）；
        - 自愈额度耗尽 → `kernel_degraded`（显式失败，不再假装成功）。
        """
        try:
            return await self._call_guarded(method, params, timeout=timeout)
        except IpcError as exc:
            if not exc.self_healable:
                raise
            self._last_error = exc

        # ---- Stage 1：静默重连（指数退避，合计 ~15.5s） ----
        await self._mark_outage()
        if await self._fast_reconnect():
            self.audit_event("ipc_reconnected", f"Stage1 静默重连成功 {self.endpoint}")
            logger.info(f"[guardian] IPC 静默重连成功: {self.endpoint}")
            await self._emit_state("reconnected", {"stage": 1})
            if method not in NO_RETRY_METHODS:
                with contextlib.suppress(IpcError):
                    return await self._call_guarded(method, params, timeout=timeout)
            else:
                raise self._last_error

        # ---- Stage 2：请求一次内核重载（受 C1/C2 闸门） ----
        granted, reason = self._request_reload()
        if not granted:
            return await self._fail_degraded(reason)

        # ---- Stage 3：宽限期（等容器起 → listen → 首次心跳） ----
        if await self._wait_grace():
            self.audit_event("kernel_reloaded_ok", f"Stage3 宽限期内恢复 {self.endpoint}")
            logger.info(f"[guardian] 内核重载后恢复: {self.endpoint}")
            await self._emit_state("reconnected", {"stage": 3})
            if method not in NO_RETRY_METHODS:
                with contextlib.suppress(IpcError):
                    return await self._call_guarded(method, params, timeout=timeout)
            raise self._last_error

        # ---- Stage 4：仍失败 → 判定自修复失败，显式失败 ----
        return await self._fail_degraded("重载后仍未恢复")

    async def _call_guarded(self, method: str, params: dict | None,
                            *, timeout: float | None) -> Any:
        if not self.connected:
            await self._try_single_connect()
        return await self.call(method, params, timeout=timeout)

    async def _try_single_connect(self) -> None:
        try:
            await self.connect()
        except IpcError as exc:
            self._last_error = exc
            raise

    async def _fast_reconnect(self) -> bool:
        for delay in RECONNECT_BACKOFF:
            await asyncio.sleep(delay)
            if self.connected:
                return True
            try:
                await self.connect()
            except IpcError as exc:
                self._last_error = exc
                continue
            else:
                return True
        return False

    def _request_reload(self) -> tuple[bool, str]:
        """Stage 2：写 `kernel_reload_request.json`，由看门狗消费。"""
        ok, reason = self.health.can_reload(
            max_auto_reloads=self.max_auto_reloads,
            cooldown=self.reload_cooldown)
        if not ok:
            return False, reason
        payload = {
            "nonce": secrets.token_hex(8),
            "ts": time.time(),
            "reason": "适配层检测到内核不可用，请求重载内核容器",
            "attempt": self.health.attempt + 1,
            "last_heartbeat": self.last_heartbeat(),
        }
        try:
            os.makedirs(os.path.dirname(self.reload_request_path), exist_ok=True)
            tmp = f"{self.reload_request_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            # 原子发布：os.link 是 no-clobber 的，天然避免两个适配层实例互相覆盖
            os.replace(tmp, self.reload_request_path)
        except OSError as exc:
            logger.warning(f"[guardian] 写内核重载请求失败: {exc}")
            return False, f"写重载请求失败: {exc}"
        self.health.note_reload(payload["ts"])
        self.audit_event("kernel_reload_requested", self.endpoint,
                         nonce=payload["nonce"], attempt=payload["attempt"])
        logger.warning(
            f"[guardian] 已请求内核重载（第 {payload['attempt']}/"
            f"{self.max_auto_reloads} 次），等待看门狗消费")
        return True, ""

    async def _wait_grace(self) -> bool:
        deadline = time.time() + self.reload_grace_sec
        while time.time() < deadline:
            await asyncio.sleep(min(2.0, max(0.2, self.reload_grace_sec / 10)))
            if self.connected:
                return True
            try:
                await self.connect()
            except IpcError as exc:
                self._last_error = exc
                continue
            return True
        return False

    async def _fail_degraded(self, reason: str) -> Any:
        self.health.mark_degraded()
        self.audit_event("kernel_degraded", reason, endpoint=self.endpoint)
        logger.error(f"[guardian] 内核自修复失败，进入降级：{reason}")
        await self._emit_state("degraded", {"reason": reason})
        raise IpcError(
            E_KERNEL_DEGRADED,
            f"守护内核不可用（自修复失败：{reason}）",
            diagnostics=self.diagnostics(),
            self_healable=False)

    async def _mark_outage(self) -> None:
        if not self._outage_started:
            self._outage_started = time.time()
        self.health.note_heartbeat(self.last_heartbeat())
        self.audit_event("kernel_unreachable", self.endpoint,
                         last_heartbeat=self.last_heartbeat())
        await self._emit_state("unreachable", {"since": self._outage_started})

    # ================================================================== #
    # 稳定计时 / 复位
    # ================================================================== #
    def note_healthy(self) -> None:
        """一次成功调用后调用：维护 C4 的"稳定计时"，并复位掉线状态。"""
        if self._outage_started:
            self.audit_event("ipc_stable", self.endpoint)
            self._outage_started = 0.0
            self._notified_outage = False
        self.health.note_heartbeat(self.last_heartbeat())
        if self.health.note_connected(reset_sec=self.reload_reset_sec):
            self.audit_event("kernel_health_reset",
                             "内核稳定满窗口，自修复额度已复位")
            logger.info("[guardian] 内核稳定满窗口，自修复额度已复位")

    def reset(self) -> None:
        """`/guardian reset`：人工清零自修复额度。"""
        self.health.reset()
        self.audit_event("kernel_health_reset", "人工 /guardian reset")
        logger.info("[guardian] 已人工复位内核自修复额度")

    def outage_seconds(self) -> float:
        return (time.time() - self._outage_started) if self._outage_started else 0.0

    def should_notify_outage(self) -> bool:
        """L4：掉线超过阈值 → 适配层主动推**唯一一条**"内核不可用"告警。

        这条告警必须由适配层产生：内核就是挂掉的那个，它无法产生"我挂了"。
        """
        if not self._outage_started or self._notified_outage:
            return False
        if self.outage_seconds() >= self.unreachable_notify_after:
            self._notified_outage = True
            return True
        return False


__all__ = ["KernelIpcClient", "KernelHealth", "RECONNECT_BACKOFF",
           "NO_RETRY_METHODS", "E_UNAUTHORIZED"]
