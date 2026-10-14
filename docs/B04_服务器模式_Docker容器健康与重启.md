# B04 · 服务器模式：Docker 容器健康与重启

> 上游契约：B00 §3/§4/§6/§7/§10。关联：B03（探针框架）、B06（OOM 取证）、B12（安全熔断）、B13（看门狗）、C04（验收）。版本 v0.1.0 | 2026-10-13 | 模式 `server`

## 1. 目标与范围

| 编号 | 目标 | 判据 |
| --- | --- | --- |
| G1 | 经 `/var/run/docker.sock` 走 Engine HTTP API 观测容器 | 容器内无需 docker CLI |
| G2 | 容器停止/崩溃后自动拉起 | 2×`check_interval` 内恢复 `running` |
| G3 | 判定与动作分离，可 dry-run / 可 mock | 单测不依赖真实 Docker |
| G4 | 自重启不形成风暴、不中断在途请求 | 默认 watchdog 策略 |
| G5 | daemon 不可达只告警不动作 | `UNKNOWN` + 零副作用 |

范围内：容器存在性/运行态/RestartCount/OOMKilled/healthcheck、restart/start 动作、本体自检、socket 可用性。范围外：镜像构建拉取、Compose 改写、卷与网络管理、跨主机集群、日志分析（B08）。

## 2. Docker 交互架构

```
  astrbot 容器
  ┌────────────────────────────────────────────┐
  │ HealthEngine                               │
  │  DockerDaemonProbe / ContainerHealthProbe  │
  │  ContainerMemoryProbe / SelfHealthProbe    │
  │     │ deps["docker"]                       │
  │     ▼  utils/dockerclient.py               │
  │        ├─ aiohttp.UnixConnector（优先）    │
  │        └─ DockerCliFallback（可选降级）    │
  └─────┬────────────────────┬─────────────────┘
        │ HTTP over UDS      │ 子进程（可选）
        ▼                    ▼
  /var/run/docker.sock     docker CLI
        └────────┬───────────┘
                 ▼  Docker Engine（宿主 daemon）
```

| 维度 | Socket HTTP API（默认） | CLI fallback（可选） |
| --- | --- | --- |
| 依赖 / 数据 | 仅 `aiohttp`，JSON 字段稳定 | 容器内需 `docker`，需解析文本 |
| 注入面 / 开关 | 无 shell 拼接；始终优先 | verb 白名单 + 参数仅容器名/数值；`docker.use_cli_fallback=true` 且 socket 不可用 |

版本协商：`GET /_ping`（无前缀）→ `GET /version` 取 `ApiVersion` → 后续请求前缀 `/v1.44`；失败回退 `docker.api_version`。

| 用途 | 方法与路径 | 关键字段 / 语义 |
| --- | --- | --- |
| 存活 / 版本 / 主机 | `GET /_ping`、`GET /version`、`GET /info` | `OK`、`ApiVersion`、`Containers` |
| 列表 / 详情 | `GET /containers/json?all=1`、`GET /containers/{id}/json` | `State.Status/Running/RestartCount/OOMKilled/Health.Status/StartedAt`、`HostConfig.RestartPolicy` |
| 重启 / 启动 / 停止 | `POST /containers/{id}/restart?t=10`、`start`、`stop?t=10` | 204；304=已在目标态，视为幂等成功 |
| 采样 / 事件 / 执行 | `GET .../stats?stream=false`、`GET /events`、`POST .../exec` + `POST /exec/{id}/start` | `memory_stats`/`cpu_stats`（B06）、`Action`（B10 订阅）、`Id`/`ExitCode` |

## 3. `utils/dockerclient.py`（完整代码）

> **与 B03 的接口约定**：本节及 §4–§6 的探针均继承 `probes.base.Probe`，实现 `async def _check() -> ProbeResult`，并用 `@register_probe("<name>")` 注册（B03 §4/§11）；构造函数签名为 `(config, deps, target="")`，其中 `deps` 提供 `docker`（`DockerClient`）、`state`（`StateStore`）、`log`（logger）、`context`（AstrBot Context，可选）。超时包装与异常兜底由基类 `check()` 统一处理，子类不再重复捕获 `TimeoutError`。

```python
# src/astrbot_plugin_guardian/utils/dockerclient.py
from __future__ import annotations
import asyncio, json, logging
from typing import Any
import aiohttp
DEFAULT_SOCKET, DEFAULT_API_VERSION, DEFAULT_TIMEOUT, PING_TIMEOUT, RAW_LIMIT = \
    "/var/run/docker.sock", "v1.44", 8.0, 3.0, 4096
class DockerUnavailable(Exception):
    """socket 不存在/权限不足/daemon 无响应：环境不可用 -> 判定 UNKNOWN，不动作。"""
class DockerError(Exception):
    """daemon 可达但非 2xx；status=404 表示容器不存在。"""
    def __init__(self, status: int, message: str, path: str = "") -> None:
        super().__init__(f"HTTP {status} {path} {message}")
        self.status, self.message, self.path = status, message, path
def shorten(obj: Any, limit: int = RAW_LIMIT) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "...<truncated>"
class DockerClient:
    """Docker Engine HTTP API 客户端（Unix socket + aiohttp），单实例复用连接。"""
    def __init__(self, socket_path: str = DEFAULT_SOCKET, api_version: str = DEFAULT_API_VERSION,
                 timeout: float = DEFAULT_TIMEOUT, use_cli_fallback: bool = False, logger=None) -> None:
        self.socket_path, self.timeout = socket_path, float(timeout)
        self.api_version = api_version or DEFAULT_API_VERSION
        self.log = logger or logging.getLogger("guardian.docker")
        self._connector = self._session = self._negotiated = None
        self._lock = asyncio.Lock()
        # CLI 降级后端在 utils/shell.py::DockerCliFallback（异步子进程 + verb 白名单）
        self._cli = DockerCliFallback(socket_path, logger=self.log) if use_cli_fallback else None
    async def _session_get(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._connector = aiohttp.UnixConnector(path=self.socket_path)
                self._session = aiohttp.ClientSession(connector=self._connector,
                    timeout=aiohttp.ClientTimeout(total=self.timeout, connect=3.0),
                    headers={"User-Agent": "astrbot-plugin-guardian/0.1"})
            return self._session
    async def close(self) -> None:
        async with self._lock:
            if self._session is not None and not self._session.closed: await self._session.close()
            self._session, self._connector = None, None
    async def __aenter__(self): await self._session_get(); return self
    async def __aexit__(self, *exc): await self.close()
    async def _request(self, method: str, path: str, *, params=None, body=None, versioned: bool = True,
                       timeout: float | None = None, tolerate_disconnect: bool = False) -> Any:
        session = await self._session_get()
        if versioned: path = f"/{await self._api_version()}{path}"
        try:
            async with session.request(method, f"http://localhost{path}", params=params, json=body,
                    timeout=aiohttp.ClientTimeout(total=timeout or self.timeout)) as resp:
                text = await resp.text()
                if resp.status >= 400: raise DockerError(resp.status, shorten(text, 512), path)
                if not text: return None
                return json.loads(text) if "application/json" in resp.headers.get("Content-Type", "") else text
        except (aiohttp.ClientConnectorError, FileNotFoundError, PermissionError, ConnectionResetError) as exc:
            raise DockerUnavailable(f"socket 不可用 {self.socket_path}: {exc}") from exc
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            if tolerate_disconnect: return None
            raise DockerUnavailable(f"{type(exc).__name__} {method} {path}: {exc}") from exc
    async def _api_version(self) -> str:
        if self._negotiated: return self._negotiated
        try:
            ver = await self._request("GET", "/version", versioned=False, timeout=3.0) or {}
            api = str(ver.get("ApiVersion") or "").strip()
            self._negotiated = f"v{api}" if api else self.api_version
        except (DockerUnavailable, DockerError) as exc:
            self.log.warning("API 版本协商失败，回退 %s：%s", self.api_version, exc)
            self._negotiated = self.api_version
        return self._negotiated
    async def ping(self, timeout: float = PING_TIMEOUT) -> bool:
        return str(await self._request("GET", "/_ping", versioned=False, timeout=timeout)).strip() == "OK"
    async def version(self) -> dict: return await self._request("GET", "/version", versioned=False) or {}
    async def info(self) -> dict: return await self._request("GET", "/info") or {}
    async def inspect(self, name: str) -> dict: return await self._request("GET", f"/containers/{name}/json") or {}
    async def stats(self, name: str) -> dict:
        return await self._request("GET", f"/containers/{name}/stats", params={"stream": "false"}) or {}
    async def list_containers(self, all_: bool = True, filters: dict | None = None) -> list[dict]:
        p: dict = {"all": 1 if all_ else 0}
        if filters: p["filters"] = json.dumps(filters)
        return await self._request("GET", "/containers/json", params=p) or []
    async def restart(self, name: str, timeout: int = 10) -> None:
        await self._request("POST", f"/containers/{name}/restart", params={"t": int(timeout)},
                            timeout=max(self.timeout, timeout + 10), tolerate_disconnect=True)
    async def start(self, name: str) -> None:
        await self._request("POST", f"/containers/{name}/start", tolerate_disconnect=True)
    async def stop(self, name: str, timeout: int = 10) -> None:
        await self._request("POST", f"/containers/{name}/stop", params={"t": int(timeout)},
                            timeout=max(self.timeout, timeout + 10), tolerate_disconnect=True)
    async def exec_run(self, name: str, cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
        created = await self._request("POST", f"/containers/{name}/exec", body={
            "AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": [str(c) for c in cmd]}) or {}
        eid = created.get("Id")
        if not eid: raise DockerError(500, "exec 创建失败：无 Id", f"/containers/{name}/exec")
        out = await self._request("POST", f"/exec/{eid}/start", body={"Detach": False, "Tty": False}, timeout=timeout)
        return int((await self._request("GET", f"/exec/{eid}/json") or {}).get("ExitCode") or 0), shorten(out)
```

`main.py` 启动时创建一次并注入 `deps["docker"]`；`terminate()` 中 `await deps["docker"].close()`。

## 4. `probes/docker_daemon.py`（完整代码）

```python
# src/astrbot_plugin_guardian/probes/docker_daemon.py
from __future__ import annotations
import os, stat, time
from ..core.models import ProbeResult, Severity, Status
from ..core.registry import register_probe
from ..utils.dockerclient import DockerError, DockerUnavailable
from .base import Probe
@register_probe("docker_daemon")
class DockerDaemonProbe(Probe):
    """判定 Docker daemon 是否可用；不可用时全模式降级 UNKNOWN + 告警（不动作）。"""
    name = "docker_daemon"
    def __init__(self, config, deps, target: str = "docker") -> None:
        super().__init__(config, deps, target=target)
        self.docker, self.log = deps["docker"], deps.get("log")
    async def _check(self) -> ProbeResult:
        ts, sock = time.time(), self.config.docker_socket
        m: dict = {"socket_path": sock, "socket_exists": False, "socket_mode": "", "api_version": "",
                   "daemon_version": "", "containers": -1, "reachable": 0,
                   "cli_fallback": self.config.docker_use_cli_fallback}
        if not os.path.exists(sock):
            return self._r(ts, Status.UNKNOWN, Severity.WARN,
                           f"Docker socket 不存在：{sock}（检查 compose 的 /var/run/docker.sock 挂载）", m)
        try:
            st = os.stat(sock)
            m.update({"socket_exists": True, "socket_mode": oct(stat.S_IMODE(st.st_mode)),
                      "socket_uid_gid": f"{st.st_uid}:{st.st_gid}", "socket_writable": os.access(sock, os.W_OK)})
        except OSError as exc:
            m["stat_error"] = str(exc)[:120]
        try:
            if not await self.docker.ping():
                return self._r(ts, Status.UNKNOWN, Severity.WARN, "/_ping 未返回 OK", m)
            ver = await self.docker.version()
            m["reachable"] = 1
            m["api_version"], m["daemon_version"] = str(ver.get("ApiVersion") or ""), str(ver.get("Version") or "")
            try:
                info = await self.docker.info()
                m["containers"] = int(info.get("Containers") or -1)
                m["host_os"] = str(info.get("OperatingSystem") or "")
            except (DockerUnavailable, DockerError):
                pass
        except DockerUnavailable as exc:
            hint = "权限不足：需 group_add docker 或调整 socket 属组" if m.get("socket_exists") else "socket 未挂载"
            return self._r(ts, Status.UNKNOWN, Severity.WARN, f"Docker daemon 不可达（{hint}）：{exc}", m)
        status, sev = Status.OK, Severity.INFO
        msg = f"daemon 正常（API {m['api_version']} / {m['daemon_version']}），容器数 {m['containers']}"
        if not m.get("socket_writable", True):
            status, sev, msg = Status.WARN, Severity.WARN, msg + "；socket 不可写，动作类探针将失败"
        return self._r(ts, status, sev, msg, m)
    def _r(self, ts, status, sev, msg, metrics) -> ProbeResult:
        return ProbeResult(probe=self.name, target="docker", status=status,
                           severity=sev, message=msg, metrics=metrics, ts=ts, raw=None)
```

## 5. `probes/container_health.py`（完整代码）

引擎按 target 实例化（`ContainerHealthProbe(config, deps, "napcat")`），`check()` 返回单个 `ProbeResult`（B03 §3 基类负责超时与异常兜底，子类实现 `_check()`）。

```python
# src/astrbot_plugin_guardian/probes/container_health.py
from __future__ import annotations
import time
from datetime import datetime, timezone
from ..core.models import ProbeResult, Severity, Status
from ..core.registry import register_probe
from ..utils.dockerclient import DockerError, DockerUnavailable
from .base import Probe
RESTART_LOOP_WINDOW_SEC, RESTART_LOOP_WARN, RESTART_LOOP_CRIT, STARTING_GRACE_SEC = 900, 1, 3, 60
@register_probe("container_health")
class ContainerHealthProbe(Probe):
    """单个受管容器的健康探针；引擎按 target 实例化（B03 §11 注册表）。"""
    name = "container_health"
    def __init__(self, config, deps, target: str) -> None:
        super().__init__(config, deps, target=target)
        self.docker, self.state, self.log = deps["docker"], deps["state"], deps.get("log")
    async def _check(self) -> ProbeResult:
        ts = time.time()
        try:
            data = await self.docker.inspect(self.target)
        except DockerError as exc:
            if exc.status == 404:
                return self._r(ts, Status.CRIT, Severity.CRIT, f"容器 {self.target} 不存在（可能被删除或改名）",
                               {"exists": False, "suggested": "start_container"}, {"http_status": 404})
            return self._r(ts, Status.UNKNOWN, Severity.WARN, f"inspect 失败：{exc.message[:120]}",
                           {"exists": None}, {"http_status": exc.status})
        except DockerUnavailable as exc:
            return self._r(ts, Status.UNKNOWN, Severity.WARN, "Docker 不可达，无法判定容器状态",
                           {"exists": None, "error": str(exc)[:160]}, None)
        state, host = data.get("State") or {}, data.get("HostConfig") or {}
        st, running = str(state.get("Status") or "unknown"), bool(state.get("Running"))
        oom, rc = bool(state.get("OOMKilled")), int(state.get("RestartCount") or 0)
        health = str((state.get("Health") or {}).get("Status") or "")
        started_at = str(state.get("StartedAt") or "")
        uptime, delta = self._uptime(started_at, ts), await self._restart_delta(rc, ts)
        metrics = {"exists": True, "status": st, "running": running, "restart_count": rc, "restart_delta": delta,
                   "oom_killed": oom, "health_unhealthy": int(health == "unhealthy"), "health": health,
                   "uptime_sec": uptime, "started_at": started_at,
                   "restart_policy": str((host.get("RestartPolicy") or {}).get("Name") or "")}
        raw = {"State": {"Status": st, "Running": running, "RestartCount": rc, "OOMKilled": oom,
                         "Health": {"Status": health}, "StartedAt": started_at}}
        r = lambda s, v, msg, **extra: self._r(ts, s, v, msg, {**metrics, "suggested": None, **extra}, raw)
        # 判定顺序：短路，先到先得
        if st == "restarting":
            return r(Status.WARN, Severity.WARN, "容器正在重启中，本次不触发动作（重启窗口抑制）")
        if not running:
            if st == "paused": return r(Status.WARN, Severity.WARN, "容器处于 paused")
            if st in ("created", "removing") and uptime < STARTING_GRACE_SEC:
                return r(Status.WARN, Severity.WARN, f"容器状态转换中（{st}）")
            return r(Status.CRIT, Severity.CRIT, f"容器未运行（Status={st}）", suggested="start_container")
        if oom:
            return r(Status.CRIT, Severity.CRIT,
                     "上次退出因 OOM 被杀（OOMKilled=true），疑似内存泄漏（取证见 B06）", forensics="oom")
        if health == "unhealthy":
            return r(Status.CRIT, Severity.CRIT, "healthcheck=unhealthy：进程存活但服务不可用",
                     suggested="restart_container")
        if delta >= RESTART_LOOP_CRIT:
            return r(Status.CRIT, Severity.CRIT,
                     f"{RESTART_LOOP_WINDOW_SEC}s 内新增 {delta} 次重启，疑似 CrashLoop，暂停自动动作", loop=True)
        if delta >= RESTART_LOOP_WARN:
            return r(Status.WARN, Severity.WARN, f"检测到新增重启 {delta} 次（累计 {rc}）")
        if health == "starting": return r(Status.WARN, Severity.WARN, "healthcheck 处于 starting")
        return r(Status.OK, Severity.INFO, f"运行正常，已运行 {uptime}s")
    def _r(self, ts, status, sev, msg, metrics, raw) -> ProbeResult:
        return ProbeResult(probe=self.name, target=self.target, status=status,
                           severity=sev, message=msg, metrics=metrics, ts=ts, raw=raw)
    @staticmethod
    def _uptime(started_at: str, now: float) -> int:
        try:
            s = started_at.split(".")[0].replace("Z", "+00:00")
            return max(0, int(now - datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()))
        except Exception:
            return -1
    async def _restart_delta(self, restart_count: int, now: float) -> int:
        """与上次采样比较 RestartCount，并维护窗口内重启时间戳序列（崩溃循环检测）。"""
        key = f"probe.container_health.{self.target}"
        prev = await self.state.get_kv(key, {"restart_count": restart_count, "marks": []})
        marks = [t for t in prev.get("marks", []) if now - t <= RESTART_LOOP_WINDOW_SEC]
        grew = restart_count - int(prev.get("restart_count", restart_count))
        if grew > 0: marks.extend([now] * grew)
        await self.state.set_kv(key, {"restart_count": restart_count, "marks": marks, "ts": now})
        return len(marks)
```

## 6. `probes/self_health.py`（完整代码）

覆盖 socket 可用性、本体容器状态、数据盘容量、本体 CPU。阈值以模块常量实现，**不新增 B00 §7 配置键**。

```python
# src/astrbot_plugin_guardian/probes/self_health.py
from __future__ import annotations
import os, shutil, time
from ..core.models import ProbeResult, Severity, Status
from ..core.registry import register_probe
from ..utils.dockerclient import DockerError, DockerUnavailable
from .base import Probe
DISK_WARN_PCT, DISK_CRIT_PCT, CPU_WARN_PCT = 85.0, 95.0, 90.0
@register_probe("self_health")
class SelfHealthProbe(Probe):
    """本体自检：socket 可用性、本体容器状态、数据盘容量、本体 CPU/内存。"""
    name = "self_health"
    def __init__(self, config, deps, target: str = "self") -> None:
        super().__init__(config, deps, target=target)
        self.docker, self.state, self.log = deps["docker"], deps["state"], deps.get("log")
    async def _check(self) -> ProbeResult:
        ts, problems = time.time(), []
        m: dict = {"data_dir": str(self.config.data_dir)}
        sock = self.config.docker_socket                                  # 1) socket：动作能力前置条件
        m["socket_exists"] = os.path.exists(sock)
        m["socket_writable"] = os.access(sock, os.W_OK) if m["socket_exists"] else False
        if not m["socket_writable"]: problems.append("socket 不可写：自动重启能力不可用")
        try:                                                               # 2) 本体容器自检
            st = (await self.docker.inspect(self.config.container_astrbot)).get("State") or {}
            m.update({"self_running": bool(st.get("Running")), "self_restart_count": int(st.get("RestartCount") or 0),
                      "self_started_at": str(st.get("StartedAt") or "")})
            if not m["self_running"]: problems.append("本体容器未运行（异常：本探针仍在执行）")
        except (DockerError, DockerUnavailable) as exc:
            m["self_inspect_error"] = str(exc)[:120]
        try:                                                               # 3) 数据盘容量
            u = shutil.disk_usage(str(self.config.data_dir))
            pct = round(u.used / u.total * 100, 2)
            m.update({"disk_total_gb": round(u.total / 2**30, 2), "disk_pct": pct})
            if pct >= DISK_CRIT_PCT: problems.append(f"数据盘使用率 {pct}% >= {DISK_CRIT_PCT}%")
            elif pct >= DISK_WARN_PCT: problems.append(f"数据盘使用率偏高 {pct}%")
        except OSError as exc:
            m["disk_error"] = str(exc)[:120]
        try:                                                               # 4) 本体 CPU / 内存（失败不致命）
            stats = await self.docker.stats(self.config.container_astrbot)
            cpu = self._cpu_pct(stats)
            m["self_cpu_pct"] = cpu
            m["self_mem_mb"] = round(int((stats.get("memory_stats") or {}).get("usage") or 0) / 2**20, 1)
            if cpu is not None and cpu >= CPU_WARN_PCT: problems.append(f"本体 CPU {cpu}% >= {CPU_WARN_PCT}%")
        except (DockerError, DockerUnavailable) as exc:
            m["self_stats_error"] = str(exc)[:120]
        # B03 §10 清单中的插件运行态指标（与容器/宿主指标共用同一探针）
        m["loop_lag_ms"] = round(float(getattr(self.deps.get("runtime"), "loop_lag_ms", 0.0)), 2)
        m["task_count"] = int(getattr(self.deps.get("runtime"), "task_count", 0))
        m["last_scan_age"] = round(time.time() - float(await self.state.get_kv("engine.last_scan_ts", 0.0)), 1)
        if any(f">= {DISK_CRIT_PCT}%" in p for p in problems): status, sev = Status.CRIT, Severity.CRIT
        elif problems: status, sev = Status.WARN, Severity.WARN
        else: status, sev = Status.OK, Severity.INFO
        msg = "本体自检通过" if not problems else f"本体自检发现 {len(problems)} 项：" + "；".join(problems)
        return ProbeResult(probe=self.name, target="self", status=status, severity=sev,
                           message=msg, metrics=m, ts=ts, raw=None)
    @staticmethod
    def _cpu_pct(stats: dict) -> float | None:
        """docker stats 的 CPU 百分比：(cpu_delta / system_delta) * online_cpus * 100。"""
        try:
            cpu, pre = stats["cpu_stats"], stats.get("precpu_stats") or {}
            delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
            sysd = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
            if delta <= 0 or sysd <= 0: return None
            ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
            return round(delta / sysd * ncpu * 100, 2)
        except (KeyError, TypeError, ZeroDivisionError):
            return None
```

## 7. `actions/container_actions.py`（完整代码）

动作由 `HealthEngine._actions` 按 `ActionSpec.kind` 查表调用（B03 §6）；构造签名为 `(config, deps)`，`dry_run` 由引擎在调用前写入 `spec.dry_run`。

```python
# src/astrbot_plugin_guardian/actions/container_actions.py
from __future__ import annotations
import asyncio, json, os, time
from ..core.models import ActionResult, ActionSpec
from ..utils.dockerclient import DockerError, DockerUnavailable
RESTARTING_WINDOW_SEC, VERIFY_TIMEOUT_SEC, VERIFY_POLL_SEC = 90, 60, 3.0
class _BaseContainerAction:
    kind = ""
    def __init__(self, config, deps) -> None:
        self.config, self.docker, self.state = config, deps["docker"], deps["state"]
        self.log, self._inflight = deps.get("log"), set()
    async def _precheck(self, spec: ActionSpec) -> ActionSpec | None:
        """幂等预检：正在重启时返回 None（视为无需动作）。"""
        try:
            state = (await self.docker.inspect(spec.target)).get("State") or {}
        except DockerError as exc:
            if exc.status == 404: return spec
            raise
        if str(state.get("Status")) == "restarting": return None
        last = float(await self.state.get_kv(f"action.last_restart.{spec.target}", 0.0))
        if time.time() - last < RESTARTING_WINDOW_SEC and str(state.get("Status")) in ("created", "exited"):
            self.log.info("容器 %s 处于重启窗口内，跳过重复动作", spec.target)
            return None
        return spec
    async def _verify_running(self, name: str, timeout: float = VERIFY_TIMEOUT_SEC) -> tuple[bool, dict]:
        deadline, last = time.monotonic() + timeout, {}
        while time.monotonic() < deadline:
            try:
                st = (await self.docker.inspect(name)).get("State") or {}
                last = {"status": st.get("Status"), "running": st.get("Running"), "started_at": st.get("StartedAt")}
                if last.get("running"): return True, last
            except (DockerError, DockerUnavailable) as exc:
                last = {"error": str(exc)[:120]}
            await asyncio.sleep(VERIFY_POLL_SEC)
        return False, last
class RestartContainerAction(_BaseContainerAction):
    kind = "restart_container"
    async def execute(self, spec: ActionSpec) -> ActionResult:
        t0 = time.perf_counter()
        name, wait = spec.target, int(spec.params.get("timeout", 10))
        ms = lambda: int((time.perf_counter() - t0) * 1000)
        if spec.dry_run:
            return ActionResult(ok=True, detail=f"[dry-run] 将重启容器 {name}（t={wait}s）", elapsed_ms=ms())
        if name in self._inflight:
            return ActionResult(ok=True, detail=f"容器 {name} 已有重启动作在执行，跳过（幂等）", elapsed_ms=ms())
        self._inflight.add(name)
        try:
            if await self._precheck(spec) is None:
                return ActionResult(ok=True, detail=f"容器 {name} 正在重启中，跳过重复动作", elapsed_ms=ms())
            if self.config.is_protected(name):
                return await self._self_restart(name, spec)
            try:
                await self.docker.restart(name, timeout=wait)
            except DockerUnavailable as exc:
                return ActionResult(ok=False, detail=f"重启失败（Docker 不可达）：{exc}", elapsed_ms=ms())
            except DockerError as exc:
                return ActionResult(ok=False, detail=f"重启失败：HTTP {exc.status} {exc.message[:160]}", elapsed_ms=ms())
            await self.state.set_kv(f"action.last_restart.{name}", time.time())
            ok, info = await self._verify_running(name)
            detail = (f"已重启 {name}，复核 running（{info.get('status')}）" if ok
                      else f"已发出重启，复核未确认 running：{info}")
            return ActionResult(ok=ok, detail=detail, elapsed_ms=ms(), output=json.dumps(info, ensure_ascii=False))
        finally:
            self._inflight.discard(name)
    async def _self_restart(self, name: str, spec: ActionSpec) -> ActionResult:
        """受保护容器（本体）自重启：三策略分支，见 §9。"""
        t0, strategy = time.perf_counter(), self.config.self_restart_strategy
        if not self.config.allow_self_restart or strategy == "disabled":
            return ActionResult(ok=False, detail="本体自重启被禁用（allow_self_restart=false 或 strategy=disabled），转人工",
                                elapsed_ms=int((time.perf_counter() - t0) * 1000))
        if strategy == "watchdog":
            req = self.config.restart_request_file
            payload = {"ts": time.time(), "container": name, "reason": spec.params.get("reason", "guardian"),
                       "incident": spec.params.get("incident", "")}
            with open(f"{req}.tmp", "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(f"{req}.tmp", req)          # 原子写，避免看门狗读到半截文件
            return ActionResult(ok=True, detail=f"已投递看门狗重启请求：{req}（宿主机 systemd 执行 compose restart）",
                                elapsed_ms=int((time.perf_counter() - t0) * 1000),
                                output=json.dumps(payload, ensure_ascii=False))
        try:                                        # docker_api：本进程会随容器被杀，连接中断视为成功
            await self.docker.restart(name, timeout=10)
        except DockerUnavailable:
            pass
        except DockerError as exc:
            return ActionResult(ok=False, detail=f"API 自重启失败：{exc.message[:160]}",
                                elapsed_ms=int((time.perf_counter() - t0) * 1000))
        return ActionResult(ok=True, detail="已通过 Docker API 发起本体重启（不可复核，启动后由 startup 巡检闭环）",
                            elapsed_ms=int((time.perf_counter() - t0) * 1000))
class StartContainerAction(_BaseContainerAction):
    kind = "start_container"
    async def execute(self, spec: ActionSpec) -> ActionResult:
        t0, name = time.perf_counter(), spec.target
        ms = lambda: int((time.perf_counter() - t0) * 1000)
        if spec.dry_run:
            return ActionResult(ok=True, detail=f"[dry-run] 将启动容器 {name}", elapsed_ms=ms())
        try:
            if bool(((await self.docker.inspect(name)).get("State") or {}).get("Running")):
                return ActionResult(ok=True, detail=f"容器 {name} 已在运行（幂等跳过）", elapsed_ms=ms())
            await self.docker.start(name)
        except DockerError as exc:
            return ActionResult(ok=False, detail=f"启动失败：HTTP {exc.status} {exc.message[:160]}", elapsed_ms=ms())
        except DockerUnavailable as exc:
            return ActionResult(ok=False, detail=f"启动失败（Docker 不可达）：{exc}", elapsed_ms=ms())
        ok, info = await self._verify_running(name)
        return ActionResult(ok=ok, detail=f"已启动 {name}，复核={'通过' if ok else '未通过'}",
                            elapsed_ms=ms(), output=json.dumps(info, ensure_ascii=False))
```

## 8. 动作幂等与「重启中」窗口

| 机制 | 实现 | 目的 |
| --- | --- | --- |
| 进程内互斥 | `_inflight` 集合 | 同容器并发动作去重 |
| 状态预检 | `State.Status == "restarting"` → 跳过 | 重启中不叠加动作 |
| 时间窗口 | `action.last_restart.{target}` + `RESTARTING_WINDOW_SEC` | 重启后短暂异常不重复触发 |
| 上层冷却 / 熔断 | `safety.action_cooldown_sec` / `max_actions_per_hour`（B12） | 跨探针限流、超限升级 FATAL |
| 幂等结果 | `304 Not Modified` / 已 running | 视为成功，不计失败 |

```
t0     probe 判定 CRIT(未运行) -> SafetyGuard 授权 -> Action.execute
t0     _precheck: Status != restarting -> POST /containers/napcat/restart?t=10
t0+1s  写 action.last_restart.napcat；轮询 inspect 直到 Running=true
t0+3s  probe 再次运行：Status=restarting -> WARN，suggested=None（不动作）
t0+9s  probe: Running -> OK，事件闭环
```

## 9. 受保护容器与自重启三策略

`astrbot` 本体默认 ∈ `safety.protected_containers`，须 `safety.allow_self_restart=true` 才可动作。

| 策略 | 配置值 | 执行者 | 在途请求 | 可复核性 | 风险 | 适用 |
| --- | --- | --- | --- | --- | --- | --- |
| watchdog（默认） | `watchdog` | 宿主机 systemd 单元 | 优雅停止 | 重启后由 `startup` 巡检闭环 | 低 | 生产默认 |
| docker_api | `docker_api` | 插件自身 | 中断（连接被杀） | 不可复核，靠启动自检 | 中（自杀式易风暴） | 无 systemd 环境 |
| disabled | `disabled` | 无 | — | — | 无 | 禁止自重启，仅告警 |

```
插件   写 data/restart_request.json（临时文件 + os.replace 原子）
       └─ Incident.result.detail = "已投递看门狗重启请求"
宿主   guardian-watchdog.timer 每 30s 检查请求文件
       └─ docker compose restart astrbot && rm -f restart_request.json
重启后 startup 巡检读取未闭环 incident -> 本体正常则关闭事件并推送"自愈成功"
```
约束：`watchdog` 策略下**不得**同时走 `docker_api` 分支；同 5 分钟内的重复请求合并为一次（防抖）。

## 10. 重启后复核

| 阶段 | 动作 | 失败处理 |
| --- | --- | --- |
| 发出 | `POST .../restart` | `ActionResult.ok=false`，计入 B12 失败即停计数 |
| 等待 | 轮询 `inspect`，上限 `VERIFY_TIMEOUT_SEC=60s` | 超时 → `ok=false` + 升级告警 |
| 验证 | `State.Running==true` 且 `State.Status=="running"` | 未满足 → 记录末次状态到 `output` |
| 闭环 | 写审计 + 关闭 incident + 推送 | 复核失败 → 连续失败 +1；`RestartCount` 仍增长则 `loop=True` |

## 11. 阈值与判定规则表

| 条件 | status | severity | suggested_action | 说明 |
| --- | --- | --- | --- | --- |
| socket 不存在/不可写 | UNKNOWN | WARN | None | 环境问题，人工介入 |
| `/_ping` 失败 | UNKNOWN | WARN | None | 全模式降级 |
| 容器 404 | CRIT | CRIT | start_container | 可能被删除 |
| `Status=restarting` / `paused` | WARN | WARN | None | 窗口抑制 / 人工确认 |
| 非 running（exited/dead/created） | CRIT | CRIT | start_container | 2×间隔内拉起 |
| `OOMKilled=true` | CRIT | CRIT | None（转 B06） | 内存取证 |
| `Health.Status=unhealthy` | CRIT | CRIT | restart_container | 服务不可用 |
| `Health.Status=starting` | WARN | WARN | None | 启动中 |
| 窗口内新增重启 ≥ 3 / ≥ 1 | CRIT / WARN | CRIT / WARN | None | CrashLoop 熔断 / 观察 |
| 数据盘 ≥ 95% / ≥ 85% | CRIT / WARN | CRIT / WARN | None | 人工清理 |
| 其余 | OK | INFO | None | 正常 |

**对 `core/policy.py::DEFAULT_RULES` 的贡献（B03 §5 节选之外需补充的规则）**

| 探针 | metric | op | threshold | status | severity | action_kind |
| --- | --- | --- | --- | --- | --- | --- |
| `docker_daemon` | `reachable` | `==` | 0 | UNKNOWN | WARN | — |
| `container_health` | `running` | `==` | 0 | CRIT | CRIT | `start_container` |
| `container_health` | `restart_delta` | `>=` | 3 | CRIT | CRIT | — |
| `container_health` | `oom_killed` | `==` | 1 | CRIT | CRIT | — |
| `container_health` | `health_unhealthy` | `==` | 1 | CRIT | CRIT | `restart_container` |
| `self_health` | `socket_writable` | `==` | 0 | WARN | WARN | — |
| `self_health` | `disk_pct` | `>=` | 85 | WARN | WARN | — |
| `self_health` | `disk_pct` | `>=` | 95 | CRIT | CRIT | — |

> `Rule.test` 以 `float(metrics[metric])` 比较，布尔指标按 `0/1` 参与（`running=false` → 0）。探针只产出指标，**建议动作由 PolicyEngine 按本表推导**（B03 §5）；`metrics["suggested"]` 仅作日志辅助，不作为授权依据。
> B03 §10 探针清单把 `docker_daemon` 不可达简写为 CRIT；本文细化为 `UNKNOWN + WARN`（不可观测时不动作，符合 B00 §10 安全红线）。

## 12. 失败模式与降级

| 失败模式 | 检测 | 行为 | 用户可见 |
| --- | --- | --- | --- |
| socket 未挂载 / 权限不足 | `os.path.exists` 假 / `EACCES` | 探针 UNKNOWN；动作层直接失败 | 提示 compose 挂载或 `group_add` |
| daemon 卡死 | `/_ping` 超时 3s | UNKNOWN + 告警，**不动作** | 「Docker 无响应」 |
| 容器名不存在 | HTTP 404 | CRIT + start_container（若在白名单） | 提示核对 `containers.*` |
| 动作被拒 | SafetyGuard deny | 记 suppressed incident + 审计 | 通知含拒绝原因 |
| 复核失败 / CLI 亦失败 | 60s 内未 running / 子进程非 0 | `ok=false`，累计失败 | 升级告警含 stderr 摘要 |

降级链：`socket API` → （若开启）`CLI fallback` → `UNKNOWN + 告警`。**绝不在不可观测时执行动作。**

## 13. 验收标准（对应 C04）

- [ ] C04-01 启动后 `docker_daemon` 30s 内产出 `OK`，`metrics.api_version` 非空。
- [ ] C04-02 `docker compose stop napcat` 后 ≤ 2×`check_interval` 产生 CRIT 并成功 `start_container`。
- [ ] C04-03 `docker kill napcat`（无优雅退出）同样能在 2×间隔内恢复 `running`。
- [ ] C04-04 容器改名后返回 CRIT + `suggested=start_container`，无异常栈。
- [ ] C04-05 人为快速重启 3 次触发 `restart_delta>=3` → CRIT 且 `suggested=None`（不动作）。
- [ ] C04-06 `dry_run=true` 时动作 `detail` 以 `[dry-run]` 开头且容器状态零变化。
- [ ] C04-07 非 `managed ∪ allowed_containers` 的重启被 SafetyGuard 拒绝并写审计。
- [ ] C04-08 `allow_self_restart=false` 时对 `astrbot` 的重启被拒并升级告警。
- [ ] C04-09 `self_restart_strategy=watchdog` 时生成 `restart_request.json`，插件未直接调用 Docker API。
- [ ] C04-10 socket 不可用且 `use_cli_fallback=false` 时探针 UNKNOWN、零动作、单条告警。
- [ ] C04-11 重启后 60s 内复核 `running`，事件闭环且 `output` 含末次 State。
- [ ] C04-12 连续 3 次动作失败后进入 `maintenance` 档位并仅告警（B00 §10.6）。
