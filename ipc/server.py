"""Guardian 守护系统 —— 内核 IPC 服务端（B16 §6.1 / §6.3 / §7）。

监听共享卷上的 Unix domain socket（D3-a 优先），Windows/异常时回退
`127.0.0.1` + `kernel.token`。同时负责三件"内核侧"的持久化：

- `kernel_heartbeat.json`：心跳（域 C 看门狗据此判断内核是否卡死）
- `kernel.token`：TCP 模式鉴权令牌（`0600`，不写日志）
- 事件推送：把内核产生的通知广播给所有已连接的适配层

**鉴权**：Unix socket 靠文件权限（`0660` + 属组限定）；TCP 靠 token，
每个请求必须携带匹配的 `auth` 字段。TCP 模式下一律绑定 loopback，
绝不监听 `0.0.0.0`（B16 §11-2）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
import sys
import time
from typing import Any

from ..core.log import logger
from .methods import build_handlers
from .protocol import (
    E_BAD_REQUEST,
    E_INTERNAL,
    E_UNAUTHORIZED,
    E_UNKNOWN_METHOD,
    EV_KERNEL_READY,
    EV_NOTIFY,
    IpcError,
    LineDecoder,
    encode,
    make_error,
    make_event,
    make_response,
)

DEFAULT_TCP_PORT = 6198
SOCKET_MODE = 0o660
TOKEN_MODE = 0o600


def unix_socket_supported() -> bool:
    """当前解释器/平台是否支持 asyncio 的 Unix domain socket。

    Windows 上 `socket.AF_UNIX` 可能存在，但 ProactorEventLoop 不支持
    `asyncio.start_unix_server`——所以不能只看 `hasattr(socket, "AF_UNIX")`。
    """
    if not hasattr(socket, "AF_UNIX"):
        return False
    if sys.platform == "win32":
        return False
    return hasattr(asyncio, "start_unix_server")


class KernelIpcServer:
    """内核 IPC 服务端。一个内核实例对应一个服务端。"""

    def __init__(self, kernel: Any, *,
                 transport: str = "unix",
                 socket_path: str = "",
                 tcp_host: str = "127.0.0.1",
                 tcp_port: int = DEFAULT_TCP_PORT,
                 token: str = "",
                 token_file: str = "",
                 shared_dir: str = "",
                 heartbeat_interval: float = 30.0,
                 version: str = "") -> None:
        self.kernel = kernel
        self.transport = transport
        self.shared_dir = shared_dir or (
            os.path.dirname(socket_path) if socket_path else kernel.data_dir)
        self.socket_path = socket_path or os.path.join(
            self.shared_dir, "guardian", "kernel.sock")
        self.tcp_host = tcp_host or "127.0.0.1"
        self.tcp_port = int(tcp_port or DEFAULT_TCP_PORT)
        self.token_file = token_file or os.path.join(
            self.shared_dir, "guardian", "kernel.token")
        self.heartbeat_interval = max(5.0, float(heartbeat_interval or 30.0))
        self.version = version or str(getattr(kernel, "version", "") or "")

        self._handlers = build_handlers(kernel)
        self._server: Any = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._stopping = False
        # Unix 模式不需要 token；TCP 模式必须有一个（显式给或现生成）
        self._token = ""
        if self.transport == "tcp":
            self._token = token or secrets.token_urlsafe(32)

    # ================================================================== #
    # 属性
    # ================================================================== #
    @property
    def heartbeat_path(self) -> str:
        return os.path.join(self.shared_dir, "guardian", "kernel_heartbeat.json")

    @property
    def token(self) -> str:
        return self._token

    @property
    def is_started(self) -> bool:
        return self._started

    def client_count(self) -> int:
        return len(self._clients)

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    async def start(self) -> None:
        if self._started:
            return
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        if self.transport == "unix":
            if not unix_socket_supported():
                raise IpcError(
                    E_INTERNAL,
                    f"当前平台不支持 Unix domain socket（{sys.platform}），"
                    "请把 kernel.transport 设为 tcp")
            self._server = await self._start_unix()
        else:
            self._server = await self._start_tcp()
        self._started = True
        # 通知发送器：内核产生的告警通过 IPC 广播给适配层
        setter = getattr(self.kernel, "set_sender", None)
        if callable(setter):
            setter(self._kernel_sender)
        self._write_heartbeat()
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        await self.broadcast_event(EV_KERNEL_READY,
                                   {"version": self.version, "ts": time.time()})
        logger.info(
            f"[guardian] 内核 IPC 服务端已启动 | transport={self.transport} "
            f"endpoint={self.endpoint}")

    async def _start_unix(self):
        # 陈旧 socket 处理：先探活。能连上 = 已有实例在跑 → 拒绝重复启动；
        # 连不上 = 上次崩溃留下的死文件 → 清掉再绑。
        if os.path.exists(self.socket_path):
            if await self._probe_existing_unix():
                raise IpcError(
                    E_INTERNAL,
                    f"已有内核在监听 {self.socket_path}，拒绝重复启动")
            with contextlib.suppress(OSError):
                os.unlink(self.socket_path)
        server = await asyncio.start_unix_server(
            self._on_client, path=self.socket_path)
        with contextlib.suppress(OSError):
            os.chmod(self.socket_path, SOCKET_MODE)
        return server

    async def _probe_existing_unix(self) -> bool:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path), timeout=1.5)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    async def _start_tcp(self):
        # 只绑 loopback：内核绝不暴露到网络（B16 §11-2）
        server = await asyncio.start_server(
            self._on_client, host=self.tcp_host, port=self.tcp_port)
        self._write_token()
        return server

    async def stop(self) -> None:
        if not self._started:
            return
        self._stopping = True
        self._started = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        for writer in list(self._clients):
            await self._drop_client(writer)
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        if self.transport == "unix":
            with contextlib.suppress(OSError):
                os.unlink(self.socket_path)
        self._stopping = False
        logger.info("[guardian] 内核 IPC 服务端已停止")

    @property
    def endpoint(self) -> str:
        if self.transport == "unix":
            return f"unix://{self.socket_path}"
        return f"tcp://{self.tcp_host}:{self.bound_port}"

    @property
    def bound_port(self) -> int:
        """实际绑定的端口（测试用 0 让内核选临时端口时，这里是真实值）。"""
        if self._server is None:
            return self.tcp_port
        try:
            return int(self._server.sockets[0].getsockname()[1])
        except (AttributeError, IndexError, OSError):  # pragma: no cover
            return self.tcp_port

    # ================================================================== #
    # 共享文件
    # ================================================================== #
    def _write_token(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
            tmp = f"{self.token_file}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_MODE)
            try:
                os.write(fd, self._token.encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp, self.token_file)
            with contextlib.suppress(OSError):
                os.chmod(self.token_file, TOKEN_MODE)
        except OSError as exc:  # pragma: no cover - 磁盘异常
            logger.warning(f"[guardian] 写入 kernel.token 失败: {exc}")

    def _write_heartbeat(self) -> None:
        payload = {
            "ts": time.time(),
            "pid": os.getpid(),
            "version": self.version,
            "transport": self.transport,
            "endpoint": self.endpoint,
            # 显式给端口：部署方常用 `tcp_port: 0` 让内核自选临时端口，
            # 此时只有心跳文件能告诉运维/适配层"实际端口是多少"。
            "tcp_port": self.bound_port if self.transport == "tcp" else 0,
        }
        try:
            os.makedirs(os.path.dirname(self.heartbeat_path), exist_ok=True)
            tmp = f"{self.heartbeat_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.heartbeat_path)
        except OSError as exc:  # pragma: no cover - 磁盘异常
            logger.warning(f"[guardian] 写入心跳失败: {exc}")

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                self._write_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[guardian] 心跳写入异常: {exc}")

    # ================================================================== #
    # 连接处理
    # ================================================================== #
    async def _on_client(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        decoder = LineDecoder()
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                try:
                    messages = decoder.feed(chunk)
                except IpcError as exc:
                    await self._send(writer, make_error(None, exc.code,
                                                        exc.message))
                    break
                for msg in messages:
                    await self._handle(writer, msg)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[guardian] IPC 连接异常: {exc}")
        finally:
            await self._drop_client(writer)

    async def _drop_client(self, writer: asyncio.StreamWriter) -> None:
        self._clients.discard(writer)
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _handle(self, writer: asyncio.StreamWriter,
                      msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params")
        if not isinstance(method, str) or not method:
            await self._send(writer, make_error(req_id, E_BAD_REQUEST,
                                                "缺少 method"))
            return
        if params is None:
            params = {}
        if not isinstance(params, dict):
            await self._send(writer, make_error(req_id, E_BAD_REQUEST,
                                                "params 必须是对象"))
            return
        # 事件类消息是内核 → 适配层单向的，适配层不该发过来
        if req_id is None:
            await self._send(writer, make_error(
                None, E_BAD_REQUEST, "请求必须带 id（事件为内核单向消息）"))
            return
        if not self._authorized(msg):
            await self._send(writer, make_error(
                req_id, E_UNAUTHORIZED,
                "IPC 鉴权失败（kernel.token 不匹配）"))
            return
        handler = self._handlers.get(method)
        if handler is None:
            await self._send(writer, make_error(
                req_id, E_UNKNOWN_METHOD, f"未知 method: {method}"))
            return
        try:
            result = await handler(params)
        except IpcError as exc:
            await self._send(writer, make_error(
                req_id, exc.code, exc.message, exc.diagnostics))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[guardian] IPC method {method} 内部错误: {exc}",
                         exc_info=True)
            await self._send(writer, make_error(
                req_id, E_INTERNAL, f"内核内部错误: {type(exc).__name__}: {exc}"))
        else:
            await self._send(writer, make_response(req_id, result))

    def _authorized(self, msg: dict[str, Any]) -> bool:
        if self.transport != "tcp":
            return True          # Unix socket 靠文件权限鉴权
        provided = msg.get("auth") or ""
        if not isinstance(provided, str):
            return False
        return secrets.compare_digest(provided, self._token)

    async def _send(self, writer: asyncio.StreamWriter,
                    msg: dict[str, Any]) -> None:
        try:
            writer.write(encode(msg))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            await self._drop_client(writer)

    # ================================================================== #
    # 事件推送
    # ================================================================== #
    async def broadcast_event(self, method: str, params: dict | None = None) -> int:
        """把事件推给所有已连接客户端，返回成功投递的连接数。"""
        if not self._clients:
            return 0
        payload = make_event(method, params)
        sent = 0
        for writer in list(self._clients):
            try:
                writer.write(encode(payload))
                await writer.drain()
                sent += 1
            except (ConnectionResetError, BrokenPipeError, OSError):
                await self._drop_client(writer)
        return sent

    async def _kernel_sender(self, umo: str, text: str) -> None:
        """作为内核的 `sender` 注入：把通知变成 IPC 事件广播出去。

        没有任何适配层连接时**不能静默**（I4）：至少要让内核日志与审计知道
        这条告警没送出去。
        """
        sent = await self.broadcast_event(
            EV_NOTIFY, {"umo": umo, "text": text, "severity": "warn",
                        "ts": time.time()})
        if sent == 0:
            logger.warning(
                f"[guardian] 通知无 IPC 接收方（适配层未连接），仅记录："
                f"{text[:120]}")


__all__ = ["DEFAULT_TCP_PORT", "KernelIpcServer", "unix_socket_supported"]
