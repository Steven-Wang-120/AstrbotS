# B05 · 服务器模式：QQ 掉线与 NapCat 重登

> 上游契约：B00 §3/§4/§5/§6/§7/§10。关联：B04（容器动作）、B06（资源）、B08（通知）、B12（安全）、C05（验收）。版本 v0.1.0 | 2026-10-13 | 模式 `server` | 默认重登方式 `restart_container`

## 1. 目标与范围

| 编号 | 目标 | 判据 |
| --- | --- | --- |
| G1 | 可靠判定 QQ 是否在线 | 单条证据失效时不误判（三条证据加权） |
| G2 | 掉线后自动重登 | `qq.relogin_cooldown` 之后 1 次尝试内恢复 |
| G3 | 抖动抑制 | 连续 N 次失败才判 OFFLINE，网络抖动不触发动作 |
| G4 | 重登失败可升级 | 升级路径：重启 → 告警 → `maintenance` → 人工扫码 |
| G5 | 凭证安全 | `qq.access_token` 永不明文入日志/事件/`raw` |

范围内：`get_status`/`get_login_info`/`get_version_info` 探测、三条证据链、重登动作（`restart_container` / `webui_quick_login` / `disabled`）、冷却与熔断、状态机。范围外：账号密码托管、验证码识别、WebUI 前端、消息内容审计。

## 2. QQ 在线判定的三条独立证据链

| 证据 | 来源 | 语义 | 失效场景（为什么单条不可靠） |
| --- | --- | --- | --- |
| E1 协议在线 | `POST /get_status` → `data.online` | OneBot 侧 QQ 会话是否在线 | HTTP 服务存活但 QQ 已掉线时仍返回 200；反向：HTTP 抖动导致误判掉线 |
| E2 登录态有效 | `POST /get_login_info` → `data.user_id` | 是否有有效登录账号 | 未登录时可能返回 `user_id=0` 或空；某些版本仍返回上次缓存值 |
| E3 宿主侧连接 | AstrBot 平台适配器 `connected` + 插件自记 `last_event_age` | 事件链路是否真正通 | 适配器可能未暴露 `connected`；无消息时 `last_event_age` 天然很大（静默期不等于掉线） |

**结论**：E1 反映协议端自述状态、E2 反映登录凭据有效性、E3 反映端到端事件链路。三者相互独立（不同接口/不同故障域），加权后单条异常不足以判定掉线；但 E1 与 E2 **同时**为否时可跳过抖动抑制直接判定掉线（硬证据）。

加权规则（权重定义于 `probes/qq_login.py`）：

| 情形 | 得分 | 判定 |
| --- | --- | --- |
| `online=true` | +0.50 | — |
| `user_id > 0` | +0.30 | — |
| E3 `connected=true`，或 `connected` 不可读但 `last_event_age < 300s` | +0.20 | — |
| score ≥ 0.5 | — | ONLINE |
| 0.2 ≤ score < 0.5 | — | SUSPECT（只记不动作） |
| score < 0.2 或 E1&E2 双否 | — | OFFLINE 候选（进入抖动抑制计数） |

**对 `core/policy.py::DEFAULT_RULES` 的贡献（B03 §5 已含 `qq_login` 基线规则，此处补充）**

| 探针 | metric | op | threshold | status | severity | action_kind |
| --- | --- | --- | --- | --- | --- | --- |
| `qq_login` | `online` | `==` | 0 | CRIT | CRIT | `qq_relogin` |
| `qq_login` | `fail_streak` | `>=` | 2 | WARN | WARN | — |
| `qq_login` | `fail_streak` | `>=` | 3 | CRIT | CRIT | `qq_relogin` |

> `metrics["online"]` 是**抖动抑制后**的结论（`streak < OFFLINE_CONSEC` 时为 1），因此观察期（`fail_streak=1~2`）不会挂载 `qq_relogin` 动作；`raw_online` 保留 E1 原始值供排障。探针只产出指标，动作由 PolicyEngine 推导（B03 §5）。

## 3. `probes/qq_login.py`（完整代码）

> 接口约定同 B04 §3：继承 `probes.base.Probe`、实现 `_check()`、`@register_probe("qq_login")` 注册；`deps` 提供 `docker` / `state` / `log` / `context`（可选）。

```python
# src/astrbot_plugin_guardian/probes/qq_login.py
from __future__ import annotations
import asyncio, time
from ..core.models import ProbeResult, Severity, Status
from ..core.registry import register_probe
from ..utils.napcat import NapCatClient, NapCatError, NapCatUnavailable
from .base import Probe
SUSPECT_CONSEC, OFFLINE_CONSEC = 2, 3        # 抖动抑制：连续 N 次失败才升级
W_STATUS, W_LOGIN, W_ADAPTER = 0.50, 0.30, 0.20
ADAPTER_EVENT_FRESH_SEC = 300
@register_probe("qq_login")
class QqLoginProbe(Probe):
    """QQ 在线探针：并发采集三条证据链，加权判定 + 连续失败计数抑制抖动。"""
    name = "qq_login"
    def __init__(self, config, deps, target: str = "qq") -> None:
        super().__init__(config, deps, target=target)
        self.state, self.log = deps["state"], deps.get("log")
    async def _check(self) -> ProbeResult:
        ts = time.time()
        napcat = NapCatClient(self.config.qq_base, self.config.qq_token, logger=self.log)
        try:
            e1, e2, e3 = await asyncio.gather(
                self._e1_status(napcat), self._e2_login(napcat), self._e3_adapter(),
                return_exceptions=False)
        finally:
            await napcat.close()
        score, hard_offline = self._score(e1, e2, e3)
        streak = await self._bump_streak(score, hard_offline)
        metrics = {"online": int(streak < OFFLINE_CONSEC),   # 抖动抑制后的结论，供 PolicyEngine 判定
                   "raw_online": int(bool(e1.get("online"))), "user_id": e2.get("user_id", 0),
                   "nickname": e2.get("nickname", ""), "last_event_age": e3.get("last_event_age"),
                   "adapter_connected": e3.get("connected"), "score": score, "fail_streak": streak,
                   "evidence": {"e1": e1, "e2": e2, "e3": e3}}
        # 安全：raw 仅保留结构化证据与基址，绝不写入 access_token
        raw = {"base": self.config.qq_base, "e1": e1, "e2": e2, "e3": e3}
        if streak >= OFFLINE_CONSEC:
            return self._r(ts, Status.CRIT, Severity.CRIT,
                           f"QQ 判定掉线（连续 {streak} 次失败，score={score}，hard={hard_offline}）",
                           {**metrics, "suggested": "qq_relogin"}, raw)
        if streak >= SUSPECT_CONSEC:
            return self._r(ts, Status.WARN, Severity.WARN,
                           f"QQ 疑似异常（连续 {streak} 次，score={score}），观察中不动作",
                           {**metrics, "suggested": None}, raw)
        if score >= 0.5:
            return self._r(ts, Status.OK, Severity.INFO,
                           f"QQ 在线（uid={metrics['user_id']} nick={metrics['nickname'] or '-'}）",
                           {**metrics, "suggested": None}, raw)
        return self._r(ts, Status.WARN, Severity.WARN,
                       f"QQ 状态不明确（score={score}），计数 {streak}/{OFFLINE_CONSEC}",
                       {**metrics, "suggested": None}, raw)
    # ---------- 证据链 ----------
    async def _e1_status(self, napcat: NapCatClient) -> dict:
        try:
            data = await napcat.get_status()
            return {"ok": True, "online": bool(data.get("online")), "good": bool(data.get("good"))}
        except (NapCatUnavailable, NapCatError) as exc:
            return {"ok": False, "error": str(exc)[:160]}
    async def _e2_login(self, napcat: NapCatClient) -> dict:
        try:
            data = await napcat.get_login_info()
            uid = int(data.get("user_id") or 0)
            return {"ok": True, "user_id": uid, "nickname": str(data.get("nickname") or ""), "valid": uid > 0}
        except (NapCatUnavailable, NapCatError) as exc:
            return {"ok": False, "user_id": 0, "error": str(exc)[:160]}
    async def _e3_adapter(self) -> dict:
        """证据链③：适配器连接状态（可读则用）+ 插件自记的最近事件时间（兜底）。"""
        last = float(await self.state.get_kv("qq.last_event_ts", 0.0))
        age = round(time.time() - last, 1) if last > 0 else None
        connected = None
        try:
            for inst in (self.deps.get("context") or object()).get_platform_insts():
                if "aiocqhttp" in type(inst).__name__.lower() or "onebot" in type(inst).__name__.lower():
                    connected = bool(getattr(inst, "connected", False))
                    break
        except Exception:
            connected = None                      # API 不可用 -> 降级为 None，仅用 last_event_age
        return {"ok": True, "connected": connected, "last_event_age": age}
    # ---------- 加权与计数 ----------
    @staticmethod
    def _score(e1: dict, e2: dict, e3: dict) -> tuple[float, bool]:
        score = 0.0
        if e1.get("ok") and e1.get("online"): score += W_STATUS
        if e2.get("ok") and e2.get("valid"): score += W_LOGIN
        if e3.get("connected") is True: score += W_ADAPTER
        elif e3.get("connected") is None:
            age = e3.get("last_event_age")
            if age is not None and age < ADAPTER_EVENT_FRESH_SEC: score += W_ADAPTER
        hard = bool(e1.get("ok") and e2.get("ok") and not e1.get("online") and not e2.get("valid"))
        return round(score, 2), hard
    async def _bump_streak(self, score: float, hard_offline: bool) -> int:
        key = "probe.qq_login"
        prev = await self.state.get_kv(key, {"fail_streak": 0, "state": "ONLINE"})
        streak = int(prev.get("fail_streak", 0))
        if hard_offline: streak = max(streak, OFFLINE_CONSEC)      # 双硬证据一致：跳过抑制
        elif score >= 0.5: streak = 0
        else: streak += 1
        state = ("OFFLINE" if streak >= OFFLINE_CONSEC else "SUSPECT" if streak >= SUSPECT_CONSEC else "ONLINE")
        await self.state.set_kv(key, {"fail_streak": streak, "state": state, "ts": time.time()})
        return streak
    def _r(self, ts, status, sev, msg, metrics, raw) -> ProbeResult:
        return ProbeResult(probe=self.name, target="qq", status=status,
                           severity=sev, message=msg, metrics=metrics, ts=ts, raw=raw)
```

> `qq.last_event_ts` 由 `main.py` 的全局消息钩子更新（收到任意事件即刷新），是 E3 的兜底来源，不依赖 AstrBot 内部 API 稳定性。

## 4. `utils/napcat.py`（完整代码）

```python
# src/astrbot_plugin_guardian/utils/napcat.py
from __future__ import annotations
import logging
from typing import Any
import aiohttp
DEFAULT_TIMEOUT = 5.0
WEBUI_DEFAULT_PORT = 6099
class NapCatUnavailable(Exception):
    """HTTP 层不可达 / 超时 / 401 / 403：协议端进程或网络问题。"""
class NapCatError(Exception):
    """HTTP 200 但 retcode != 0：OneBot 业务层错误（如未登录）。"""
    def __init__(self, retcode: int, message: str, endpoint: str = "") -> None:
        super().__init__(f"retcode={retcode} {endpoint} {message}")
        self.retcode, self.message, self.endpoint = retcode, message, endpoint
def mask_token(token: str) -> str:
    """日志脱敏：只保留前 2 位与长度，绝不输出完整凭证。"""
    return "<empty>" if not token else f"{token[:2]}***({len(token)})"
class NapCatClient:
    """OneBot11 HTTP 客户端（NapCat 实现）。默认基址 http://napcat:3000，Bearer 鉴权。"""
    def __init__(self, base_url: str, access_token: str = "", timeout: float = DEFAULT_TIMEOUT,
                 logger: logging.Logger | None = None) -> None:
        self.base = (base_url or "http://napcat:3000").rstrip("/")
        self.token = access_token or ""
        self.timeout = float(timeout)
        self.log = logger or logging.getLogger("guardian.napcat")
        self._session: aiohttp.ClientSession | None = None
    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"      # 仅入请求头，不入日志
            self._session = aiohttp.ClientSession(
                headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session
    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
    async def call(self, action: str, payload: dict | None = None) -> Any:
        """统一 POST /{action}。401/403 -> NapCatUnavailable；retcode!=0 -> NapCatError。"""
        session = await self._session_get()
        url = f"{self.base}/{action}"
        try:
            async with session.post(url, json=payload or {}) as resp:
                if resp.status in (401, 403):
                    raise NapCatUnavailable(f"鉴权失败 HTTP {resp.status}（token={mask_token(self.token)}）")
                if resp.status >= 400:
                    raise NapCatUnavailable(f"HTTP {resp.status} {action}")
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise NapCatUnavailable(f"{type(exc).__name__} {action}: {exc}") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise NapCatUnavailable(f"响应解析失败 {action}: {exc}") from exc
        if not isinstance(data, dict):
            raise NapCatUnavailable(f"响应格式异常 {action}")
        retcode = int(data.get("retcode", 0) or 0)
        if retcode != 0:
            raise NapCatError(retcode, str(data.get("message") or data.get("msg") or ""), action)
        return data.get("data")
    async def get_status(self) -> dict: return await self.call("get_status") or {}
    async def get_login_info(self) -> dict: return await self.call("get_login_info") or {}
    async def get_version_info(self) -> dict: return await self.call("get_version_info") or {}
    async def get_online_clients(self) -> dict: return await self.call("get_online_clients") or {}
    async def get_group_list(self) -> list: return await self.call("get_group_list") or []
    @property
    def webui_base(self) -> str:
        """从 OneBot 基址推导 NapCat WebUI 地址（默认端口 6099），避免新增配置键。"""
        host = self.base.split("//")[-1].split("/")[0].split(":")[0]
        return f"http://{host}:{WEBUI_DEFAULT_PORT}"
    async def webui_quick_login(self, webui_token: str) -> tuple[bool, str]:
        """触发 WebUI 快速登录：先取 WebUI 会话凭据，再调用快速登录接口。"""
        session = await self._session_get()
        try:
            async with session.post(f"{self.webui_base}/api/auth/login",
                                    json={"token": webui_token}) as resp:
                if resp.status >= 400:
                    return False, f"WebUI 鉴权失败 HTTP {resp.status}（token={mask_token(webui_token)}）"
                auth = await resp.json(content_type=None)
            cred = str((auth or {}).get("token") or (auth or {}).get("Credential") or webui_token)
            async with session.post(f"{self.webui_base}/api/QQLogin/SetQuickLogin",
                                    headers={"Authorization": f"Bearer {cred}"}, json={}) as resp:
                if resp.status >= 400:
                    return False, f"快速登录接口 HTTP {resp.status}"
                body = await resp.json(content_type=None)
            code = int((body or {}).get("code", 0) or 0)
            return (True, "已触发快速登录") if code == 0 else (False, f"快速登录返回 code={code}")
        except aiohttp.ClientError as exc:
            return False, f"WebUI 不可达：{type(exc).__name__}"
```

## 5. 重登策略矩阵

| 策略 | 配置值 `qq.relogin_method` | 机制 | 前置条件 | 优点 | 风险 / 代价 |
| --- | --- | --- | --- | --- | --- |
| 重启协议端（默认） | `restart_container` | `RestartContainerAction` 重启 `napcat` 容器；容器启动时 NapCat 若存在已保存会话则自动快速登录 | `napcat` ∈ 动作白名单；有有效会话缓存 | 最可靠、无需额外接口；进程状态彻底重置 | 重启期间消息丢失；会话失效时仍需扫码 |
| WebUI 快速登录 | `webui_quick_login` | 调用 NapCat WebUI（默认 6099）触发 `SetQuickLogin` | WebUI 可达且 token 正确；会话仍有效 | 不重启进程，秒级恢复 | 依赖 WebUI 稳定性；token 需单独配置 |
| 只告警 | `disabled` | 不动作，仅推送事件 | — | 零副作用，适合手工运维 | 无法自愈 |

选择建议：默认 `restart_container`（B00 §15 R3 明确「优先带会话重启」）；容器编排禁止重启协议端时改用 `webui_quick_login`。

> 使用 `webui_quick_login` 需提供 WebUI token：本键为 B00 §7 的**可选增补项** `qq.webui_token`（未配置时回退 `qq.access_token`），不改变既有契约；WebUI 基址由 `qq.onebot_http_base` 的主机名 + 端口 6099 推导（`NapCatClient.webui_base`），不新增配置键。

## 6. `actions/qq_actions.py`（完整代码）

动作由 `HealthEngine._actions["qq_relogin"]` 调用（B03 §6）；构造签名为 `(config, deps)`，`deps` 提供 `docker` / `state` / `log`。

```python
# src/astrbot_plugin_guardian/actions/qq_actions.py
from __future__ import annotations
import json, time
from ..core.models import ActionResult, ActionSpec
from ..utils.napcat import NapCatClient, mask_token
VERIFY_WAIT_SEC = 20          # 重登后等待再复核
VERIFY_POLL_SEC = 5.0
ATTEMPT_ESCALATE = 3          # 连续尝试失败 N 次 -> 升级
class QqReloginAction:
    """QQ 重登动作：冷却 + 每小时熔断 + 尝试次数升级 + 重登后复核。"""
    kind = "qq_relogin"
    def __init__(self, config, deps) -> None:
        self.config, self.docker, self.state = config, deps["docker"], deps["state"]
        self.log = deps.get("log")
    async def execute(self, spec: ActionSpec) -> ActionResult:
        t0 = time.perf_counter()
        ms = lambda: int((time.perf_counter() - t0) * 1000)
        method = self.config.qq_relogin_method
        if spec.dry_run:
            return ActionResult(ok=True, detail=f"[dry-run] 将按 method={method} 重登 QQ", elapsed_ms=ms())
        # 1) 冷却
        last = float(await self.state.get_kv("action.last_relogin", 0.0))
        cooldown = int(self.config.qq_relogin_cooldown)
        if time.time() - last < cooldown:
            return ActionResult(ok=False, detail=f"重登处于冷却期（剩余 {int(cooldown - (time.time() - last))}s）",
                                elapsed_ms=ms())
        # 2) 每小时熔断
        hour = await self._relogin_marks()
        if len(hour) >= int(self.config.qq_max_relogin_per_hour):
            await self._bump_attempts(fail=True)
            return ActionResult(ok=False, detail=f"每小时重登次数已达上限 {self.config.qq_max_relogin_per_hour}，升级人工",
                                elapsed_ms=ms(), output=json.dumps({"marks": len(hour)}))
        await self._relogin_marks(append=True)
        # 3) 分支执行
        if method == "disabled":
            return ActionResult(ok=False, detail="qq.relogin_method=disabled：仅告警，不执行重登", elapsed_ms=ms())
        if method == "webui_quick_login":
            napcat = NapCatClient(self.config.qq_base, self.config.qq_token, logger=self.log)
            try:
                ok, detail = await napcat.webui_quick_login(self.config.qq_webui_token)
            finally:
                await napcat.close()
            if not ok:
                await self._bump_attempts(fail=True)
                return ActionResult(ok=False, detail=f"WebUI 快速登录失败：{detail}", elapsed_ms=ms())
        else:  # restart_container（默认）
            try:
                await self.docker.restart(self.config.container_napcat, timeout=10)
            except Exception as exc:
                await self._bump_attempts(fail=True)
                return ActionResult(ok=False, detail=f"重启 napcat 失败：{type(exc).__name__} {exc}", elapsed_ms=ms())
        # 4) 复核：等待后重新探测 get_login_info
        ok, info = await self._verify_login()
        await self.state.set_kv("action.last_relogin", time.time())
        if ok:
            await self._bump_attempts(fail=False)
            await self.state.set_kv("probe.qq_login", {"fail_streak": 0, "state": "ONLINE", "ts": time.time()})
            return ActionResult(ok=True, detail=f"重登成功（method={method}，uid={info.get('user_id')}）",
                                elapsed_ms=ms(), output=json.dumps(info, ensure_ascii=False))
        attempts = await self._bump_attempts(fail=True)
        return ActionResult(ok=False,
                            detail=f"重登后复核失败（第 {attempts} 次尝试，method={method}）"
                                   + ("；已达升级阈值，建议人工扫码" if attempts >= ATTEMPT_ESCALATE else ""),
                            elapsed_ms=ms(), output=json.dumps(info, ensure_ascii=False))
    async def _verify_login(self) -> tuple[bool, dict]:
        import asyncio
        await asyncio.sleep(VERIFY_WAIT_SEC)
        deadline = time.monotonic() + 30
        last: dict = {}
        while time.monotonic() < deadline:
            napcat = NapCatClient(self.config.qq_base, self.config.qq_token, logger=self.log)
            try:
                data = await napcat.get_login_info()
                last = {"user_id": int(data.get("user_id") or 0), "nickname": str(data.get("nickname") or "")}
                if last["user_id"] > 0:
                    return True, last
            except Exception as exc:
                last = {"error": type(exc).__name__}
            finally:
                await napcat.close()
            await asyncio.sleep(VERIFY_POLL_SEC)
        return False, last
    async def _relogin_marks(self, append: bool = False) -> list:
        """维护 1 小时窗口内的重登时间戳（熔断依据）。"""
        now = time.time()
        marks = [t for t in await self.state.get_kv("qq.relogin_marks", []) if now - t < 3600]
        if append:
            marks.append(now)
        await self.state.set_kv("qq.relogin_marks", marks)
        return marks
    async def _bump_attempts(self, fail: bool) -> int:
        n = int(await self.state.get_kv("qq.relogin_attempts", 0))
        n = n + 1 if fail else 0
        await self.state.set_kv("qq.relogin_attempts", n)
        return n
```

## 7. 掉线判定的抖动抑制

| 层级 | 机制 | 参数 | 效果 |
| --- | --- | --- | --- |
| L1 证据加权 | 三条证据独立打分，非布尔与 | 权重 0.5/0.3/0.2 | 单条接口抖动不足以判掉线 |
| L2 连续计数 | `fail_streak` 持久化于 StateStore | `SUSPECT_CONSEC=2`、`OFFLINE_CONSEC=3` | 需连续 3 次（≈3×间隔）失败才动作 |
| L3 硬证据直通 | E1&E2 双否时 `streak` 直接置为 `OFFLINE_CONSEC` | — | 明确掉线无需等待 3 轮 |
| L4 动作冷却 | `qq.relogin_cooldown=300` | 300s | 避免重登风暴 |
| L5 熔断 | `qq.max_relogin_per_hour=4` | 4 次/小时 | 超限升级 FATAL + 人工 |
| L6 成功复位 | 复核成功后 `fail_streak=0`、`state=ONLINE` | — | 事件闭环 |

```
E1=ok,E2=ok,E3=ok  ->  score 1.00  -> streak=0  -> ONLINE
E1=err,E2=ok,E3=ok ->  score 0.50  -> streak=0  -> ONLINE（容忍 E1 抖动）
E1=ok(false),E2=ok(false) -> hard   -> streak=3  -> OFFLINE（立即）
E1=err,E2=err,E3=err -> score 0.00 -> streak 1->2->3 -> OFFLINE（第 3 轮）
```

## 8. 重登后复核

| 步骤 | 动作 | 判定 |
| --- | --- | --- |
| 等待 | `sleep(VERIFY_WAIT_SEC=20s)` 等协议端完成启动/登录 | — |
| 轮询 | 最多 30s，每 5s 调用 `get_login_info` | `user_id > 0` → 成功 |
| 成功 | 复位 `fail_streak=0`、`state=ONLINE`；关闭 incident；推送「重登成功」 | `ActionResult.ok=true` |
| 失败 | `attempts += 1`；写审计；推送失败详情 | `ActionResult.ok=false` |
| 阈值 | `attempts >= 3` 时提示人工扫码并进入升级路径 | 见 §9 |

## 9. 失败升级路径

```
重登失败（attempts=1）
  └─ 记录 + 告警（WARN），等待下一轮巡检（冷却后重试）
重登失败（attempts=2）
  └─ 告警升级为 CRIT，附诊断：get_version_info / get_online_clients 摘要
重登失败（attempts>=3）或每小时熔断命中
  └─ 进入 maintenance 档位（暂停自动动作，B00 §10.6）
     └─ FATAL 通知：包含「请人工在 NapCat WebUI 扫码登录」与 WebUI 地址
        └─ 人工恢复后：/guardian profile normal 或 /guardian check 触发复位
```

| 触发条件 | 级别 | 自动动作 | 人工介入 |
| --- | --- | --- | --- |
| 首次失败 / 连续 2 次失败 | WARN → CRIT | 冷却后重试 | 否 |
| 连续 ≥3 次失败 或 每小时 > 4 次 | FATAL | 熔断并切 `maintenance`，停止自动重登 | 是（扫码） |

## 10. `access_token` 安全

| 规则 | 实现 |
| --- | --- |
| 只从配置读取 | `config.qq_token`（`qq.access_token`），不接受 LLM/命令传入 |
| 日志脱敏 | `mask_token()` 输出 `ab***(N)`；`NapCatUnavailable` 文案已脱敏 |
| 不入事件与 `raw` | `ProbeResult.raw` 仅含 `base`/`e1`/`e2`/`e3`；`Incident.message` 不含 token |
| 不入审计 | 审计仅记 `kind/target/result.ok/detail`，`detail` 已由 `mask_token` 处理 |
| 请求头承载 | 仅 `Authorization: Bearer <token>`，不放入 query string（避免 URL 落日志） |
| 空 token 处理 | 未配置时不发送 Authorization 头，OneBot 侧需相应放开鉴权 |

## 11. 时间线与状态机

```
ONLINE ──(score<0.5, streak=1)──► SUSPECT ──(streak=2)──► SUSPECT
   ▲                                  │                      │
   │                                  │ (score>=0.5)         │ (streak>=3 或 hard)
   └──────────────────────────────────┘                      ▼
                                                        OFFLINE
                                                           │
                              SafetyGuard.authorize(qq_relogin)
                                    ├─ deny ──► SUPPRESSED（审计 + 通知）
                                    └─ allow ─► RELOGIN_IN_PROGRESS
                                                     │
                              ┌──────────────────────┴──────────────────────┐
                              ▼                                             ▼
                    复核成功(user_id>0)                          复核失败 / attempts>=3
                              │                                             │
                          ONLINE                                    OFFLINE_HARD
                     （streak=0, 事件闭环）                    （maintenance + FATAL 通知人工扫码）
```

```
t0        QqLoginProbe 连续第 3 次失败 -> CRIT + suggested=qq_relogin
t0        SafetyGuard 授权（冷却/熔断/dry_run 通过）
t0        QqReloginAction: method=restart_container -> POST /containers/napcat/restart?t=10
t0+20s    sleep(VERIFY_WAIT_SEC)
t0+25s    get_login_info -> user_id=10001 -> ok=true
t0+25s    复位 streak=0、state=ONLINE；关闭 incident；推送「重登成功」
```

## 12. 验收标准（对应 C05）

- [ ] C05-01 三条证据链并发采集，单次 `check()` 总耗时 < `qq` 超时之和（≤ 6s，含并发）。
- [ ] C05-02 `get_status` 返回 `online=false` 且 `get_login_info` 返回 `user_id=0` 时，首轮即判 OFFLINE（硬证据直通）。
- [ ] C05-03 仅 `get_status` 超时而 `get_login_info` 正常时，判定仍为 ONLINE（score=0.5）。
- [ ] C05-04 模拟单轮网络抖动（E1/E2/E3 全失败 1 次）不触发动作，仅 `fail_streak=1`。
- [ ] C05-05 连续 3 轮全失败后产生 CRIT 事件且 `metrics.suggested=qq_relogin`。
- [ ] C05-06 `dry_run=true` 时 `QqReloginAction` 返回 `[dry-run]`，容器与登录态零变化。
- [ ] C05-07 `relogin_method=restart_container` 时 `napcat` 容器被重启且 60s 内复核 `user_id>0`。
- [ ] C05-08 冷却期内重复触发返回 `ok=false` 且 `detail` 含剩余秒数。
- [ ] C05-09 1 小时内第 5 次重登被熔断，返回 FATAL 级事件并提示人工。
- [ ] C05-10 连续 3 次重登失败后档位切换为 `maintenance`，自动动作暂停。
- [ ] C05-11 `access_token` 不出现在任何日志行、`ProbeResult.raw`、`Incident.message`、审计记录中。
- [ ] C05-12 401/403 被归类为 `NapCatUnavailable` 并计入 `fail_streak`，不误判为 ONLINE。
- [ ] C05-13 `relogin_method=disabled` 时仅产生告警，不产生任何容器或 WebUI 调用。
