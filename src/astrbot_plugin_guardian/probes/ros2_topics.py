"""Guardian 守护系统 —— ROS2 话题探针。

对应文档：B07 §5
"""

from __future__ import annotations

from ..core.models import Severity, Status
from ..core.registry import register_probe
from ..utils.ros2_runner import parse_topic_hz, parse_topic_list
from .base import Probe


@register_probe("ros2_topics")
class Ros2TopicsProbe(Probe):
    name = "ros2_topics"
    scope = "ros2"
    timeout = 40.0

    @staticmethod
    def expand_targets(config, targets=None) -> list[str]:
        return ["ros2"]

    async def _check(self):
        cfg = self.config.ros2
        expected = [str(t) for t in (cfg.expected_topics or [])]
        min_hz = float(cfg.topic_hz_min or 0.0)

        out = await self.ros2.topic_list()
        if not out.ok:
            return self.result(Status.UNKNOWN, Severity.WARN,
                               f"ros2 topic list 失败(rc={out.rc})：{out.text[:200]}",
                               {"error": True})
        found = parse_topic_list(out.text)
        missing = [t for t in expected if t not in found]

        hz_map: dict[str, float] = {}
        dead: list[str] = []
        if min_hz > 0:
            for topic in expected:
                if topic in missing:
                    continue
                hz_out = await self.ros2.topic_hz(topic)
                hz = parse_topic_hz(hz_out.text)
                hz_map[topic] = hz
                if hz < 0 or hz < min_hz:
                    dead.append(topic)

        metrics = {
            "expected": expected,
            "found": found,
            "missing": missing,
            "hz": hz_map,
            "dead": dead,
            "topic_count": len(found),
        }
        if missing:
            return self.result(Status.CRIT, Severity.CRIT,
                               f"ROS2 话题缺失：{', '.join(missing)}", metrics)
        if dead:
            detail = ", ".join(f"{t}={hz_map.get(t, -1):.1f}Hz" for t in dead)
            return self.result(Status.WARN, Severity.WARN,
                               f"ROS2 话题频率过低（阈值 {min_hz}Hz）：{detail}", metrics)
        return self.result(Status.OK, Severity.INFO,
                           f"ROS2 话题正常（{len(found)} 个）", metrics)
