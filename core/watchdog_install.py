"""Guardian 守护系统 —— 宿主看门狗自动安装（容器侧发起）。

**为什么需要这个模块**：看门狗（`deploy/guardian-watchdog.py`）必须跑在**宿主机**
上，因为它要 `docker compose stop/start` 那些容器；而插件跑在**容器里**。两者之
间隔着一层容器边界，所以"装插件时顺便把看门狗装上"不能靠普通的文件写入完成。

**怎么做到（Linux 宿主）**：容器里挂着 `/var/run/docker.sock`（这个部署本来就有，
插件已经用它探测容器状态）。通过 Docker API 创建一个**一次性容器**，把宿主机的
`/` 挂到 `/host`，再在临时容器里执行宿主机侧的 `install-watchdog.sh`，脚本就能
真的写到宿主机磁盘、注册 systemd timer。

**Windows 宿主做不到全自动**：Docker Desktop 下的容器是 Linux 环境，
跑不了 `powershell.exe`（没有 wine，也没有 binfmt 注册）。所以本模块在 Windows
上**只给出可复制的手动命令**，绝不假装装好了。

**这不是提权，是既有权限的显式化**：挂了 `docker.sock` 的容器本质上已经等同于
宿主机 root（可以 `docker run -v /:/host --privileged`）。本模块只是把这件事用
在"安装我们自己的看门狗"上，并且：
  * 安装脚本随插件分发，内容可审计（`deploy/install-watchdog.sh`）
  * 只在配置允许时执行（`safety.auto_install_watchdog`，默认开）
  * 宿主机已有看门狗时**不重复安装**
  * 临时容器以 `NetworkMode=none` 运行、用完即删
  * 任何一步失败都只降级为"给出可复制的手动命令"，绝不影响插件启动

**本模块不得 `import astrbot`**（I6 域分离）。
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import time
from typing import Any

DOCKER_SOCK = "/var/run/docker.sock"
#: 临时容器用的镜像。必须足够小且**已在宿主机上**——自动安装不该为此拉取镜像
#: （在离线机器上会卡住很久）。按顺序尝试，第一个存在的就用。
CANDIDATE_IMAGES = (
    "alpine:latest",
    "busybox:latest",
    "debian:bookworm-slim",
)
#: 临时容器名字前缀，便于失败后人工清理
CONTAINER_PREFIX = "guardian-watchdog-installer-"

#: Windows 上的 Python **没有** `socket.AF_UNIX`（AttributeError，不是连接失败）。
#: 必须先探测再用，否则在 Windows 原生跑单测/本地调试时会直接抛异常，
#: 破坏 `install()` 的"永不抛"承诺。
AF_UNIX = getattr(socket, "AF_UNIX", None)


class DockerUnixConnection(http.client.HTTPConnection):
    """通过 unix socket 说 HTTP —— Docker API 就是这么暴露的。

    标准库的 `HTTPConnection` 只会 TCP，这里把 `connect()` 换成 unix socket。
    比手写 socket 拼 HTTP 可靠得多：`/containers/json` 等接口返回分块编码
    （`Transfer-Encoding: chunked`），手写读写很容易挂住。
    """

    def __init__(self, sock_path: str = DOCKER_SOCK, timeout: float = 30.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self) -> None:
        if AF_UNIX is None:      # Windows 原生 Python
            raise OSError("当前平台不支持 unix socket，无法访问 Docker socket")
        sock = socket.socket(AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.sock_path)
        self.sock = sock


def socket_available(sock_path: str = DOCKER_SOCK) -> bool:
    """能否连上 Docker socket。连不上就别装了，退化成手动提示。

    平台不支持 unix socket（Windows 原生）、文件不存在、连接被拒——三种情况
    都返回 False，调用方一律走手动提示分支。
    """
    if AF_UNIX is None:
        return False
    if not os.path.exists(sock_path):
        return False
    sock = socket.socket(AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    try:
        sock.connect(sock_path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _api(method: str, path: str, body: Any = None,
         sock_path: str = DOCKER_SOCK, timeout: float = 60.0
         ) -> tuple[int, bytes]:
    """发一个 Docker API 请求。返回 (status, body)。不吞异常。"""
    conn = DockerUnixConnection(sock_path, timeout=timeout)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def first_available_image(sock_path: str = DOCKER_SOCK) -> str | None:
    """从候选镜像里挑一个宿主机上已经存在的。

    刻意**不**自动 `pull`：自动安装的目标是"少一步手工"，不是"替用户下载几百 MB"。
    离线机器上 pull 会静静挂很久，比直接给出手动命令更糟。
    """
    try:
        status, raw = _api("GET", "/images/json", sock_path=sock_path)
    except (OSError, AttributeError):
        return None
    if status != 200:
        return None
    try:
        images = json.loads(raw)
    except (ValueError, TypeError):
        return None
    present: set[str] = set()
    for item in images or []:
        for tag in (item.get("RepoTags") or []):
            present.add(tag)
    for candidate in CANDIDATE_IMAGES:
        if candidate in present:
            return candidate
    return None


def host_is_windows(sock_path: str = DOCKER_SOCK) -> bool:
    """判断宿主机是不是 Windows。

    `/info` 的 `OperatingSystem` 在 Docker Desktop 上是 `Docker Desktop`，
    这是最直接的信号。**不能**用 `OSType`——Windows 上跑 Docker Desktop 时
    它也是 `linux`（那是容器内核类型，不是宿主系统）。

    socket 不可用（Windows 原生等）时退回到路径嗅探，再不成就假定非 Windows。
    """
    try:
        status, raw = _api("GET", "/info", sock_path=sock_path, timeout=15.0)
        if status == 200:
            info = json.loads(raw)
            os_desc = str(info.get("OperatingSystem") or "").lower()
            name = str(info.get("Name") or "").lower()
            if "desktop" in os_desc or "windows" in os_desc or "desktop" in name:
                return True
    except (OSError, ValueError, TypeError, AttributeError):
        # AttributeError：Windows 原生 Python 的 socket 连 AF_UNIX 都没有
        pass
    # 退路：Windows 宿主上才存在的盘符挂载点
    return any(os.path.exists(p) for p in ("/host_mnt/c", "/run/desktop/mnt/host/c"))


def manual_hint(windows: bool) -> str:
    """给人复制的手动安装命令（自动安装不可用时）。"""
    if windows:
        return ("在宿主机（不是容器内）以管理员身份打开 PowerShell，运行：\n"
                "  cd <插件目录>\\deploy\n"
                "  powershell -ExecutionPolicy Bypass -File .\\install-watchdog.ps1")
    return ("在宿主机（不是容器内）运行：\n"
            "  sudo <插件目录>/deploy/install-watchdog.sh")


def _demux_log(raw: bytes) -> str:
    """解析 Docker 的复用日志流（每帧 8 字节头 + payload）。

    非 TTY 容器的 `/logs` 是这种帧格式；直接 decode 会在每行前面留一堆控制字符。
    """
    if not raw:
        return ""
    out = bytearray()
    i = 0
    n = len(raw)
    parsed = False
    while i + 8 <= n:
        if raw[i] in (0, 1, 2) and raw[i + 1] == 0 and raw[i + 2] == 0 \
                and raw[i + 3] == 0:
            length = int.from_bytes(raw[i + 4:i + 8], "big")
            i += 8
            out += raw[i:i + length]
            i += length
            parsed = True
        else:
            break
    return bytes(out).decode("utf-8", "replace") if parsed \
        else raw.decode("utf-8", "replace")


def already_installed(ledger_path: str, stale_sec: float = 300.0,
                      now: float | None = None) -> bool:
    """看门狗账本存在且足够新 → 认为已安装且在跑。

    只看"文件存在"不够：一周前留下的死账本也会让这里为真。所以带上新鲜度。
    """
    try:
        mtime = os.stat(ledger_path).st_mtime
    except (OSError, TypeError, ValueError):
        # TypeError/ValueError：ledger_path 为 None 或含非法字符。调用方可能
        # 传来未初始化的配置值，这种时候"没装"才是正确判断，不该崩。
        return False
    now = time.time() if now is None else now
    return abs(now - mtime) <= stale_sec * 12   # 12 个 tick 的宽限


def _run_container(spec: dict[str, Any], sock_path: str = DOCKER_SOCK,
                   timeout: float = 120.0) -> tuple[bool, str]:
    """创建 → 启动 → 等结束 → 取日志 → 删除。返回 (ok, 输出)。

    无论中间哪一步失败都会尝试删除容器：自动安装失败时最不该留下的就是
    一堆 `guardian-watchdog-installer-*` 垃圾容器。
    """
    name = f"{CONTAINER_PREFIX}{int(time.time())}"
    status, raw = _api("POST", f"/containers/create?name={name}", spec,
                       sock_path=sock_path)
    if status not in (200, 201):
        return False, f"创建临时容器失败（HTTP {status}）：{raw[:300]!r}"
    try:
        cid = json.loads(raw).get("Id")
    except (ValueError, TypeError):
        return False, f"创建临时容器返回无法解析：{raw[:300]!r}"
    if not cid:
        return False, "创建临时容器未返回 Id"

    try:
        status, raw = _api("POST", f"/containers/{cid}/start", sock_path=sock_path)
        if status not in (204, 304):
            return False, f"启动临时容器失败（HTTP {status}）：{raw[:300]!r}"

        deadline = time.time() + timeout
        exit_code = None
        while time.time() < deadline:
            status, raw = _api("GET", f"/containers/{cid}/json", sock_path=sock_path)
            if status == 200:
                try:
                    state = (json.loads(raw) or {}).get("State") or {}
                except (ValueError, TypeError):
                    state = {}
                if not state.get("Running", False):
                    exit_code = state.get("ExitCode")
                    break
            time.sleep(1.0)
        if exit_code is None:
            return False, f"临时容器 {timeout:.0f} 秒内未结束（已超时）"

        out = b""
        with contextlib.suppress(OSError):
            _, out = _api("GET", f"/containers/{cid}/logs?stdout=1&stderr=1",
                          sock_path=sock_path, timeout=30.0)
        text = _demux_log(out)
        return exit_code == 0, (text or f"（退出码 {exit_code}，无输出）")
    finally:
        with contextlib.suppress(OSError):
            _api("DELETE", f"/containers/{cid}?force=1&v=1", sock_path=sock_path)


def install(deploy_script: str, ledger_path: str,
            sock_path: str = DOCKER_SOCK,
            compose_dir_host: str = "/opt/astrbot-stack",
            data_root_host: str | None = None,
            targets: list[str] | None = None,
            force: bool = False,
            timeout: float = 180.0) -> dict[str, Any]:
    """尝试在宿主机上安装看门狗。**永不抛异常**。

    参数里凡是带 `_host` 后缀的，都必须是**宿主机视角的路径**，不是容器内路径。
    临时容器是另一个容器，它看不见本容器内的任何目录——早期实现拿容器内路径
    去挂载，结果脚本根本找不到（`not found`）。

    安装脚本本身不进宿主机挂载：它被**内联进 `Cmd`**，通过 stdin 传给 `sh`。
    这样不需要为脚本单独找一个宿主机可见的位置，也就没有第二处路径映射会出错。

    返回字典（给调用方记日志/审计用）：
      - `ok`: 是否成功
      - `skipped`: 是否因"已装"而跳过
      - `reason`: 失败/跳过原因（人话）
      - `hint`: 失败时给人复制的手动命令
      - `output`: 宿主机侧脚本的原始输出（截断）
    """
    result: dict[str, Any] = {
        "ok": False, "skipped": False, "reason": "", "hint": "", "output": "",
    }

    # 入参规整：None / 空串都当成"没有部署脚本"，走明确的失败分支。
    # 这一步必须在任何文件操作之前——否则 os.stat(None) 会抛 TypeError
    # 穿过整个函数，正好破坏"永不抛异常"这条契约。
    if not deploy_script:
        result.update(reason="未提供安装脚本路径（deploy/install-watchdog.sh 缺失？）",
                      hint=manual_hint(False))
        return result
    compose_dir_host = compose_dir_host or "/opt/astrbot-stack"

    if not force and already_installed(ledger_path or ""):
        result.update(ok=True, skipped=True,
                      reason="宿主看门狗账本新鲜，判定已安装，跳过")
        return result

    if not socket_available(sock_path):
        result.update(
            reason="插件无法访问 Docker socket，不能在宿主机上安装看门狗"
                   "（需要把 /var/run/docker.sock 挂进容器）",
            hint=manual_hint(False))
        return result

    if host_is_windows(sock_path):
        # 明确的限制，不是"还没实现"：Linux 容器无法执行 powershell.exe。
        # 宁可如实说清楚，也不要写一堆半成品文件让用户以为装好了。
        result.update(
            reason="检测到 Windows 宿主机：容器内无法注册 Windows 计划任务"
                   "（Linux 容器跑不了 powershell.exe）。请手动执行一次安装脚本",
            hint=manual_hint(True))
        return result

    # 脚本内容内联进 Cmd —— 不做第二处路径映射
    try:
        with open(deploy_script, encoding="utf-8") as fp:
            script = fp.read()
    except OSError as exc:
        result.update(reason=f"读不到安装脚本 {deploy_script}：{exc}",
                      hint=manual_hint(False))
        return result

    image = first_available_image(sock_path)
    if image is None:
        result.update(
            reason="宿主机上没有可用于临时容器的镜像（试过 "
                   f"{', '.join(CANDIDATE_IMAGES)}）；不会自动拉取镜像",
            hint=manual_hint(False))
        return result

    args = ["--compose-dir", compose_dir_host]
    if data_root_host:
        args.extend(["--data-root", data_root_host])
    if targets:
        args.extend(["--targets", ",".join(targets)])

    # 脚本从 stdin 读入后执行；宿主机根挂到 /host。
    # `sh -s` 让内联脚本与参数分开，避免引号地狱。
    wrapper = f"cat > /tmp/inst.sh <<'GUARDIAN_EOF'\n{script}\nGUARDIAN_EOF\n" \
              f"chmod +x /tmp/inst.sh\n" \
              f"sh /tmp/inst.sh {' '.join(args)} 2>&1\n"

    spec = {
        "Image": image,
        "Cmd": ["sh", "-c", wrapper],
        "WorkingDir": "/",
        "Tty": False,
        "HostConfig": {
            # 宿主机根可写挂载：安装脚本要写 /etc/systemd、/usr/local/bin、/var/log
            "Binds": ["/:/host:rw"],
            "AutoRemove": False,
            "NetworkMode": "none",
            "Privileged": False,
            "PidMode": "host",       # systemctl 需要看到宿主的 systemd
        },
    }

    try:
        ok, out = _run_container(spec, sock_path, timeout)
        result["output"] = out[-4000:]
        if ok:
            result.update(ok=True, reason="宿主看门狗已安装并启用（systemd timer）")
        else:
            result.update(reason="宿主机侧安装脚本执行失败",
                          hint=manual_hint(False))
    except Exception as exc:  # noqa: BLE001 - 自动安装绝不能让插件起不来
        result.update(reason=f"自动安装过程异常：{type(exc).__name__}: {exc}",
                      hint=manual_hint(False))
    return result
