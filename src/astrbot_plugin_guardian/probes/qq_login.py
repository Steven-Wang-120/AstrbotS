"""QQ 登录态：复用反向 WebSocket（或显式配置 HTTP）直接查询。

只认协议端的确定回答。无消息、请求失败、无效响应都不证明掉线。
UNKNOWN 会打断连续确认，不能跨不确定轮次或插件重载积累掉线计数。
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..core.models import Severity, Status
from ..core.registry import register_probe
from ..utils.napcat import NapCatError
from .base import Probe


@register_probe("qq_login")
class QqLoginProbe(Probe):
    name = "qq_login"
    scope = "server"
    timeout = 12.0

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
        metrics: dict[str, Any] = {
            "online": None, "evidence_status": None, "evidence_login": None,
            "user_id": "", "nickname": "", "fail_streak": 0,
            "transport": cfg.transport,
        }
        # 并发查询，单请求超时不叠加到探针总超时之上。
        responses = await asyncio.gather(self.napcat.get_status(),
                                         self.napcat.get_login_info(),
                                         return_exceptions=True)
        errors: list[str] = []
        for action, response in zip(("get_status", "get_login_info"), responses):
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

        # get_status 的明确 offline 优先于可能残留的账号信息。
        online = metrics["evidence_status"]
        if online is None:
            online = metrics["evidence_login"]
        metrics["online"] = online
        if online is True:
            self.state.set("qq_fail_streak", 0)
            who = metrics["nickname"] or metrics["user_id"] or "已连接账号"
            return self.result(Status.OK, Severity.INFO, f"QQ 在线（{who}）", metrics)
        if online is None:
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
