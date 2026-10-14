"""Guardian 守护系统 —— 文本工具。

对应文档：B08 §消息模板、B11 §脱敏
"""

from __future__ import annotations

import time
from typing import Any

STATUS_ICON = {
    "ok": "[OK]",
    "warn": "[!]",
    "crit": "[X]",
    "unknown": "[?]",
}

SEVERITY_ICON = {
    "info": "[i]",
    "warn": "[!]",
    "crit": "[X]",
    "fatal": "[!!]",
}


def human_bytes(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}PB"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h}h{m}m"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


def mask_umo(umo: str) -> str:
    """UMO 脱敏展示：保留平台前缀与尾部 4 位。"""
    if not umo:
        return ""
    if len(umo) <= 8:
        return "*" * len(umo)
    return f"{umo[:6]}...{umo[-4:]}"


def truncate(text: str, limit: int = 300) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def status_icon(status: Any) -> str:
    key = getattr(status, "value", status)
    return STATUS_ICON.get(str(key), "[?]")


def severity_icon(severity: Any) -> str:
    key = getattr(severity, "value", severity)
    return SEVERITY_ICON.get(str(key), "[i]")
