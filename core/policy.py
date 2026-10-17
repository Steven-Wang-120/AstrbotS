"""Guardian 守护系统 —— 判定引擎（ProbeResult -> Finding -> 建议动作）。

对应文档：B03 §6

设计：探针负责阈值计算（产出 status/severity），策略层负责"给出建议动作"。
规则表数据驱动，便于测试与扩展。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    STATUS_ORDER,
    ActionSpec,
    Finding,
    ProbeResult,
    Status,
)


@dataclass
class ActionRule:
    probe: str
    when: dict[str, Any]
    action: str
    min_status: Status = Status.WARN
    reason: str = ""

    def matches(self, res: ProbeResult) -> bool:
        if res.probe != self.probe:
            return False
        if STATUS_ORDER.get(res.status, 0) < STATUS_ORDER.get(self.min_status, 2):
            return False
        for key, expected in self.when.items():
            actual = res.metrics.get(key)
            if isinstance(expected, bool):
                if bool(actual) is not expected:
                    return False
            elif actual != expected:
                return False
        return True


# 默认动作规则表（按顺序匹配，命中即止）
DEFAULT_ACTION_RULES: list[ActionRule] = [
    ActionRule("container_health", {"missing": True}, "start_container",
               Status.CRIT, "容器不存在，尝试启动"),
    ActionRule("container_health", {"running": False}, "start_container",
               Status.CRIT, "容器未运行，尝试启动"),
    ActionRule("container_health", {"oom_killed": True}, "restart_container",
               Status.CRIT, "容器被 OOM 杀死，重启"),
    ActionRule("container_health", {"health_unhealthy": True}, "restart_container",
               Status.WARN, "健康检查失败，重启"),
    ActionRule("qq_login", {"online": False}, "qq_relogin",
               Status.CRIT, "QQ 掉线，尝试重登"),
    ActionRule("container_memory", {"leak": True}, "restart_container",
               Status.WARN, "内存疑似泄漏，重启"),
    ActionRule("ros2_nodes", {"all_present": False}, "ros2_restart",
               Status.CRIT, "ROS2 节点缺失，尝试恢复"),
]


class PolicyEngine:
    def __init__(self, config=None, rules: list[ActionRule] | None = None) -> None:
        self.config = config
        self.rules = list(rules if rules is not None else DEFAULT_ACTION_RULES)

    def evaluate(self, results: list[ProbeResult]) -> list[Finding]:
        findings: list[Finding] = []
        for res in results:
            action = self._suggest(res)
            findings.append(Finding(
                probe=res.probe,
                target=res.target,
                status=res.status,
                severity=res.severity,
                reason=res.message,
                suggested_action=action,
            ))
        return findings

    def _suggest(self, res: ProbeResult) -> ActionSpec | None:
        if res.status in (Status.OK, Status.UNKNOWN):
            return None
        if res.probe == "container_memory":
            # 默认只告警：规则不能绕过用户关闭的自动重启开关。
            if self.config is None or not self.config.memory.restart_on_leak:
                return None
            if res.metrics.get("leak") is True:
                return ActionSpec("restart_container", res.target,
                                  params={"reason": "memory_leak", "cooldown_key": "memory_leak"},
                                  reason=res.message)
            return None
        for rule in self.rules:
            if rule.matches(res):
                return ActionSpec(
                    kind=rule.action,
                    target=res.target,
                    params={"reason": rule.reason or res.message},
                    reason=rule.reason or res.message,
                )
        return None
