"""Guardian 守护系统 —— Docker Engine HTTP 客户端（unix socket）。

对应文档：B04 §3

设计：优先走 `/var/run/docker.sock` 的 Engine HTTP API（容器内无需 docker CLI），
可选降级到 CLI。所有调用带超时，异常统一为 DockerUnavailable / DockerError。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from ..core.log import logger


class DockerUnavailable(RuntimeError):
    """daemon/socket 不可达。"""


class DockerError(RuntimeError):
    """Docker API 返回错误或操作失败。"""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _demux(data: bytes) -> str:
    """解析 Docker exec 的多路复用流（8 字节帧头）。"""
    out: list[str] = []
    i = 0
    while i + 8 <= len(data):
        size = int.from_bytes(data[i + 4:i + 8], "big")
        payload = data[i + 8:i + 8 + size]
        out.append(payload.decode("utf-8", "replace"))
        i += 8 + size
    if not out:
        return data.decode("utf-8", "replace")
    return "".join(out)


class DockerClient:
    def __init__(self, socket_path: str = "/var/run/docker.sock",
                 api_version: str = "v1.44", timeout: float = 10.0,
                 use_cli_fallback: bool = False, cli_path: str = "docker") -> None:
        self.socket_path = socket_path
        self.api_version = api_version or "v1.44"
        self.timeout = float(timeout)
        self.use_cli_fallback = bool(use_cli_fallback)
        self.cli_path = cli_path
        self._session: aiohttp.ClientSession | None = None
        self._negociated = False

    # ------------------------------------------------------------------ #
    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.UnixConnector(path=self.socket_path)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._session

    async def _request(self, method: str, path: str, *,
                       params: dict | None = None,
                       payload: Any = None,
                       raw: bool = False,
                       timeout: float | None = None) -> Any:
        session = await self._ensure_session()
        url = f"http://localhost/{self.api_version}{path}"
        try:
            async with session.request(
                method, url, params=params, json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout or self.timeout),
            ) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    text = body.decode("utf-8", "replace")[:400]
                    raise DockerError(
                        f"Docker API {method} {path} -> {resp.status}: {text}",
                        resp.status)
                if raw:
                    return body
                if not body:
                    return {}
                try:
                    return json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return {"_raw": body.decode("utf-8", "replace")}
        except asyncio.TimeoutError as exc:
            # 请求超时 **不等于** daemon 不可达：请求已经发出、daemon 正在处理
            # （或这次操作很慢），结果未知。必须归到 DockerError（daemon 有应答、
            # 这次操作失败），否则调用方的降级分支会误判成"docker 访问失效"。
            # 2026-10-16 实机 C16-K3 发现：`docker stop -t 10` 的 HTTP 往返必然
            # ≥10s，撞上客户端 10s 总超时 → 被判成 DockerUnavailable →
            # `_execute_direct` 走 `unreachable` 分支，**跳过恢复与 `StartedAt`
            # 复核**，D2-b 的"亲眼看到"完全失效。注意要排在 aiohttp.ClientError
            # 之前：aiohttp 的 ServerTimeoutError 同时是 ClientError 子类。
            raise DockerError(
                f"Docker 请求超时（{timeout or self.timeout:.0f}s）: {method} {path}"
                "（daemon 已收到请求，本次操作结果未知）") from exc
        except aiohttp.ClientError as exc:
            raise DockerUnavailable(f"Docker 不可达({self.socket_path}): {exc}") from exc
        except NotImplementedError as exc:
            # Windows 的 ProactorEventLoop 不支持 UnixConnector（连接期抛
            # NotImplementedError，不是 aiohttp.ClientError）。不翻译会让它击穿
            # "所有 Docker 失败都是 DockerUnavailable/DockerError"的契约，
            # 使调用方的降级分支失效（例如内核直连动作回退看门狗）。
            raise DockerUnavailable(
                f"当前平台不支持 Unix socket 连接（{self.socket_path}）") from exc

    # ------------------------------------------------------------------ #
    async def ping(self) -> bool:
        try:
            await self._request("GET", "/_ping")
            return True
        except (DockerUnavailable, DockerError):
            return False

    async def version(self) -> dict[str, Any]:
        return await self._request("GET", "/version")

    async def negotiate(self) -> str:
        """协商 API 版本：优先配置值，失败则回退到 /version 报告的版本。"""
        if self._negociated:
            return self.api_version
        try:
            info = await self._request("GET", "/version")
            api = str(info.get("ApiVersion") or "").strip()
            if api:
                self.api_version = api if api.startswith("v") else f"v{api}"
        except (DockerUnavailable, DockerError) as exc:
            logger.debug(f"[guardian] Docker 版本协商失败，沿用 {self.api_version}: {exc}")
        self._negociated = True
        return self.api_version

    async def info(self) -> dict[str, Any]:
        return await self._request("GET", "/info")

    async def list_containers(self, all_: bool = True) -> list[dict[str, Any]]:
        data = await self._request("GET", "/containers/json", params={"all": "1" if all_ else "0"})
        return data if isinstance(data, list) else []

    async def exists(self, name: str) -> bool:
        try:
            await self.inspect(name)
            return True
        except DockerError as exc:
            if exc.status == 404:
                return False
            raise
        except DockerUnavailable:
            raise

    async def inspect(self, name: str) -> dict[str, Any]:
        return await self._request("GET", f"/containers/{name}/json")

    async def restart(self, name: str, timeout: int = 10) -> None:
        # `t` 是 daemon 的优雅停止宽限期（SIGTERM → 等待 → SIGKILL），HTTP 往返
        # 必然 ≥ 该值；客户端总超时必须显著大于它，否则每次重启/停止都会撞超时。
        await self._request("POST", f"/containers/{name}/restart",
                            params={"t": str(int(timeout))},
                            timeout=int(timeout) + 20)

    async def start(self, name: str) -> None:
        await self._request("POST", f"/containers/{name}/start")

    async def stop(self, name: str, timeout: int = 10) -> None:
        await self._request("POST", f"/containers/{name}/stop",
                            params={"t": str(int(timeout))},
                            timeout=int(timeout) + 20)

    async def stats(self, name: str) -> dict[str, Any]:
        return await self._request("GET", f"/containers/{name}/stats",
                                   params={"stream": "false"})

    # ---- exec ---- #
    async def exec_run(self, name: str, cmd: list[str],
                       timeout: float | None = None) -> tuple[int, str]:
        """在容器内执行命令，返回 (exit_code, 合并输出)。cmd 为 argv 列表。"""
        created = await self._request(
            "POST", f"/containers/{name}/exec",
            payload={
                "AttachStdout": True,
                "AttachStderr": True,
                "Tty": False,
                "Cmd": list(cmd),
            })
        exec_id = created.get("Id")
        if not exec_id:
            raise DockerError("exec 创建失败：未返回 Id")
        session = await self._ensure_session()
        url = f"http://localhost/{self.api_version}/exec/{exec_id}/start"
        try:
            async with session.post(
                url, json={"Detach": False, "Tty": False},
                timeout=aiohttp.ClientTimeout(total=timeout or self.timeout),
            ) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    raise DockerError(f"exec start -> {resp.status}", resp.status)
        except asyncio.TimeoutError as exc:
            # 与 `_request` 同一条判据：exec 超时说明 daemon 可达、命令跑太久，
            # 不是"不可达"。归到 DockerError，调用方（ros2_runner）会给出
            # "exec 失败/超时"而不是"Docker 不可达"。
            raise DockerError(
                f"exec 超时({timeout or self.timeout}s): {' '.join(cmd[:3])}") from exc
        except aiohttp.ClientError as exc:
            raise DockerUnavailable(f"exec 不可达: {exc}") from exc
        except NotImplementedError as exc:
            raise DockerUnavailable(
                f"当前平台不支持 Unix socket 连接（{self.socket_path}）") from exc
        text = _demux(body)
        info = await self._request("GET", f"/exec/{exec_id}/json")
        return int(info.get("ExitCode") or 0), text

    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
