"""Guardian 守护系统 —— 服务器模式。

对应文档：B00 §4、B04、B05、B06

默认面向 Arch Linux + Docker 双容器（napcat / astrbot）。
"""

from __future__ import annotations

from .base import ModeAdapter


class ServerMode(ModeAdapter):
    name = "server"
    display = "服务器模式"
    probe_names = [
        "docker_daemon",
        "container_health",
        "container_memory",
        "qq_login",
        "self_health",
        "uptime",
    ]

    def describe(self) -> str:
        os_hint = getattr(self.config, "os_hint", "arch")
        managed = ", ".join(sorted(self.config.action_allowlist)) or "(空)"
        return (f"{self.display}(server) | 目标系统 {os_hint} | Docker 双容器 | "
                f"白名单: {managed} | 探针: {', '.join(self.probe_names)}")
