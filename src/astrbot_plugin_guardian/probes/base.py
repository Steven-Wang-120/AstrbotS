"""Guardian 守护系统 —— 探针基类。

对应文档：B03 §3

约定：子类实现 `_check()`；`check()` 负责超时包装与异常兜底（一律降级为 UNKNOWN）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..core.log import logger

from ..core.models import ProbeResult, Severity, Status


class Probe:
    name = "probe"
    scope = "server"
    timeout: float = 12.0

    def __init__(self, config, deps: dict[str, Any], target: str) -> None:
        self.config = config
        self.deps = deps
        self.target = target
        self.docker = deps.get("docker")
        self.napcat = deps.get("napcat")
        self.ros2 = deps.get("ros2")
        self.state = deps.get("state")

    # ------------------------------------------------------------------ #
    async def check(self) -> ProbeResult:
        try:
            return await asyncio.wait_for(self._check(), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.debug(f"[guardian] 探针 {self.name}/{self.target} 超时")
            return self._unknown("探针执行超时", "timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[guardian] 探针 {self.name}/{self.target} 异常: {exc}")
            return self._unknown(f"探针异常: {exc}", str(exc))

    async def _check(self) -> ProbeResult:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------------ #
    def _unknown(self, message: str, error: str = "") -> ProbeResult:
        return ProbeResult(
            probe=self.name, target=self.target,
            status=Status.UNKNOWN, severity=Severity.WARN,
            message=message, metrics={}, ts=time.time(), error=error,
        )

    def result(self, status: Status, severity: Severity, message: str,
               metrics: dict[str, Any] | None = None,
               raw: dict[str, Any] | None = None) -> ProbeResult:
        return ProbeResult(
            probe=self.name, target=self.target, status=status, severity=severity,
            message=message, metrics=metrics or {}, ts=time.time(), raw=raw,
        )
