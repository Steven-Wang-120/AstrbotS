"""Guardian 守护系统 —— 动作基类与注册表。

对应文档：B03 §7、B04 §6
"""

from __future__ import annotations

from typing import Any

from ..core.models import ActionResult, ActionSpec

_REGISTRY: dict[str, type[Any]] = {}


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


def get_action(kind: str) -> type[Action] | None:
    return _REGISTRY.get(kind)


def all_action_kinds() -> list[str]:
    return sorted(_REGISTRY)


def direct_actions_enabled(config) -> bool:
    """M5/D2-b：内核能否**直连** docker.sock 执行动作。

    两个条件同时满足才为真：

    1. `kernel.direct_actions` 为真（显式回滚开关，B16 §9 M5）；
    2. `kernel.mode == "ipc"`——只有内核真的独立成进程时，直连重启 `astrbot` 才
       不再是自杀（B16 §1 S1）。`inproc` 下"内核"就住在 `astrbot` 进程里，
       直连等于自杀，必须回退"经看门狗"。
    """
    kernel = getattr(config, "kernel", None)
    if kernel is None:
        return False
    enabled = bool(getattr(kernel, "direct_actions", True))
    mode = str(getattr(kernel, "mode", "inproc") or "inproc")
    return enabled and mode == "ipc"
