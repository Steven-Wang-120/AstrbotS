"""Guardian 守护系统 —— 内部事件总线（进程内 async pub/sub）。

对应文档：B03 §11、B08 §2
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Awaitable, Callable

from .log import logger

Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


class EventBus:
    """极简异步发布订阅。

    - 主题支持精确匹配与 `*` 通配（前缀通配，如 `incident.*`）。
    - 单个订阅者抛异常不影响其他订阅者与发布方（异常隔离）。
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        if handler in self._subs.get(topic, []):
            self._subs[topic].remove(handler)

    def clear(self) -> None:
        self._subs.clear()

    def _match(self, topic: str) -> list[Handler]:
        out: list[Handler] = list(self._subs.get(topic, []))
        for key, handlers in self._subs.items():
            if key.endswith("*") and topic.startswith(key[:-1]):
                out.extend(handlers)
        return out

    async def publish(self, topic: str, payload: dict[str, Any] | None = None) -> None:
        payload = payload or {}
        for handler in self._match(topic):
            try:
                await handler(topic, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[guardian] 事件订阅者异常 topic={topic}: {exc}")
