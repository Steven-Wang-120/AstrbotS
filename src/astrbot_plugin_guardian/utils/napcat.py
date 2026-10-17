"""Guardian 守护系统 —— NapCat / OneBot 11 HTTP 客户端。

对应文档：B05 §2、B17 §6

两条通道，职责严格分开：

- **OneBot 11 HTTP**（`base`，默认 http://napcat:3000）：登录态探针用，
  Bearer `access_token` 鉴权，返回 `{status, retcode, data}` 信封。
- **NapCat WebUI**（`webui_base`，默认 :6099）：登录恢复用，先拿 `webui_token`
  换 `Credential`（JWT），再用 `Authorization: Bearer <credential>` 调
  `/api/QQLogin/*`，返回 `{code, data, message}` 信封。

两条信封的失败语义不同，**不能共用一个"成功"判据**：

- OneBot：`retcode != 0` 或 `status != "ok"` 即失败；
- WebUI：`code != 0` 即失败；但 `PasswordLogin` 是例外——验证码/新设备
  标记在 `data` 里而 `code` 仍是 0（B17 §6.5，最容易写错的一处）。

`webui.json` 的 `loginRate: 3` 是**每分钟 3 次**的登录频率限制，所以 Credential
必须缓存；每次调用都换会把自己锁在门外。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from ..core.log import logger


class NapCatError(RuntimeError):
    pass


class NapCatUnavailable(NapCatError):
    """网络不可达 / 超时。"""


class NapCatAuthError(NapCatError):
    """鉴权失败（401/403 / Credential 换不到）。"""


#: `PasswordLogin` 会真的走一遍 QQ 登录协议，比普通 API 慢得多。
#: 用 `request_timeout`（默认 8s）会把"正在登录"误判成"不可达"。
PASSWORD_LOGIN_TIMEOUT = 30.0


class NapCatClient:
    def __init__(self, base: str, token: str = "", timeout: float = 8.0,
                 webui_base: str = "", webui_token: str = "",
                 action_caller: Callable[[str], Awaitable[Any]] | None = None,
                 account: str = "") -> None:
        self.base = (base or "").rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self.webui_base = (webui_base or "").rstrip("/")
        self.webui_token = webui_token or token or ""
        self.account = str(account or "").strip()
        self._session: aiohttp.ClientSession | None = None
        self._action_caller = action_caller
        #: B17 §6.2：Credential 缓存。空串表示"还没换/已失效"。
        self._credential: str = ""

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session

    def _headers(self, token: str = "") -> dict[str, str]:
        tok = token or self.token
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    # ------------------------------------------------------------------ #
    # 传输层
    # ------------------------------------------------------------------ #
    async def _http_post(self, url: str, payload: dict | None = None,
                         headers: dict[str, str] | None = None,
                         timeout: float | None = None) -> tuple[int, str]:
        """发一次 POST，返回 `(status, body_text)`。

        只把**网络层**失败转成 `NapCatUnavailable`；HTTP 状态码与业务码交给
        调用方判断——两条信封对"失败"的定义不一样。
        """
        session = await self._ensure_session()
        kwargs: dict[str, Any] = {"json": payload or {}, "headers": headers or {}}
        if timeout is not None:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        try:
            async with session.post(url, **kwargs) as resp:
                return resp.status, await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise NapCatUnavailable(f"NapCat 不可达({url.split('?')[0]}): {exc}") from exc

    @staticmethod
    def _parse_json(body: str, endpoint: str) -> dict[str, Any]:
        if not body:
            return {}
        try:
            data = json.loads(body)
        except Exception as exc:
            raise NapCatError(f"NapCat 返回畸形 JSON: {body[:120]}") from exc
        return data if isinstance(data, dict) else {"data": data}

    async def _post(self, endpoint: str, payload: dict | None = None,
                    *, use_webui: bool = False) -> dict[str, Any]:
        """OneBot 风格 POST（`use_webui=True` 时走 WebUI 地址 + token 鉴权）。

        **B17 §6.3**：HTTP 200 但业务码非 0 必须抛错。旧实现把 `{"code":-1}`
        当成功返回，正是 `webui_quick_login` 假成功的根源（缺陷 P2）。
        """
        base = self.webui_base if use_webui else self.base
        if not base:
            raise NapCatUnavailable("未配置 NapCat 地址")
        url = f"{base}{endpoint}"
        headers = self._headers(self.webui_token if use_webui else self.token)
        status, body = await self._http_post(url, payload, headers)
        if status in (401, 403):
            raise NapCatAuthError(f"NapCat 鉴权失败({status})")
        if status >= 400:
            raise NapCatError(f"NapCat {endpoint} -> {status}: {body[:200]}")
        data = self._parse_json(body, endpoint)
        code = data.get("code")
        if code is not None and code != 0:
            raise NapCatError(
                f"NapCat {endpoint} 返回失败(code={code}): {data.get('message') or '-'}")
        return data

    # ------------------------------------------------------------------ #
    # WebUI：Credential 换取与缓存（B17 §6.2）
    # ------------------------------------------------------------------ #
    def _auth_hash(self) -> str:
        return hashlib.sha256(f"{self.webui_token}.napcat".encode()).hexdigest()

    async def auth_login(self, *, force: bool = False) -> str:
        """用 `webui_token` 换 Credential；带缓存。返回 Credential 字符串。"""
        if self._credential and not force:
            return self._credential
        if not self.webui_base:
            raise NapCatUnavailable("未配置 NapCat WebUI 地址")
        if not self.webui_token:
            raise NapCatAuthError("未配置 qq.webui_token，无法换取 Credential")
        status, body = await self._http_post(
            f"{self.webui_base}/api/auth/login", {"hash": self._auth_hash()}, {})
        if status in (401, 403):
            raise NapCatAuthError(f"NapCat WebUI token 无效({status})")
        if status >= 400:
            raise NapCatError(f"NapCat /api/auth/login -> {status}: {body[:200]}")
        data = self._parse_json(body, "/api/auth/login")
        if data.get("code") != 0:
            raise NapCatAuthError(
                f"Credential 换取失败: {data.get('message') or '未知错误'}")
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        credential = str(inner.get("Credential") or "")
        if not credential:
            raise NapCatAuthError("Credential 换取失败: 响应缺少 data.Credential")
        self._credential = credential
        logger.debug(f"[guardian] NapCat Credential 已更新（len={len(credential)}）")
        return credential

    def reset_credential(self) -> None:
        """丢弃缓存（配置热更新 / 登出后调用）。"""
        self._credential = ""

    @staticmethod
    def _is_auth_failure(resp: dict[str, Any]) -> bool:
        """业务层鉴权失败。

        NapCat 在 Credential 失效（典型场景：容器重启后旧 JWT 作废）时返回的是
        **HTTP 200 + `{"code":-1,"message":"Unauthorized"}`**，不是 401。只看 HTTP
        状态码会把它当成"正常响应"，于是 `CheckLoginStatus` 被读成
        `isLogin=false / qrcodeurl=""`——动作会以为"QQ 还没到登录页"而空转到超时，
        永远不去重换 Credential（K-03 实机发现）。
        """
        code = resp.get("code")
        if code in (401, 403):
            return True
        if code in (None, 0):
            return False
        return "unauthor" in str(resp.get("message") or "").lower()

    async def _webui_call(self, endpoint: str, payload: dict | None = None, *,
                          timeout: float | None = None,
                          retry_auth: bool = True) -> dict[str, Any]:
        """调 WebUI 接口，返回原始信封；**不**把业务失败码当异常。

        `PasswordLogin` 需要读到失败原因（"密码错误"）和 `data` 里的验证码标记，
        所以业务码判断留给调用方。鉴权失败（HTTP 401/403 或业务层 Unauthorized）
        自动清缓存重换一次（**只一次**）。
        """
        if not self.webui_base:
            raise NapCatUnavailable("未配置 NapCat WebUI 地址")
        url = f"{self.webui_base}{endpoint}"
        attempts = 2 if retry_auth else 1
        for attempt in range(attempts):
            cred = await self.auth_login()
            status, body = await self._http_post(
                url, payload, {"Authorization": f"Bearer {cred}"}, timeout=timeout)
            if status in (401, 403):
                self._credential = ""
                if attempt + 1 < attempts:
                    logger.debug(f"[guardian] {endpoint} 返回 {status}，重换 Credential 后重试一次")
                    continue
                raise NapCatAuthError(f"NapCat WebUI 鉴权失败({status})")
            if status >= 400:
                raise NapCatError(f"NapCat {endpoint} -> {status}: {body[:200]}")
            resp = self._parse_json(body, endpoint)
            if self._is_auth_failure(resp):
                self._credential = ""
                if attempt + 1 < attempts:
                    logger.debug(f"[guardian] {endpoint} 业务层鉴权失败，重换 Credential 后重试一次")
                    continue
                raise NapCatAuthError(
                    f"NapCat WebUI 鉴权失败: {resp.get('message') or 'Unauthorized'}")
            return resp
        raise NapCatAuthError(f"NapCat WebUI 鉴权失败({endpoint})")  # pragma: no cover

    async def _webui_post(self, endpoint: str, payload: dict | None = None, *,
                          timeout: float | None = None) -> dict[str, Any]:
        """同 `_webui_call`，但 `code != 0` 直接抛 `NapCatError`。"""
        resp = await self._webui_call(endpoint, payload, timeout=timeout)
        code = resp.get("code")
        if code is not None and code != 0:
            raise NapCatError(
                f"NapCat {endpoint} 返回失败(code={code}): {resp.get('message') or '-'}")
        return resp

    # ------------------------------------------------------------------ #
    # OneBot 查询
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

    # ------------------------------------------------------------------ #
    # WebUI 登录能力（B17 §6.1）
    # ------------------------------------------------------------------ #
    async def check_login_status(self) -> dict[str, Any]:
        """`/api/QQLogin/CheckLoginStatus` → `{isLogin, isOffline, qrcodeurl, loginError}`。"""
        resp = await self._webui_call("/api/QQLogin/CheckLoginStatus", {})
        code = resp.get("code")
        if code is not None and code != 0:
            raise NapCatError(
                f"CheckLoginStatus 失败(code={code}): {resp.get('message') or '-'}")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        return {
            "isLogin": bool(data.get("isLogin")),
            "isOffline": bool(data.get("isOffline")),
            "qrcodeurl": str(data.get("qrcodeurl") or ""),
            "loginError": str(data.get("loginError") or ""),
        }

    async def get_qq_login_info(self) -> dict[str, Any]:
        """`/api/QQLogin/GetQQLoginInfo` → `{uin, nick, online}`。

        `online` 是**协议端自己认的在线态**，比 OneBot 的 `user_id` 非空更强：
        `PasswordLogin` 返回成功但账号其实没上线时，只有这里能看出来（B17 R2）。
        """
        resp = await self._webui_call("/api/QQLogin/GetQQLoginInfo", {})
        code = resp.get("code")
        if code is not None and code != 0:
            raise NapCatError(
                f"GetQQLoginInfo 失败(code={code}): {resp.get('message') or '-'}")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        return {
            "uin": str(data.get("uin") or ""),
            "nick": str(data.get("nick") or ""),
            "online": bool(data.get("online")),
        }

    async def password_login(self, uin: str, password_md5: str) -> dict[str, Any]:
        """账密登录。返回 `{'ok', 'kind', 'message', 'url'}`，**不抛业务异常**。

        `kind` ∈ `ok | captcha | new_device | failed`。

        ⚠️ B17 §6.5：`code == 0` 不等于成功。需要验证码/新设备时 `code` 仍是 0，
        标记在 `data.needCaptcha` / `data.needNewDevice` 里。只看 `code` 会把
        "要你输验证码"当成"登录成功"，然后在下一次复核里莫名其妙地失败。
        """
        resp = await self._webui_call(
            "/api/QQLogin/PasswordLogin",
            {"uin": str(uin), "passwordMd5": str(password_md5)},
            timeout=max(PASSWORD_LOGIN_TIMEOUT, self.timeout))
        if resp.get("code") != 0:
            return {"ok": False, "kind": "failed", "url": "",
                    "message": str(resp.get("message") or "登录失败")}
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        if data.get("needCaptcha"):
            return {"ok": False, "kind": "captcha", "url": str(data.get("proofWaterUrl") or ""),
                    "message": str(data.get("message") or "需要输入验证码")}
        if data.get("needNewDevice"):
            return {"ok": False, "kind": "new_device", "url": str(data.get("jumpUrl") or ""),
                    "message": str(data.get("message") or "需要新设备验证")}
        return {"ok": True, "kind": "ok", "url": "",
                "message": str(resp.get("message") or "success")}

    async def set_quick_login_qq(self, uin: str) -> dict[str, Any]:
        """`/api/QQLogin/SetQuickLoginQQ` —— 写 `webui.json.autoLoginAccount`。

        这是恢复阶梯的**闭环**：只有写回之后，下次容器重启才能靠 `-q` 自动登录。
        """
        return await self._webui_post("/api/QQLogin/SetQuickLoginQQ", {"uin": str(uin)})

    async def get_qrcode_url(self) -> str:
        """`/api/QQLogin/GetQQLoginQrcode` → 二维码**载荷 URL**（不是图片）。"""
        resp = await self._webui_post("/api/QQLogin/GetQQLoginQrcode", {})
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        return str(data.get("qrcode") or "")

    async def webui_quick_login(self) -> dict[str, Any]:
        """立即触发一次快速登录（不重启容器）。

        B17 §6.3 重写：旧实现打的是 `/api/QQLogin/QuickLogin`，该路由在
        NapCat 4.18.14 上是 **404**，所以这个策略从来没生效过。
        """
        uin = str(self.account or "").strip()
        if not uin:
            raise NapCatError("未配置 qq.account，无法快速登录")
        return await self._webui_post("/api/QQLogin/SetQuickLogin", {"uin": uin})

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
