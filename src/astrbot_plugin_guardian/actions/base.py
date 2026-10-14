"""Guardian 守护系统 —— 动作基类与注册表。

对应文档：B03 §7、B04 §6
"""

from __future__ import annotations

from typing import Any, Type

from ..core.models import ActionResult, ActionSpec

_REGISTRY: dict[str, Type[Any]] = {}


def register_action(kind: str):
    def deco(cls):
        cls.action_kind = kind
        _REGISTRY[kind] = cls
        return cls
    return deco


class Action:
    kind = "base"

    def __init__(self, config, deps: dict[str, Any]) -> None:
        self.config = config
        self.deps = deps
        self.docker = deps.get("docker")
        self.napcat = deps.get("napcat")
        self.ros2 = deps.get("ros2")
        self.state = deps.get("state")
        self.audit = deps.get("audit")
        self.engine = deps.get("engine")

    async def execute(self, spec: ActionSpec) -> ActionResult:  # pragma: no cover
        raise NotImplementedError


def get_action(kind: str) -> Type[Action] | None:
    return _REGISTRY.get(kind)


def all_action_kinds() -> list[str]:
    return sorted(_REGISTRY)
