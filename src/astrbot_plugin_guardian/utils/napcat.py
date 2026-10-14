"""Guardian 守护系统 —— NapCat / OneBot 11 HTTP 客户端。

对应文档：B05 §2

- OneBot 11 HTTP：默认 http://napcat:3000，Bearer token 鉴权
- NapCat WebUI：默认 :6099，用于快速登录（可选）
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import aiohttp

from astrbot.api import logger


class NapCatError(RuntimeError):
    pass


class NapCatUnavailable(NapCatError):
    """网络不可达 / 超时。"""


class NapCatAuthError(NapCatError):
    """鉴权失败（401/403）。"""


class NapCatClient:
    def __init__(self, base: str, token: str = "", timeout: float = 8.0,
                 webui_base: str = "", webui_token: str = "",
                 action_caller: Callable[[str], Awaitable[Any]] | None = None) -> None:
        self.base = (base or "").rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self.webui_base = (webui_base or "").rstrip("/")
        self.webui_token = webui_token or token or ""
        self._session: aiohttp.ClientSession | None = None
        self._action_caller = action_caller

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session

    def _headers(self, token: str = "") -> dict[str, str]:
        tok = token or self.token
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    async def _post(self, endpoint: str, payload: dict | None = None,
                    *, use_webui: bool = False) -> dict[str, Any]:
        base = self.webui_base if use_webui else self.base
        if not base:
            raise NapCatUnavailable("未配置 NapCat 地址")
        session = await self._ensure_session()
        url = f"{base}{endpoint}"
        headers = self._headers(self.webui_token if use_webui else self.token)
        try:
            async with session.post(url, json=payload or {}, headers=headers) as resp:
                body = await resp.text()
                if resp.status in (401, 403):
                    raise NapCatAuthError(f"NapCat 鉴权失败({resp.status})")
                if resp.status >= 400:
                    raise NapCatError(f"NapCat {endpoint} -> {resp.status}: {body[:200]}")
                if not body:
                    return {}
                try:
                    import json
                    data = json.loads(body)
                except Exception as exc:  # noqa: BLE001
                    raise NapCatError(f"NapCat 返回畸形 JSON: {body[:120]}") from exc
                return data if isinstance(data, dict) else {"data": data}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise NapCatUnavailable(f"NapCat 不可达({base}): {exc}") from exc

    # ------------------------------------------------------------------ #
    async def _query(self, action: str) -> dict[str, Any]:
        try:
            if self._action_caller is not None:
                data = await asyncio.wait_for(self._action_caller(action), self.timeout)
            else:
                data = await self._post(f"/{action}")
        except asyncio.CancelledError:
            raise
        except NapCatError:
            raise
        except Exception as exc:  # aiocqhttp 的断连、超时和 API 失败均不得算离线证据
            raise NapCatUnavailable(f"OneBot {action} 查询失败 ({type(exc).__name__})") from exc
        if not isinstance(data, dict) or not data:
            raise NapCatError(f"OneBot {action} 未返回有效对象")
        if data.get("status") not in (None, "ok") or data.get("retcode", 0) != 0:
            raise NapCatError(f"OneBot {action} 返回失败")
        if "data" in data and not isinstance(data["data"], dict):
            raise NapCatError(f"OneBot {action} 的 data 非对象")
        return data

    async def get_status(self) -> dict[str, Any]:
        return await self._query("get_status")

    async def get_login_info(self) -> dict[str, Any]:
        return await self._query("get_login_info")

    async def get_version_info(self) -> dict[str, Any]:
        return await self._post("/get_version_info")

    async def get_online_clients(self) -> dict[str, Any]:
        return await self._post("/get_online_clients")

    async def webui_quick_login(self) -> dict[str, Any]:
        """尝试通过 NapCat WebUI 触发快速登录（需已保存会话）。"""
        if not self.webui_base:
            raise NapCatUnavailable("未配置 NapCat WebUI 地址")
        for ep in ("/api/auth/login", "/api/QQLogin/QuickLogin"):
            try:
                return await self._post(ep, {}, use_webui=True)
            except NapCatError as exc:
                logger.debug(f"[guardian] WebUI 快速登录 {ep} 失败: {exc}")
        raise NapCatError("WebUI 快速登录不可用（可能需要人工扫码）")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
