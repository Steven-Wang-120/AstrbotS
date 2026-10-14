"""Guardian 守护系统 —— Docker daemon 与容器健康探针。

对应文档：B04 §4-§5
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from ..core.models import Severity, Status
from ..core.registry import register_probe
from ..utils.dockerclient import DockerError, DockerUnavailable
from .base import Probe


@register_probe("docker_daemon")
class DockerDaemonProbe(Probe):
    name = "docker_daemon"
    scope = "server"
    timeout = 8.0

    @staticmethod
    def expand_targets(config, targets=None) -> list[str]:
        return ["docker"]

    async def _check(self):
        try:
            await self.docker.negotiate()
            ok = await self.docker.ping()
        except DockerUnavailable as exc:
            return self.result(
                Status.UNKNOWN, Severity.WARN,
                f"Docker daemon 不可达：{exc}（守护能力降级，不执行动作）",
                {"daemon_ok": False})
        if not ok:
            return self.result(Status.UNKNOWN, Severity.WARN,
                               "Docker daemon ping 失败（降级，不动作）",
                               {"daemon_ok": False})
        try:
            info = await self.docker.info()
            containers = int(info.get("Containers") or 0)
            running = int(info.get("ContainersRunning") or 0)
        except (DockerUnavailable, DockerError) as exc:
            return self.result(Status.WARN, Severity.WARN,
                               f"Docker info 读取失败：{exc}", {"daemon_ok": True})
        return self.result(
            Status.OK, Severity.INFO,
            f"Docker daemon 正常（容器 {running}/{containers} 运行中）",
            {"daemon_ok": True, "containers": containers, "running": running,
             "api_version": self.docker.api_version})


@register_probe("container_health")
class ContainerHealthProbe(Probe):
    name = "container_health"
    scope = "server"
    timeout = 10.0

    @staticmethod
    def expand_targets(config, targets=None) -> list[str]:
        return list(targets) if targets else list(config.all_containers)

    async def _check(self):
        target = self.target
        try:
            data = await self.docker.inspect(target)
        except DockerError as exc:
            if exc.status == 404:
                return self.result(
                    Status.CRIT, Severity.CRIT,
                    f"容器 {target} 不存在",
                    {"missing": True, "running": False})
            return self.result(Status.UNKNOWN, Severity.WARN,
                               f"inspect 失败：{exc}", {"inspect_error": True})
        except DockerUnavailable as exc:
            return self.result(Status.UNKNOWN, Severity.WARN,
                               f"Docker 不可达：{exc}", {"daemon_ok": False})

        state = data.get("State", {}) or {}
        running = bool(state.get("Running"))
        restart_count = int(state.get("RestartCount") or 0)
        oom = bool(state.get("OOMKilled"))
        health = ((state.get("Health") or {}).get("Status") or "").lower()
        started_at = state.get("StartedAt") or ""
        status_txt = str(state.get("Status") or "unknown")

        metrics = {
            "missing": False,
            "running": running,
            "restart_count": restart_count,
            "oom_killed": oom,
            "health_unhealthy": health == "unhealthy",
            "health": health,
            "docker_status": status_txt,
            "started_at": started_at,
        }

        # 重启次数增长检测
        prev = (self.state.get("restart_counts") or {}).get(target)
        counts = self.state.get("restart_counts") or {}
        if prev is not None and restart_count > int(prev):
            metrics["restart_grew"] = True
        counts[target] = restart_count
        self.state.set("restart_counts", counts)

        if not running:
            return self.result(Status.CRIT, Severity.CRIT,
                               f"容器 {target} 未运行（状态 {status_txt}）", metrics, state)
        if oom:
            return self.result(Status.CRIT, Severity.CRIT,
                               f"容器 {target} 曾被 OOM 杀死", metrics, state)
        if health == "unhealthy":
            return self.result(Status.WARN, Severity.WARN,
                               f"容器 {target} 健康检查失败", metrics, state)
        if metrics.get("restart_grew"):
            return self.result(Status.WARN, Severity.WARN,
                               f"容器 {target} 重启次数增长至 {restart_count}", metrics, state)
        return self.result(Status.OK, Severity.INFO,
                           f"容器 {target} 运行正常", metrics, state)


@register_probe("self_health")
class SelfHealthProbe(Probe):
    """本体容器自检：磁盘、socket 可用性、容器时区。"""

    name = "self_health"
    scope = "server"
    timeout = 8.0

    @staticmethod
    def expand_targets(config, targets=None) -> list[str]:
        return ["astrbot"]

    async def _check(self):
        import shutil
        import time as _t

        metrics: dict[str, Any] = {}
        # 磁盘
        try:
            usage = shutil.disk_usage("/")
            pct = usage.used / usage.total * 100 if usage.total else 0.0
            metrics["disk_used_pct"] = round(pct, 1)
            metrics["disk_free_gb"] = round(usage.free / 1024**3, 2)
        except OSError:
            metrics["disk_used_pct"] = -1.0
        # socket
        try:
            metrics["socket_ok"] = await self.docker.ping()
        except Exception:  # noqa: BLE001
            metrics["socket_ok"] = False
        metrics["tz_offset_h"] = round(-_t.timezone / 3600, 1)

        if metrics.get("disk_used_pct", 0) >= 95:
            return self.result(Status.CRIT, Severity.CRIT,
                               f"磁盘使用率 {metrics['disk_used_pct']}% 过高", metrics)
        if metrics.get("disk_used_pct", 0) >= 85:
            return self.result(Status.WARN, Severity.WARN,
                               f"磁盘使用率 {metrics['disk_used_pct']}% 偏高", metrics)
        if not metrics.get("socket_ok"):
            return self.result(Status.WARN, Severity.WARN,
                               "Docker socket 不可用：自动重启能力失效", metrics)
        return self.result(Status.OK, Severity.INFO,
                           f"本体自检正常（磁盘 {metrics.get('disk_used_pct')}%）", metrics)
