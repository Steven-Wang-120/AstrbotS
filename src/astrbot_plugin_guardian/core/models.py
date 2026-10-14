"""Guardian 守护系统 —— 数据模型与枚举契约。

对应文档：B00 §6、B03 §2、B15 §3.1
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class Mode(str, Enum):
    SERVER = "server"
    ROS2 = "ros2"


class Profile(str, Enum):
    NORMAL = "normal"
    NIGHT = "night"
    MAINTENANCE = "maintenance"
    AGGRESSIVE = "aggressive"


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    CRIT = "crit"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRIT = "crit"
    FATAL = "fatal"


# 用于比较与阈值过滤
SEVERITY_ORDER: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARN: 1,
    Severity.CRIT: 2,
    Severity.FATAL: 3,
}
STATUS_ORDER: dict[Status, int] = {
    Status.OK: 0,
    Status.UNKNOWN: 1,
    Status.WARN: 2,
    Status.CRIT: 3,
}


def severity_at_least(value: Severity | str, floor: Severity | str) -> bool:
    """判断 value 是否 >= floor（用于 min_severity 过滤）。"""
    if isinstance(value, str):
        value = Severity(value)
    if isinstance(floor, str):
        try:
            floor = Severity(floor)
        except ValueError:
            floor = Severity.WARN
    return SEVERITY_ORDER.get(value, 0) >= SEVERITY_ORDER.get(floor, 1)


def worst_status(statuses: list[Status]) -> Status:
    if not statuses:
        return Status.UNKNOWN
    return max(statuses, key=lambda s: STATUS_ORDER.get(s, 0))


def worst_severity(sevs: list[Severity]) -> Severity:
    if not sevs:
        return Severity.INFO
    return max(sevs, key=lambda s: SEVERITY_ORDER.get(s, 0))


def new_incident_id() -> str:
    return f"inc-{int(time.time())}-{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #
@dataclass
class ProbeResult:
    probe: str
    target: str
    status: Status
    severity: Severity
    message: str
    metrics: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    raw: dict[str, Any] | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "target": self.target,
            "status": self.status.value,
            "severity": self.severity.value,
            "message": self.message,
            "metrics": self.metrics,
            "ts": self.ts,
            "error": self.error,
        }


@dataclass
class ActionSpec:
    kind: str
    target: str
    params: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    actor: str = "system"          # system | llm | command | lifecycle
    trigger: str = "interval"      # interval | deadline | llm | command | startup | manual
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "params": self.params,
            "dry_run": self.dry_run,
            "actor": self.actor,
            "trigger": self.trigger,
            "reason": self.reason,
        }


@dataclass
class ActionResult:
    ok: bool
    detail: str
    elapsed_ms: int = 0
    output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
            "output": self.output,
        }


@dataclass
class Finding:
    probe: str
    target: str
    status: Status
    severity: Severity
    reason: str
    suggested_action: ActionSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "target": self.target,
            "status": self.status.value,
            "severity": self.severity.value,
            "reason": self.reason,
            "suggested_action": (
                self.suggested_action.to_dict() if self.suggested_action else None
            ),
        }


@dataclass
class Incident:
    probe: str
    target: str
    status: Status
    severity: Severity
    message: str
    id: str = field(default_factory=new_incident_id)
    ts: float = field(default_factory=time.time)
    action: ActionSpec | None = None
    result: ActionResult | None = None
    suppressed: bool = False
    suppress_reason: str = ""
    notified: bool = False
    trigger: str = "interval"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "probe": self.probe,
            "target": self.target,
            "status": self.status.value,
            "severity": self.severity.value,
            "message": self.message,
            "action": self.action.to_dict() if self.action else None,
            "result": self.result.to_dict() if self.result else None,
            "suppressed": self.suppressed,
            "suppress_reason": self.suppress_reason,
            "notified": self.notified,
            "trigger": self.trigger,
        }

    def one_line(self) -> str:
        tail = ""
        if self.suppressed:
            tail = f"（已抑制：{self.suppress_reason}）"
        elif self.result is not None:
            tail = f"（动作 {self.action.kind if self.action else '-'} -> " \
                   f"{'成功' if self.result.ok else '失败'}）"
        return f"[{self.severity.value.upper()}] {self.target} {self.message}{tail}"
