"""Guardian 守护系统 —— 通知器。

对应文档：B08

- 目标会话来自 notify.umo_list
- 按 min_severity 过滤
- 按 rate_limit_sec 限流 + 内容去重（同 target+status 窗口内只推一次）
- 档位静默：night 静默低于 mute_below_severity；maintenance 全静默
- 推送失败重试并降级到日志；失效 UMO 自动跳过
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from ..core.models import Incident, Severity, severity_at_least
from ..utils.textutil import mask_umo, truncate


class Notifier:
    def __init__(self, config, state, audit, sender) -> None:
        self.config = config
        self.state = state
        self.audit = audit
        self._sender = sender            # async (umo, text) -> None
        self._last_sent: dict[str, float] = {}
        self._dedup: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    def _allowed_by_profile(self, severity: Severity) -> bool:
        profile = self.state.get("profile") or self.config.profile
        if profile == "maintenance":
            return False
        if profile == "night":
            floor = self.config.night_profile.mute_below_severity
            return severity_at_least(severity, floor)
        return True

    def _dedup_key(self, text: str, target: str, status: str) -> str:
        return f"{target}|{status}|{truncate(text, 60)}"

    async def push(self, text: str, *, severity: Severity = Severity.INFO,
                   force: bool = False, target: str = "", status: str = "") -> bool:
        """推送一条消息。force=True 时绕过档位静默与去重（用于睡眠等关键回执）。"""
        if not force:
            if not self._allowed_by_profile(severity):
                logger.debug(f"[guardian] 通知被档位静默: {truncate(text, 40)}")
                return False
            floor = self.config.notify.min_severity
            if not severity_at_least(severity, floor):
                return False

        now = time.time()
        key = self._dedup_key(text, target, status)
        if not force:
            if (now - self._dedup.get(key, 0)) < int(self.config.notify.rate_limit_sec):
                logger.debug(f"[guardian] 通知去重: {truncate(text, 40)}")
                return False
        self._dedup[key] = now

        umos = list(self.config.notify.umo_list or [])
        if not umos:
            logger.info(f"[guardian] 无通知目标，仅记录日志：{truncate(text, 200)}")
            self.audit.append(kind="notify.no_target", detail=truncate(text, 200))
            return False

        invalid = set(self.state.get("umos_invalid") or [])
        sent = False
        for umo in umos:
            if umo in invalid:
                continue
            window = int(self.config.notify.rate_limit_sec)
            if (now - self._last_sent.get(umo, 0)) < window and not force:
                continue
            ok = await self._send_one(umo, text)
            if ok:
                self._last_sent[umo] = now
                sent = True
        return sent

    async def _send_one(self, umo: str, text: str) -> bool:
        for attempt in range(2):
            try:
                await self._sender(umo, text)
                self.audit.append(kind="notify.sent", target=mask_umo(umo),
                                  detail=truncate(text, 200), ok=True)
                return True
            except Exception as exc:  # noqa: BLE001
                if attempt == 0:
                    logger.debug(f"[guardian] 通知重试 {mask_umo(umo)}: {exc}")
                    continue
                logger.warning(f"[guardian] 通知失败 {mask_umo(umo)}: {exc}")
                self._mark_invalid(umo)
        return False

    def _mark_invalid(self, umo: str) -> None:
        fails = self.state.get("umo_fails") or {}
        fails[umo] = int(fails.get(umo, 0)) + 1
        self.state.set("umo_fails", fails)
        if fails[umo] >= 3:
            invalid = set(self.state.get("umos_invalid") or [])
            invalid.add(umo)
            self.state.set("umos_invalid", list(invalid))
            self.audit.append(kind="notify.umo_invalid", target=mask_umo(umo),
                              detail="连续失败 3 次，已跳过")

    # ------------------------------------------------------------------ #
    async def push_incidents(self, incidents: list[Incident], *, force: bool = False) -> None:
        """把一轮巡检的多条事件聚合成一条摘要推送。"""
        if not incidents:
            return
        lines = [i.one_line() for i in incidents]
        text = "[守护] 本轮发现 %d 项异常：\n%s" % (
            len(incidents), "\n".join(f"  - {ln}" for ln in lines[:10]))
        worst = max((i.severity for i in incidents),
                    key=lambda s: severity_at_least(s, Severity.INFO) and list(Severity).index(s))
        target = incidents[0].target
        await self.push(text, severity=worst, force=force, target=target,
                        status=incidents[0].status.value)

    def reset_dedup(self) -> None:
        self._dedup.clear()
