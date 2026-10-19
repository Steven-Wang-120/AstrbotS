"""QQ 登录态：复用反向 WebSocket（或显式配置 HTTP）直接查询。

只认协议端的确定回答。无消息、请求失败、无效响应都不证明掉线。
UNKNOWN 会打断连续确认，不能跨不确定轮次或插件重载积累掉线计数。
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..core.models import Severity, Status
from ..core.registry import register_probe
from ..utils.napcat import NapCatAuthError, NapCatError, NapCatUnavailable
from .base import Probe


@register_probe("qq_login")
class QqLoginProbe(Probe):
    name = "qq_login"
    scope = "server"
    timeout = 12.0

    #: WebUI 证据链的独立超时（秒）。比探针总超时短得多——它只是补充证据，
    #: 不该把整轮巡检拖到超时。取值依据：`auth_login` 换凭据 + 一次业务请求
    #: 各算一次往返，4s 足够本机/同网络内的 NapCat 应答。
    WEBUI_EVIDENCE_TIMEOUT = 4.0

    #: 最近一次 WebUI 证据失败的原因（空串 = 无失败或未启用）。
    #: 异常在 `_webui_evidence` 里被收敛成 `None`，原因靠这里带回给 `_check`。
    _webui_error: str = ""

    @staticmethod
    def expand_targets(config, targets=None) -> list[str]:
        return ["qq"]

    async def check(self):
        result = await super().check()
        if result.status == Status.UNKNOWN:
            self.state.set("qq_fail_streak", 0)
            result.metrics.update(fail_streak=0, online=None, detection_degraded=True)
        return result

    async def _check(self):
        cfg = self.config.qq
        # 每轮重置：上一轮的失败原因不能残留到这一轮的 errors 里。
        self._webui_error = ""
        metrics: dict[str, Any] = {
            "online": None, "evidence_status": None, "evidence_login": None,
            "user_id": "", "nickname": "", "fail_streak": 0,
            "transport": cfg.transport,
        }
        # 并发查询，单请求超时不叠加到探针总超时之上。
        # 第三条证据链走 NapCat WebUI（`/api/QQLogin/GetQQLoginInfo`），与
        # OneBot 通道**完全独立**：OneBot 的 WS 断了但 NapCat 进程活着时，
        # 前两条证据都会失败，只有这条还能给出确定答案（D00 §5.2-5）。
        responses = await asyncio.gather(self.napcat.get_status(),
                                         self.napcat.get_login_info(),
                                         self._webui_evidence(),
                                         return_exceptions=True)
        errors: list[str] = []
        for action, response in zip(("get_status", "get_login_info"), responses[:2],
                                    strict=False):
            if isinstance(response, BaseException):
                if isinstance(response, asyncio.CancelledError):
                    raise response
                errors.append(f"{action}: {type(response).__name__}")
                continue
            if not isinstance(response, dict):
                errors.append(f"{action}: 无效响应")
                continue
            if response.get("status") not in (None, "ok") or response.get("retcode", 0) != 0:
                errors.append(f"{action}: API 返回失败")
                continue
            data = response.get("data", response)
            if not isinstance(data, dict):
                errors.append(f"{action}: data 非对象")
                continue
            if action == "get_status":
                if isinstance(data.get("online"), bool):
                    metrics["evidence_status"] = data["online"]
                    metrics["good"] = data.get("good")
                else:
                    errors.append("get_status: 缺少有效 online 字段")
            elif "user_id" in data:
                uid = str(data.get("user_id") or "").strip()
                metrics["evidence_login"] = uid.isdigit() and int(uid) > 0
                if metrics["evidence_login"]:
                    metrics.update(user_id=uid, nickname=str(data.get("nickname") or ""))
            else:
                errors.append("get_login_info: 缺少 user_id 字段")

        # ---- 第三条证据：WebUI ------------------------------------------ #
        # `None` 表示「这条证据不可用」，与 `False`（协议端明确说不在线）严格区分。
        # 不可用的原因（未配置 / 鉴权失败 / 不可达 / 超时）只进 errors 供排障，
        # **绝不**参与判定——否则一次 WebUI token 配错就会把所有正常部署判成离线。
        webui_online = responses[2]
        if isinstance(webui_online, BaseException):
            if isinstance(webui_online, asyncio.CancelledError):
                raise webui_online
            errors.append(f"webui: {type(webui_online).__name__}")
            webui_online = None
        elif not isinstance(webui_online, bool):
            webui_online = None
            # 失败原因由 `_webui_evidence` 内部记下（异常已被它收敛成 None），
            # 只有真出现了问题才拼进 errors，避免「未配置」也刷一条噪声。
            reason = getattr(self, "_webui_error", "")
            if reason:
                errors.append(reason)
        metrics["evidence_webui"] = webui_online

        # get_status 的明确 offline 优先于可能残留的账号信息。
        online = metrics["evidence_status"]
        if online is None:
            online = metrics["evidence_login"]
        # 前两条都给不出结论时，才让第三条说话。顺序不能颠倒：OneBot 是
        # 与 Bot 同一通道的正据，WebUI 只作为「OneBot 已死、NapCat 还活着」时的补充。
        if online is None and webui_online is not None:
            online = webui_online
            metrics["evidence_source"] = "webui"
        metrics["online"] = online
        if online is True:
            self.state.set("qq_fail_streak", 0)
            who = metrics["nickname"] or metrics["user_id"] or "已连接账号"
            if metrics.get("evidence_source") == "webui":
                # OneBot 两条都没答上来，是 WebUI 给的结论。说清楚来源，
                # 否则运维会误以为 OneBot 通道是好的。
                return self.result(
                    Status.OK, Severity.INFO,
                    f"QQ 在线（{who}，经 NapCat WebUI 确认；OneBot 通道无响应）",
                    metrics)
            return self.result(Status.OK, Severity.INFO, f"QQ 在线（{who}）", metrics)
        if online is None:
            # F11：把"最近有消息事件"作为**弱正据**。协议端能持续把消息推上来，
            # 本身就证明它活着。这是本部署（NapCat 未开 OneBot 11 HTTP）里唯一
            # 真正可用的证据链——此前算了却不用，导致探测能力为 0。
            window = float(getattr(cfg, "activity_evidence_sec", 1800) or 0)
            age = self._activity_age()
            metrics["evidence_activity_age"] = age
            if window > 0 and age is not None and 0 <= age <= window:
                self.state.set("qq_fail_streak", 0)
                metrics["activity_evidence"] = True
                return self.result(
                    Status.OK, Severity.INFO,
                    f"QQ 在线（最近 {age / 60:.0f} 分钟内有消息事件，弱正据；"
                    "协议端活跃度）", metrics)
            self.state.set("qq_fail_streak", 0)
            metrics["detection_degraded"] = True
            return self.result(Status.UNKNOWN, Severity.WARN,
                               "QQ 登录态无法判定（无确定证据，不触发重登）。"
                               + "；".join(errors), metrics)

        streak = int(self.state.get("qq_fail_streak") or 0) + 1
        self.state.set("qq_fail_streak", streak)
        metrics["fail_streak"] = streak
        need = cfg.offline_confirm_rounds
        if streak < need:
            return self.result(Status.WARN, Severity.INFO,
                               f"QQ 疑似掉线（{streak}/{need} 轮确认中）", metrics)
        return self.result(Status.CRIT, Severity.CRIT,
                           f"QQ 已掉线（连续 {streak} 轮确认）", metrics)

    async def _webui_evidence(self) -> bool | None:
        """第三条证据：NapCat WebUI `GetQQLoginInfo.online`。

        返回值语义严格三分，调用方据此判定：

        - `True`  —— 协议端自己认的在线态为真；
        - `False` —— 协议端明确说不在线（可作为离线证据，计入 fail_streak）；
        - `None`  —— **这条证据不可用**（未配置 / 鉴权失败 / 不可达 / 超时）。

        `None` 绝不能被读成 `False`。`qq.webui_base` 默认为空，配置了但 token
        写错也很常见——把这两种情况当成"离线"会让一个纯配置问题变成
        「每 20 分钟重启一次 NapCat」的自动事故。

        失败原因写入 `self._webui_error`，供调用方拼进 errors 便于排障
        （异常已被本方法吞掉，`gather` 拿不到类名）。
        """
        self._webui_error = ""
        getter = getattr(self.napcat, "get_qq_login_info", None)
        if getter is None or not callable(getter):
            # 客户端没实现该能力（例如测试桩）：静默跳过，不算错误。
            return None
        cfg = self.config.qq
        if not str(getattr(cfg, "webui_base", "") or "").strip():
            # 没配 WebUI 就根本不发请求：省一次必然失败的往返。
            # 这也是默认部署的常态，不该每轮都报一条错误。
            return None
        try:
            # 超时给得比探针总超时（12s）短：`auth_login` 换凭据与业务请求
            # 各算一次往返，4s 是留足余量的上界。它只是补充证据，
            # 不值得把整轮巡检拖到超时。
            info = await asyncio.wait_for(getter(), timeout=self.WEBUI_EVIDENCE_TIMEOUT)
        except (NapCatUnavailable, NapCatAuthError, NapCatError) as exc:
            self._webui_error = f"webui: {type(exc).__name__}"
            return None
        except asyncio.TimeoutError:
            self._webui_error = f"webui: 超时(>{self.WEBUI_EVIDENCE_TIMEOUT:.0f}s)"
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 兜底：任何意外都只能降级为"不可用"，绝不能变成离线证据。
            self._webui_error = f"webui: {type(exc).__name__}"
            return None
        if not isinstance(info, dict):
            self._webui_error = "webui: 返回非对象"
            return None
        online = info.get("online")
        if not isinstance(online, bool):
            self._webui_error = "webui: 缺少有效 online 字段"
            return None
        return online

    def _activity_age(self) -> float | None:
        """最近一次消息事件的年龄（秒）。

        由 `GuardianKernel.note_event()` / `_patrol()` 写入 `last_event_age`。
        从未收到过事件时返回 None——**不把"没数据"当成"没活跃"**。
        """
        if self.state is None:
            return None
        raw = self.state.get("last_event_age")
        if raw is None:
            return None
        try:
            age = float(raw)
        except (TypeError, ValueError):
            return None
        if age != age or age < 0:  # NaN 或负值（时钟回拨）→ 不可用
            return None
        return age
