"""Guardian 守护系统 —— ROS2 模式。

对应文档：B00 §4、B07
"""

from __future__ import annotations

from .base import ModeAdapter


class Ros2Mode(ModeAdapter):
    name = "ros2"
    display = "ROS2 模式"
    probe_names = [
        "docker_daemon",
        "ros2_nodes",
        "ros2_topics",
        "ros2_doctor",
        "uptime",
    ]

    def describe(self) -> str:
        backend = self.config.ros2.exec_backend
        nodes = len(self.config.ros2.expected_nodes or [])
        topics = len(self.config.ros2.expected_topics or [])
        return (f"{self.display}(ros2) | 执行后端 {backend} | "
                f"期望节点 {nodes} 个 / 话题 {topics} 个 | "
                f"探针: {', '.join(self.probe_names)}")
