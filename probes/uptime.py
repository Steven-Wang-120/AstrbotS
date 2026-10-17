"""Guardian 守护系统 —— 运行时长探针（生命周期等级依据）。

对应文档：B15 §3.2

运行时长 = now - min(所有 uptime_targets 容器的 State.StartedAt)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..core.models import Severity, Status
from ..core.registry import register_probe
from ..utils.dockerclient import DockerError, DockerUnavailable
from .base import Probe


def parse_started_at(value: str) -> float:
    """解析 Docker 的 RFC3339（含纳秒）时间戳为 epoch 秒；失败返回 0。"""
    if not value:
        return 0.0
    txt = value.strip().replace("Z", "+00:00")
    # 截断纳秒到微秒
    if "." in txt:
        head, _, tail = txt.partition(".")
        tz = ""
        for i, ch in enumerate(tail):
            if ch in "+-":
                tz = tail[i:]
                tail = tail[:i]
                break
        digits = "".join(c for c in tail if c.isdigit())[:6]
        txt = f"{head}.{digits}{tz}" if digits else f"{head}{tz}"
    try:
        return datetime.fromisoformat(txt).astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


@register_probe("uptime")
class UptimeProbe(Probe):
    name = "uptime"
    scope = "lifecycle"
    timeout = 10.0

    @staticmethod
    def expand_targets(config, targets=None) -> list[str]:
        return ["system"]

    async def _check(self):
        targets = list(self.config.lifecycle.uptime_targets or self.config.all_containers)
        starts: list[float] = []
        detail: dict[str, Any] = {}
        for cname in targets:
            try:
                info = await self.docker.inspect(cname)
            except DockerError as exc:
                detail[cname] = f"err:{exc.status or 'api'}"
                continue
            except DockerUnavailable as exc:
                return self.result(Status.UNKNOWN, Severity.WARN,
                                   f"Docker 不可达：{exc}", {"containers": detail})
            started = parse_started_at((info.get("State") or {}).get("StartedAt", ""))
            detail[cname] = round(started, 1)
            if started > 0:
                starts.append(started)

        if not starts:
            return self.result(Status.UNKNOWN, Severity.WARN,
                               "无法获取任何容器启动时间", {"containers": detail})

        earliest = min(starts)
        now = time.time()
        if earliest > now + 60:
            # 时钟回拨
            return self.result(Status.WARN, Severity.WARN,
                               "检测到时钟回拨（容器启动时间在未来），运行时长不可信",
                               {"clock_skew": True, "containers": detail})
        uptime_h = (now - earliest) / 3600.0
        return self.result(
            Status.OK, Severity.INFO,
            f"系统连续运行 {uptime_h:.1f} 小时（{uptime_h / 24:.1f} 天）",
            {"uptime_hours": round(uptime_h, 3), "earliest_start": earliest,
             "containers": detail})
