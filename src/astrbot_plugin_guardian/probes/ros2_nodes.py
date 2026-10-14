"""Guardian 守护系统 —— ROS2 节点探针。

对应文档：B07 §4
"""

from __future__ import annotations

from ..core.models import Severity, Status
from ..core.registry import register_probe
from ..utils.ros2_runner import parse_node_list
from .base import Probe


@register_probe("ros2_nodes")
class Ros2NodesProbe(Probe):
    name = "ros2_nodes"
    scope = "ros2"
    timeout = 20.0

    @staticmethod
    def expand_targets(config, targets=None) -> list[str]:
        return ["ros2"]

    async def _check(self):
        expected = [str(n) for n in (self.config.ros2.expected_nodes or [])]
        out = await self.ros2.node_list()
        if not out.ok:
            return self.result(Status.UNKNOWN, Severity.WARN,
                               f"ros2 node list 失败(rc={out.rc})：{out.text[:200]}",
                               {"all_present": False, "error": True})
        found = parse_node_list(out.text)
        missing = [n for n in expected if n not in found]
        metrics = {
            "expected": expected,
            "found": found,
            "missing": missing,
            "all_present": not missing,
            "node_count": len(found),
        }
        if missing:
            return self.result(Status.CRIT, Severity.CRIT,
                               f"ROS2 节点缺失：{', '.join(missing)}", metrics,
                               {"raw": out.text[:1000]})
        return self.result(Status.OK, Severity.INFO,
                           f"ROS2 节点正常（{len(found)} 个）", metrics)
