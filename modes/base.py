"""Guardian 守护系统 —— 模式适配器基类。

对应文档：B02 §4
"""

from __future__ import annotations

from typing import Any

from ..core.registry import build_probes


class ModeAdapter:
    name = "base"
    display = "基础"
    probe_names: list[str] = []

    def __init__(self, config, deps: dict[str, Any]) -> None:
        self.config = config
        self.deps = deps

    def build_probes(self) -> list[Any]:
        return build_probes(self.probe_names, self.config, self.deps)

    def describe(self) -> str:
        return f"{self.display}({self.name})，探针: {', '.join(self.probe_names)}"

    async def close(self) -> None:
        return None
