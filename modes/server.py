"""Guardian 守护系统 —— 服务器模式。

对应文档：B00 §4、B04、B05、B06

默认面向 Arch Linux + Docker 双容器（napcat / astrbot）。
运行环境（系统 / 内核 / 架构 / 内存）由 `env_platform` 探针从宿主机侧读取，
不在前端手填。
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
        "env_platform",
        "uptime",
    ]

    def describe(self) -> str:
        from ..probes.env_platform import collect_env, env_summary

        managed = ", ".join(sorted(self.config.action_allowlist)) or "(空)"
        # 环境指纹取自宿主机真实值，不再由前端手填：手填值一旦与镜像实际基底
        # 不符，就会变成一条主动误导运维的"文档"。
        return (f"{self.display}(server) | 环境 {env_summary(collect_env())} | "
                f"白名单: {managed} | 探针: {', '.join(self.probe_names)}")
