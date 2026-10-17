"""Guardian 守护系统 —— 模式工厂。

对应文档：B02 §5
"""

from __future__ import annotations

from typing import Any

from .base import ModeAdapter
from .ros2 import Ros2Mode
from .server import ServerMode

_MODES: dict[str, type[ModeAdapter]] = {
    "server": ServerMode,
    "ros2": Ros2Mode,
}


def create_mode(name: str, config, deps: dict[str, Any]) -> ModeAdapter:
    cls = _MODES.get(str(name or "server").lower(), ServerMode)
    return cls(config, deps)


def available_modes() -> list[str]:
    return sorted(_MODES)
