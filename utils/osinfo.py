"""Guardian 守护系统 —— 运行环境探测（操作系统 / 发行版 / 内核）。

只在宿主机侧采集，供 `env_platform` 探针展示，**不参与任何判定或命令拼装**。

采集顺序固定为「零依赖优先」：
1. `/etc/os-release`（几乎所有现代发行版的唯一事实来源，含 PRETTY_NAME）
2. `/etc/alpine-release` / `/etc/debian_version`…（没有 os-release 的精简镜像）
3. `platform.system()` 归一出的家族（兜底，只给 `linux` 这种粗粒度结论）

所有函数都是纯数据变换，方便单测；任何采集失败只降级为「信息缺失」，绝不抛异常。
"""

from __future__ import annotations

import platform
from typing import Any

from ..core.log import logger

# 没有 os-release 时按"文件存在"推断的发行版（顺序即优先级）
RELEASE_MARKERS: tuple[tuple[str, str], ...] = (
    ("/etc/alpine-release", "Alpine Linux"),
    ("/etc/debian_version", "Debian"),
    ("/etc/redhat-release", "Red Hat"),
    ("/etc/centos-release", "CentOS"),
    ("/etc/arch-release", "Arch Linux"),
    ("/etc/gentoo-release", "Gentoo"),
)


def canonical_os(system: str | None) -> str:
    """把 `uname` 的系统名归一成家族：`linux` / `windows` / `darwin` / 小写原名。"""
    sysname = (system or "").strip().lower()
    if not sysname:
        return "unknown"
    if sysname.startswith("linux"):
        return "linux"
    if sysname.startswith(("win", "cygwin", "msys")):
        return "windows"
    if sysname == "darwin":
        return "darwin"
    return sysname


def digest_os_release(text: str) -> dict[str, Any]:
    """解析 `os-release` 风格键值文本（`/etc/os-release` 与 `/etc/alpine-release` 同源）。

    纯函数：接受任意以换行分隔的 `KEY=value` 文本，`value` 两侧引号会被去掉。
    畸形行（无 `=`、空键）一律跳过，不抛异常。
    """
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key] = value.strip().strip("'\"")
    return out


def digest_release_file(path: str, text: str) -> dict[str, str]:
    """处理「单行版本号文件」（如 `/etc/alpine-release` 只有 `3.20.3`）。"""
    first = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            first = line
            break
    if not first:
        return {}
    name = "Alpine Linux" if "alpine" in path else path.rsplit("/", 1)[-1].replace("-release", "")
    return {"NAME": name, "VERSION_ID": first}


def pretty_name(info: dict[str, Any]) -> str:
    """人耳友好的系统名：`Arch Linux 2024.09` / `Debian 12` / `Linux 6.8.0`。"""
    name = str(info.get("name") or "").strip()
    version = str(info.get("version") or "").strip()
    if not name:
        os_name = str(info.get("os") or "unknown")
        # "windows 11" / "linux 6.8.0" 首字母大写更好读；未知值原样保留
        os_name = os_name.capitalize() if os_name.islower() else os_name
        return f"{os_name} {version}".strip()
    # PRETTY_NAME 里通常已经含版本号，避免 "Arch Linux 2024.09 2024.09"
    if version and version in name:
        return name
    return f"{name} {version}".strip()


def detect_os(open_file, path_exists) -> dict[str, Any]:
    """采集系统信息。

    `open_file` / `path_exists` 由调用方注入（真实实现见 `probes/env_platform.py`），
    这样本函数可以在测试里完全脱离宿主机文件系统。
    """
    info: dict[str, Any] = {
        "os": "unknown",
        "name": "",
        "version": "",
        "id": "",
        "like": [],
        "pretty": "",
        "source": "",
    }
    if platform is not None:
        try:
            info["os"] = canonical_os(platform.system())
            # `version` 在无 os-release 时兜底为内核版本
            info["version"] = str(platform.release() or "")
        except Exception:  # noqa: BLE001 —— 采集失败只能是"信息缺失"，不能影响巡检
            logger.debug("[guardian] platform.system/release 失败", exc_info=True)

    data: dict[str, str] = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        raw = open_file(path) if path_exists(path) else ""
        data = digest_os_release(raw)
        if data.get("ID") or data.get("NAME"):
            info["source"] = path
            break

    if not data.get("ID") and not data.get("NAME"):
        for path, fallback_name in RELEASE_MARKERS:
            if not path_exists(path):
                continue
            merged = digest_release_file(path, open_file(path))
            if merged:
                merged.setdefault("ID", fallback_name.split()[0].lower())
                data = merged
                info["source"] = path
                break

    if data:
        info["name"] = str(data.get("NAME") or data.get("ID") or "")
        info["id"] = str(data.get("ID") or "")
        info["like"] = [x for x in str(data.get("ID_LIKE") or "").split() if x]
        info["pretty"] = str(data.get("PRETTY_NAME") or "") or info["name"]
        if data.get("VERSION_ID"):
            info["version"] = str(data["VERSION_ID"])
    if not info["source"]:
        info["source"] = "uname"
    # Windows：`platform.release()` 返回 "11"，没有 NAME 时只能拼出 "windows 11"。
    # 补一次 `win32_ver()` 拿 "Windows-11-10.0.22631-SP0"，让 distro 字段更像系统名。
    if info["os"] == "windows" and not info["name"] and platform is not None:
        try:
            parts = platform.win32_ver()
        except Exception:  # noqa: BLE001
            parts = ()
        if parts and parts[0] and parts[0] != "post2008Server":
            info["name"] = f"Windows {parts[0]}"
            info["version"] = str(parts[1] or "")
    return info
