"""Guardian 守护系统 —— ROS2 doctor 探针。

对应文档：B07 §6
"""

from __future__ import annotations

from ..core.models import Severity, Status
from ..core.registry import register_probe
from .base import Probe


@register_probe("ros2_doctor")
class Ros2DoctorProbe(Probe):
    name = "ros2_doctor"
    scope = "ros2"
    timeout = 30.0

    @staticmethod
    def expand_targets(config, targets=None) -> list[str]:
        return ["ros2"]

    async def _check(self):
        out = await self.ros2.doctor_report()
        if not out.ok:
            return self.result(Status.UNKNOWN, Severity.WARN,
                               f"ros2 doctor 失败(rc={out.rc})：{out.text[:200]}",
                               {"error": True})
        text = out.text or ""
        warnings = [ln for ln in text.splitlines() if "warning" in ln.lower()]
        errors = [ln for ln in text.splitlines() if "error" in ln.lower()]
        metrics = {
            "warning_count": len(warnings),
            "error_count": len(errors),
            "warnings": warnings[:5],
            "errors": errors[:5],
        }
        if errors:
            return self.result(Status.CRIT, Severity.CRIT,
                               f"ros2 doctor 报告 {len(errors)} 个错误", metrics)
        if warnings:
            return self.result(Status.WARN, Severity.WARN,
                               f"ros2 doctor 报告 {len(warnings)} 个警告", metrics)
        return self.result(Status.OK, Severity.INFO, "ros2 doctor 无异常", metrics)
