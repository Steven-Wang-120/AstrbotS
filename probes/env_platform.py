"""Guardian 守护系统 —— 运行环境探针（体系结构 / 操作系统 / 内存 / 时区）。

**存在的理由是把"人填的环境"换成"机器自报的环境"。** 原先前端有 `os_hint` /
`package_manager` 之类的字段，靠运维手填，一旦填错（或 Dockerfile 换了基底镜像）
就只剩一句过时的提示词。这个探针直接从宿主机侧读 `/etc/os-release`、`uname`、
`/proc/meminfo`，把真实值写进 `ProbeResult.metrics`，由状态快照对外暴露。

**它是只读的、且永远不参与判定。** 不产出 `suggested_action`，检查结论恒为 OK/UNKNOWN；
出现任何异常都降级为 UNKNOWN（"信息缺失"），绝不会因为它让引擎误判容器故障。
"""

from __future__ import annotations

import os
import platform
import time
from typing import Any

from ..core.models import Severity, Status
from ..core.registry import register_probe
from ..utils.osinfo import canonical_os, detect_os, pretty_name
from .base import Probe

# 采样结果缓存：环境信息在一次进程生命周期内基本不变，重复读 /proc 没有意义。
# 但 `**必须**带上 TTL**：容器改名/换镜像后只重启进程即可刷新；
# 直接把缓存写死会让"改了配置但报告还是旧的"变成新的误导源。
CACHE_TTL_SEC = 300.0


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(65536)
    except OSError:
        return ""


def _exists(path: str) -> bool:
    try:
        return os.path.exists(path)
    except OSError:
        return False


def _mem_total_mb(meminfo: str) -> float:
    """从 `/proc/meminfo` 取 MemTotal；取不到返回 0。"""
    for line in (meminfo or "").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return round(float(parts[1]) / 1024.0, 1)
                except ValueError:
                    return 0.0
    return 0.0


def _cpu_count() -> int:
    try:
        return int(os.cpu_count() or 0)
    except (TypeError, ValueError):
        return 0


_CACHE: dict[str, Any] = {"at": 0.0, "data": {}}


def collect_env() -> dict[str, Any]:
    """采集一次环境信息（带进程内 5 分钟缓存）。任何字段失败都是空值，不抛。"""
    now = time.time()
    if _CACHE["data"] and now - float(_CACHE["at"]) < CACHE_TTL_SEC:
        return dict(_CACHE["data"])

    info = detect_os(_read_text, _exists)
    try:
        machine = platform.machine() if platform is not None else ""
    except Exception:  # noqa: BLE001
        machine = ""
    try:
        tz_offset_h = round(-time.timezone / 3600, 1)
    except Exception:  # noqa: BLE001
        tz_offset_h = 0.0

    data = {
        "os": canonical_os(info.get("os") or (platform.system() if platform else "")),
        "os_id": str(info.get("id") or ""),
        "os_like": list(info.get("like") or []),
        "distro": pretty_name(info),
        "kernel": str(info.get("version") or ""),
        "arch": str(machine or ""),
        "libc": platform.libc_ver()[0] if platform is not None else "",
        "python": platform.python_version() if platform is not None else "",
        "cpu_count": _cpu_count(),
        "mem_total_mb": _mem_total_mb(_read_text("/proc/meminfo")),
        "tz_offset_h": tz_offset_h,
        "source": str(info.get("source") or ""),
    }
    _CACHE["at"], _CACHE["data"] = now, data
    return dict(data)


def reset_cache() -> None:
    """清空缓存（测试用；也让 `/guardian check` 之后能看到换镜像后的新值）。"""
    _CACHE["at"], _CACHE["data"] = 0.0, {}


def env_summary(data: dict[str, Any]) -> str:
    """一行式摘要，用于日志与状态文本。"""
    parts = [data.get("distro") or data.get("os") or "unknown"]
    if data.get("arch"):
        parts.append(str(data["arch"]))
    if data.get("mem_total_mb"):
        parts.append(f"{data['mem_total_mb']:.0f}MB")
    text = " · ".join(p for p in parts if p)
    if data.get("mem_total_mb") and float(data["mem_total_mb"]) < 256:
        # 明确标出"看起来是容器而非宿主机"：避免运维把它当宿主机全部内存
        text += "（内存疑似容器视图，非宿主机总量）"
    return text


@register_probe("env_platform")
class EnvPlatformProbe(Probe):
    """运行环境指纹：系统 / 内核 / 架构 / 内存 / 时区。

    `target` 恒为 `"env"`（这是本机自述，不是某个容器），不参与动作。
    """

    name = "env_platform"
    scope = "server"
    timeout = 5.0

    @staticmethod
    def expand_targets(config, targets=None) -> list[str]:
        # 不使用 config.self_container：那是"要守护的容器名"，
        # 而这里描述的是"跑内核的这个进程/机器的环境"，改名无关。
        return ["env"]

    async def _check(self):
        data = collect_env()
        if not data.get("distro"):
            return self.result(
                Status.UNKNOWN, Severity.WARN,
                "无法识别运行环境（/etc/os-release 与 uname 均不可用）",
                data)
        return self.result(
            Status.OK, Severity.INFO,
            f"运行环境：{env_summary(data)}",
            data)
