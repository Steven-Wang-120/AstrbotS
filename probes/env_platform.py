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
import re
import time
from datetime import datetime
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


def detect_tz_offset_h(env: dict[str, str] | None = None) -> float:
    """返回本地时区相对 UTC 的偏移（小时，东正西负），例如东八区 -> ``8.0``。

    **原来的写法是错的**：``round(-time.timezone / 3600, 1)``。
    两个问题叠在一起：

    1. ``time.timezone`` 是 **standard time** 偏移，不含夏令时；
    2. 更要命的是容器里没设 ``TZ`` 时它**直接给 0**，与真实本地时区无关。
       东八区的镜像如果漏了 ``TZ``，作息窗口会整体偏移 8 小时——这是本模块
       最危险的静默失败点。

    所以按可靠性从高到低依次尝试：

    1. ``TZ`` 环境变量（``Asia/Shanghai`` 这类 IANA 名，或 ``CST-8`` 这类 POSIX 串）
    2. ``datetime.now().astimezone().utcoffset()`` —— 跟随进程的实际本地时区
    3. ``-time.altzone / 3600``（夏令时版本）
    4. ``-time.timezone / 3600``（标准时版本，最后的兜底）

    第 2 步在绝大多数容器里已经能给出正确答案（glibc 会读 ``/etc/localtime``），
    第 1 步只是为了在 ``TZ`` 显式设置时优先信任运维的意图。
    """
    env = os.environ if env is None else env

    # ① TZ 显式设置：IANA 名优先（zoneinfo 能正确处理该地区的夏令时历史）
    raw_tz = str(env.get("TZ", "") or "").strip()
    if raw_tz:
        offset = _offset_from_tz_name(raw_tz)
        if offset is None:
            offset = _offset_from_posix_tz(raw_tz)
        if offset is not None:
            return offset
        # TZ 写了但解不出来：**不要**偷偷回落到"进程时区"——那会把一个明确的
        # 配置错误伪装成正确答案。交给下面两步，但记住这是一次降级。
    # ② 进程实际本地时区（最贴合"这台机器认为现在几点"）
    try:
        offset = datetime.now().astimezone().utcoffset()
        if offset is not None:
            return round(offset.total_seconds() / 3600.0, 2)
    except (OSError, OverflowError, ValueError):
        pass

    # ③ 夏令时版本，④ 标准时版本
    for value in (getattr(time, "altzone", None), getattr(time, "timezone", None)):
        if value is None:
            continue
        try:
            return round(-float(value) / 3600.0, 2)
        except (TypeError, ValueError):
            continue

    return 0.0


def _offset_from_tz_name(name: str) -> float | None:
    """用 ``zoneinfo`` 解析 IANA 时区名；不是 IANA 名或不可用时返回 None。"""
    # 只有形如 "Area/City" 或 "UTC"/"GMT" 的才值得交给 zoneinfo，
    # 否则会把 "CST-8" 这类 POSIX 串当文件路径去查，纯属浪费。
    if "/" not in name and name.upper() not in {"UTC", "GMT", "UCT", "Z"}:
        return None
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        return None
    try:
        offset = datetime.now(ZoneInfo(name)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # **注意**：Windows 与精简镜像可能没有 tzdata，此时所有 IANA 名都会
        # 落到这里。上层会继续用 `astimezone()` 兜底，结果通常仍然正确
        # （glibc/Windows 自己知道本地时区），只是"TZ 优先"这一层失效了。
        # 不要在这里 raise：时区拿不到是降级，不是故障。
        return None
    if offset is None:
        return None
    return round(offset.total_seconds() / 3600.0, 2)

def _offset_from_posix_tz(value: str) -> float | None:
    """解析 POSIX 风格 ``CST-8`` / ``UTC0``；POSIX 的符号与直觉相反。

    POSIX 里 ``CST-8`` 表示"比 UTC **快** 8 小时"，也就是东八区 ``+8``。
    即 **偏移 = 数字部分的相反数**：``-8`` -> ``+8``，``+5`` -> ``-5``。

    只接受"字母名 + 可选数字"，且字母名长度 3..5（``UTC``/``GMT`` 这类特殊名另判）。
    带夏令时规则的形态（``EST5EDT``、``CST-8CDT``）这里**解不了**——那需要完整
    的 tzdata 规则，交给第 ② 步的 ``astimezone()`` 更可靠，所以这里返回 None。
    """
    if value.upper() in {"UTC", "GMT", "UCT", "Z"}:
        return 0.0
    match = re.fullmatch(r"([A-Za-z]{3,5})([+-]?\d{1,2}(?::\d{2})?(?::\d{2})?)", value)
    if not match:
        return None
    body = match.group(2)
    # POSIX 符号是反的：`CST-8` 的 `-8` 表示"比 UTC 快 8 小时" = +8。
    sign = 1.0 if body[0] == "-" else -1.0
    if body[0] in "+-":
        body = body[1:]
    chunks = body.split(":")
    try:
        hours = int(chunks[0])
        minutes = int(chunks[1]) if len(chunks) > 1 else 0
    except ValueError:
        return None
    if not 0 <= hours <= 24 or not 0 <= minutes < 60:
        return None
    total = sign * (hours + minutes / 60.0)
    # `-0.0` 会一路传到状态文本里显示成 "-0"，归一化成 +0.0
    return round(total, 2) + 0.0


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
    tz_offset_h = detect_tz_offset_h()

    data = {
        "os": canonical_os(info.get("os") or (platform.system() if platform else "")),
        "os_id": str(info.get("id") or ""),
        "os_like": list(info.get("like") or []),
        "distro": pretty_name(info),
        # 内核取 `info["kernel"]`（`platform.release()`），**不是** `info["version"]`：
        # 后者是发行版 `VERSION_ID`（Debian 13 → "13"），当内核用会报出假内核版本。
        "kernel": str(info.get("kernel") or ""),
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
