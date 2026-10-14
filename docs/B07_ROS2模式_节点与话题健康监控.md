# B07 · ROS2 模式：节点与话题健康监控

> 上位规格：B00《项目总览与需求规格》v0.1.0。实现 B00 §4（`mode=ros2` 探针集）、§7（`ros2.*` 契约）、§10（红线 1/8）、§13（Gate `G-ROS2`）。关联 B03 / B08 / B12 / C07。

## 1. 目标与范围

| ID | 目标 | 判据 |
| --- | --- | --- |
| R-G1 | 期望节点缺失可发现 | `ros2 node list` 比对 `ros2.expected_nodes`，缺失 → `Status.CRIT` |
| R-G2 | 期望话题存在且存活 | `ros2 topic list` 判存在；`ros2 topic hz --window 5` 判频率 |
| R-G3 | 无发布者话题可发现 | `ros2 topic info` 的 publisher count = 0 → `CRIT` |
| R-G4 | 可执行白名单恢复 | 仅执行 `ros2.node_restart_commands` 中命令，执行后复核 |
| R-G5 | 域级问题可诊断 | `ros2 doctor --report` 摘要与异常行提取 |
| R-G6 | 与容器守护共存 | `mode=ros2` 时仍可启用 `docker_daemon` |

交付物：探针 `ros2_nodes` / `ros2_topics` / `ros2_doctor`，动作 `ros2_restart`，底座 `utils/ros2_runner.py`。范围外（B00 §1.3）：Service/Action 业务编排、参数服务器、TF 校验、多机集群、任意 shell。

## 2. 与服务器模式的差异

| 维度 | `server` 模式 | `ros2` 模式 |
| --- | --- | --- |
| 守护对象 | 容器 `astrbot` / `napcat` | 节点 `/camera_node`、话题 `/scan` |
| 状态来源 | Docker Engine HTTP API（结构化 JSON） | `ros2` CLI 文本输出（需解析） |
| 健康判据 | running / restart_count / 内存 MB | 存在性 / 频率 Hz / publisher 数 |
| 恢复手段 | `docker restart <name>` | 配置映射命令（systemctl / docker / lifecycle） |
| 目标集合 | 容器白名单显式枚举 | `expected_nodes` / `expected_topics` 显式枚举 |
| 失败模式 | 容器退出、OOM、QQ 掉线 | 节点崩溃、话题静默、DDS 域不通 |
| 主要风险 | 重启风暴 | 命令注入（B00 R9）、域隔离误判 |

1. 解析必须防御性：CLI 输出随发行版变化，解析失败一律 `UNKNOWN` + `WARN`，绝不判 `OK`。
2. 先枚举后比对：ROS2 图是开放集合，无 `expected_*` 则「缺失」无从定义，也给了 LLM 幻觉空间。
3. 频率判定限定在 `expected_topics` 内（避免事件型话题误报），固定 `--window 5` 抑制抖动。
4. 恢复动作天然高风险（`ros2 lifecycle set` 近似任意执行），必须收口到配置白名单。

```
Scheduler.tick()  (间隔由 profile 决定: normal 60s / night 180s / aggressive 20s)
  └─ HealthEngine.run_once(trigger="interval")
       ├─ Ros2NodesProbe.check()  → [ProbeResult(node, OK|CRIT)] × N
       ├─ Ros2TopicsProbe.check() → [ProbeResult(topic, OK|WARN|CRIT)] × M
       ├─ Ros2DoctorProbe.check() → [ProbeResult(domain, OK|WARN)]
       └─ (可选) DockerDaemonProbe.check()
  └─ PolicyEngine.evaluate() → Finding(ActionSpec(kind="ros2_restart", target="/camera_node"))
  └─ SafetyGuard.authorize() ├─ deny  → Incident(suppressed=True) → 通知
                             └─ allow → Ros2RestartNodeAction.execute() → ActionResult
  └─ 复核（8s 后重跑 ros2_nodes）→ Incident / Audit / Notifier
```

## 3. 执行后端设计

| `ros2.exec_backend` | 语义 | 适用 | 前置条件 |
| --- | --- | --- | --- |
| `local` | astrbot 容器内 `asyncio.create_subprocess_exec` | 镜像内已装 ROS2 并 source 好 | 容器内 `ros2` 可执行、DDS 变量正确 |
| `docker_exec` | Docker Engine HTTP API 在 `ros2.docker_container` 内 exec（推荐） | astrbot 不装 ROS2，隔离更干净 | socket 可用、目标容器运行中 |

```
ros2.exec_backend ?
  ├─ "local"       → create_subprocess_exec(argv, env=build_env())
  └─ "docker_exec" → ros2.docker_container 为空 ? → rc=127（配置错误，不重试）
                     POST /containers/{c}/exec (AttachStdout/Stderr=true, Cmd=argv, Env=[...])
                     POST /exec/{id}/start     (Detach=false, Tty=false)
                     GET  /exec/{id}/json      → ExitCode + 合并输出（Demux=false）
                   → 失败且 docker.use_cli_fallback=true → 降级 docker CLI（B00 D2）
```

跨容器守护最常见的误判不是节点真挂了，而是探针与目标不在同一 DDS 域：

| 变量 | 建议值 | 说明 |
| --- | --- | --- |
| `ROS_DOMAIN_ID` | 与业务侧一致（默认 `0`） | 域号不同则完全互不可见，误报全部节点缺失 |
| `ROS_LOCALHOST_ONLY` | **必须 `0`**（或未设置） | 为 `1` 时只走回环，跨容器发现直接失效 |
| `RMW_IMPLEMENTATION` | 与业务侧一致（如 `rmw_fastrtps_cpp`） | RMW 不同且无桥接同样互不可见 |
| `ROS_DISTRO` | 与业务侧一致（如 `humble`） | 影响 CLI 子命令行为（`hz --window`） |
| `AMENT_PREFIX_PATH` | 由 `source /opt/ros/<distro>/setup.bash` 提供 | 缺失则 `ros2` 不可用（rc=127） |

本模块**不新增配置键**：变量从插件进程环境（compose `environment:`）继承，`docker_exec` 再透传给目标容器。若需按探针覆盖，须先按 B00 治理规则在 §7 增补 `ros2.env` 子键再改代码。

## 4. `utils/ros2_runner.py`（完整代码）

```python
# utils/ros2_runner.py
"""ROS2 命令白名单化执行器：唯一可发起子进程 / docker exec 的入口（B00 §10-1/8）。
只接受「具名动词 + 具名参数」；恢复命令必须逐字命中 ros2.node_restart_commands。"""
from __future__ import annotations
import asyncio, os, re, shlex, time
from dataclasses import dataclass
from ..core.models import ActionResult

DEFAULT_TIMEOUT, HZ_TIMEOUT, DOCTOR_TIMEOUT = 10.0, 15.0, 20.0   # 代码级硬超时，不设配置键
VERBS = {"node_list": ["ros2", "node", "list"], "topic_list": ["ros2", "topic", "list"],
         "topic_hz": ["ros2", "topic", "hz", "--window", "5"],
         "topic_info": ["ros2", "topic", "info"], "doctor": ["ros2", "doctor", "--report"]}
_NAME_RE = re.compile(r"^/[A-Za-z0-9_~/]+$")
_ENV_KEYS = ("ROS_DOMAIN_ID", "ROS_LOCALHOST_ONLY", "RMW_IMPLEMENTATION", "ROS_DISTRO")
_HZ_RE = re.compile(r"average rate:\s*([0-9]+(?:\.[0-9]+)?)")
_PUB_RE = re.compile(r"Publisher count:\s*(\d+)", re.IGNORECASE)
_SUB_RE = re.compile(r"Subscription count:\s*(\d+)", re.IGNORECASE)


class Ros2CommandError(ValueError):
    """命令未通过白名单校验（调用方/配置错误，不重试）。"""


@dataclass
class RunOutput:
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    elapsed_ms: int
    backend: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    def to_action_result(self, detail: str = "") -> ActionResult:
        out = (self.stdout or "").strip()
        if len(out) > 2000:                       # 原始输出截断（同 ProbeResult.raw 策略）
            out = out[:2000] + "\n...[truncated]"
        if not detail:
            tail = (self.stderr or "").strip().splitlines()
            detail = "ok" if self.ok else ("timeout" if self.timed_out
                                           else "rc=%d %s" % (self.rc, tail[-1] if tail else ""))
        return ActionResult(ok=self.ok, detail=detail, elapsed_ms=self.elapsed_ms, output=out)


class Ros2Runner:
    """按 ros2.exec_backend 选择后端执行 ROS2 命令。"""

    def __init__(self, cfg, docker, logger) -> None:
        self.cfg, self.docker, self.log = cfg, docker, logger
        self.backend = str(cfg.get("ros2.exec_backend", "local")).strip().lower()
        if self.backend not in ("local", "docker_exec"):
            self.log.warning("ros2.exec_backend=%r 非法，回退 local", self.backend)
            self.backend = "local"
        self.container = str(cfg.get("ros2.docker_container", "") or "").strip()
        self._restart_argv = set()      # 白名单：配置值 -> shlex 切分后的 argv 元组
        for node, cmd in (cfg.get("ros2.node_restart_commands", {}) or {}).items():
            try:
                argv = tuple(shlex.split(str(cmd)))
            except ValueError:
                self.log.error("node_restart_commands[%s] 无法解析，已忽略", node)
                continue
            if argv:
                self._restart_argv.add(argv)

    def build_env(self) -> dict[str, str]:
        """继承进程环境 + 补齐 DDS 默认值（跨容器发现必须 ROS_LOCALHOST_ONLY=0）。"""
        env = {k: str(v) for k, v in os.environ.items()}
        env.setdefault("ROS_DOMAIN_ID", "0")
        env.setdefault("RMW_IMPLEMENTATION", "rmw_fastrtps_cpp")
        if env.get("ROS_LOCALHOST_ONLY", "0") not in ("0", ""):
            self.log.warning("ROS_LOCALHOST_ONLY=%s 将阻断跨容器发现", env["ROS_LOCALHOST_ONLY"])
        return env

    def assert_allowed_restart(self, argv: list[str]) -> None:
        if tuple(argv) not in self._restart_argv:
            raise Ros2CommandError("命令不在 ros2.node_restart_commands 白名单内: %s" % " ".join(argv))

    @staticmethod
    def _name(name: str) -> str:
        name = str(name or "").strip()
        if not name or not _NAME_RE.match(name):
            raise Ros2CommandError("话题/节点名称非法: %r" % name)
        return name

    async def run_verb(self, verb: str, args: list[str] | None = None,
                       timeout: float | None = None) -> RunOutput:
        if verb not in VERBS:
            raise Ros2CommandError("未知动词: %r" % verb)
        argv = list(VERBS[verb]) + [self._name(a) for a in (args or [])]
        to = timeout if timeout is not None else (HZ_TIMEOUT if verb == "topic_hz"
              else DOCTOR_TIMEOUT if verb == "doctor" else DEFAULT_TIMEOUT)
        return await self._run(argv, to)

    async def run_whitelisted(self, argv: list[str], timeout: float = DEFAULT_TIMEOUT) -> RunOutput:
        """执行配置白名单内的恢复命令（唯一允许的非动词入口）。"""
        argv = [str(x) for x in argv]
        self.assert_allowed_restart(argv)
        return await self._run(argv, timeout)

    async def _run(self, argv: list[str], timeout: float) -> RunOutput:
        if self.backend == "docker_exec":
            return await self._docker_exec(argv, timeout)
        return await self._local(argv, timeout)

    async def _docker_exec(self, argv: list[str], timeout: float) -> RunOutput:
        t0 = time.monotonic()
        el = lambda: int((time.monotonic() - t0) * 1000)               # noqa: E731
        if not self.container:
            return RunOutput(argv, 127, "", "ros2.docker_container 未配置", el(), "docker_exec")
        env = self.build_env()
        try:
            rc, merged = await self.docker.exec_capture(
                self.container, argv, timeout=timeout,
                env=["%s=%s" % (k, env[k]) for k in _ENV_KEYS if k in env])
            return RunOutput(argv, int(rc), merged, "", el(), "docker_exec")
        except asyncio.TimeoutError:
            return RunOutput(argv, 124, "", "docker exec timeout %.1fs" % timeout, el(),
                             "docker_exec", True)
        except Exception as e:                                         # noqa: BLE001 统一降级
            self.log.warning("docker_exec 失败: %s", e)
            if self.cfg.get("docker.use_cli_fallback", False):
                return await self._local(["docker", "exec", self.container, *argv], timeout)
            return RunOutput(argv, 125, "", "docker_exec error: %s" % e, el(), "docker_exec")

    async def _local(self, argv: list[str], timeout: float) -> RunOutput:
        t0, proc = time.monotonic(), None
        el = lambda: int((time.monotonic() - t0) * 1000)               # noqa: E731
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self.build_env())
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return RunOutput(argv, proc.returncode or 0, _dec(out_b), _dec(err_b), el(), "local")
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            return RunOutput(argv, 124, "", "timeout %.1fs" % timeout, el(), "local", True)
        except FileNotFoundError as e:
            return RunOutput(argv, 127, "", "ros2 不可用: %s" % e, el(), "local")


# ---------- 输出解析（纯函数，便于单测 C07） ----------
def _dec(raw: bytes) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


def parse_node_list(text: str) -> list[str]:
    """`ros2 node list`：每行一个 /name，忽略空行与警告行。"""
    return sorted({s for s in (x.strip() for x in (text or "").splitlines())
                   if s.startswith("/") and _NAME_RE.match(s)})


def parse_topic_list(text: str) -> list[str]:
    """`ros2 topic list`；兼容 `name [type]` 形式。"""
    return sorted({s for s in (x.strip().split()[0] if x.strip() else ""
                               for x in (text or "").splitlines())
                   if s.startswith("/") and _NAME_RE.match(s)})


def parse_topic_hz(text: str) -> float | None:
    """解析 `ros2 topic hz --window 5` 的 average rate；无输出返回 None。"""
    m = _HZ_RE.search(text or "")
    return float(m.group(1)) if m else None


def parse_topic_info(text: str) -> tuple[int | None, int | None]:
    """解析 `ros2 topic info` 的 publisher / subscription 数量。"""
    p, s = _PUB_RE.search(text or ""), _SUB_RE.search(text or "")
    return (int(p.group(1)) if p else None, int(s.group(1)) if s else None)
```

## 5. `probes/ros2_nodes.py`（完整代码）

```python
# probes/ros2_nodes.py
"""节点存活探针：`ros2 node list` 与 ros2.expected_nodes 比对。缺失 -> CRIT。"""
from __future__ import annotations
import time
from ..core.models import ProbeResult, Severity, Status
from ..utils.ros2_runner import Ros2CommandError, parse_node_list
from .base import Probe

PROBE_NAME = "ros2_nodes"
SEV = {Status.OK: Severity.INFO, Status.WARN: Severity.WARN,
       Status.CRIT: Severity.CRIT, Status.UNKNOWN: Severity.WARN}


def mk(target, ts, status, message, metrics=None, raw=None) -> ProbeResult:
    """统一 severity 映射，避免每个分支重复写枚举。"""
    return ProbeResult(PROBE_NAME, target, status, SEV[status], message, metrics or {}, ts, raw)


class Ros2NodesProbe(Probe):
    name = PROBE_NAME

    def __init__(self, runner, cfg, logger) -> None:
        self.runner, self.cfg, self.log = runner, cfg, logger
        self.expected = [str(x).strip() for x in (cfg.get("ros2.expected_nodes", []) or [])
                         if str(x).strip()]

    async def check(self) -> list[ProbeResult]:
        ts = time.time()
        try:
            out = await self.runner.run_verb("node_list")
        except Ros2CommandError as e:
            return [mk("(runner)", ts, Status.UNKNOWN, "节点状态未知：%s" % e)]
        if not out.ok:      # CLI 不可用 / 超时 / 域不通：降级 UNKNOWN，绝不判 OK
            return [mk("(runner)", ts, Status.UNKNOWN,
                       "节点状态未知：%s" % out.to_action_result().detail)]
        nodes = parse_node_list(out.stdout)
        if not nodes and self.expected:
            # 空列表 + 有期望节点：几乎必然是 DDS 域/环境问题，而非全部节点同时死亡
            return [mk("(domain)", ts, Status.UNKNOWN,
                       "node list 为空但期望 %d 个节点，疑为 ROS_DOMAIN_ID/RMW 不一致"
                       % len(self.expected), {"nodes": [], "hint": "check_ros_domain"})]

        res = [mk(n, ts, Status.OK if n in nodes else Status.CRIT,
                  "节点在线" if n in nodes else "节点缺失（期望存在但未发现）",
                  {"present": n in nodes, "graph_total": len(nodes)},
                  raw=None if n in nodes else {"node_list": nodes})
               for n in self.expected]
        miss = [n for n in self.expected if n not in nodes]
        res.append(mk("(summary)", ts, Status.CRIT if miss else Status.OK,
                      ("缺失 %d/%d 个期望节点: %s" % (len(miss), len(self.expected), ", ".join(miss))
                       if miss else "全部 %d 个期望节点在线" % len(self.expected)),
                      {"expected": len(self.expected), "present": len(self.expected) - len(miss),
                       "missing": miss, "graph_total": len(nodes),
                       "unexpected": sorted(set(nodes) - set(self.expected))}))
        return res
```

## 6. `probes/ros2_topics.py`（完整代码）

判定规则（只用 B00 §7 已有键，不引入新阈值键）：

| 情形 | Status | Severity |
| --- | --- | --- |
| 期望话题未出现在 `topic list` / publisher count = 0 / `hz` 无输出（`topic_hz_min > 0`） | `CRIT` | `CRIT` |
| `topic info` 解析不出 count / `0 < hz < topic_hz_min` | `WARN` | `WARN` |
| `hz >= topic_hz_min` | `OK` | `INFO` |

```python
# probes/ros2_topics.py
"""话题探针：存在性 + 发布者数量 + 频率。"""
from __future__ import annotations
import time
from ..core.models import ProbeResult, Severity, Status
from ..utils.ros2_runner import (Ros2CommandError, parse_topic_hz, parse_topic_info,
                                 parse_topic_list)
from .base import Probe

PROBE_NAME = "ros2_topics"
SEV = {Status.OK: Severity.INFO, Status.WARN: Severity.WARN,
       Status.CRIT: Severity.CRIT, Status.UNKNOWN: Severity.WARN}


def mk(target, ts, status, message, metrics=None, raw=None) -> ProbeResult:
    return ProbeResult(PROBE_NAME, target, status, SEV[status], message, metrics or {}, ts, raw)


class Ros2TopicsProbe(Probe):
    name = PROBE_NAME

    def __init__(self, runner, cfg, logger) -> None:
        self.runner, self.cfg, self.log = runner, cfg, logger
        self.expected = [str(x).strip() for x in (cfg.get("ros2.expected_topics", []) or [])
                         if str(x).strip()]
        try:
            self.hz_min = float(cfg.get("ros2.topic_hz_min", 0.0) or 0.0)
        except (TypeError, ValueError):
            self.hz_min = 0.0

    async def check(self) -> list[ProbeResult]:
        ts = time.time()
        try:
            out = await self.runner.run_verb("topic_list")
        except Ros2CommandError as e:
            return [mk("(runner)", ts, Status.UNKNOWN, "话题状态未知：%s" % e)]
        if not out.ok:
            return [mk("(runner)", ts, Status.UNKNOWN,
                       "话题状态未知：%s" % out.to_action_result().detail)]
        topics = parse_topic_list(out.stdout)
        results: list[ProbeResult] = []
        for topic in self.expected:
            if topic not in topics:
                results.append(mk(topic, ts, Status.CRIT, "期望话题不存在",
                                  {"exists": False, "graph_total": len(topics)},
                                  raw={"topic_list": topics}))
                continue
            results.extend(await self._inspect(topic, ts))
        return results

    async def _inspect(self, topic: str, ts: float) -> list[ProbeResult]:
        try:
            info = await self.runner.run_verb("topic_info", [topic])
            pubs, subs = parse_topic_info(info.stdout)
        except Ros2CommandError as e:
            return [mk(topic, ts, Status.UNKNOWN, "话题状态未知：%s" % e)]

        if not info.ok:
            head = mk(topic, ts, Status.UNKNOWN, "话题状态未知：%s" % info.to_action_result().detail)
        elif pubs is None:
            head = mk(topic, ts, Status.WARN, "无法解析 publisher 数量（输出格式变化或类型未知）",
                      {"publishers": None, "subscriptions": subs})
        elif pubs == 0:
            head = mk(topic, ts, Status.CRIT, "话题无发布者（publisher count = 0），业务链路已断",
                      {"publishers": 0, "subscriptions": subs})
        else:
            head = mk(topic, ts, Status.OK, "发布者 %d 个" % pubs,
                      {"publishers": pubs, "subscriptions": subs})
        if self.hz_min <= 0:      # 仅在配置了最小频率时判定，避免事件型话题误报
            return [head]
        return [head, await self._hz(topic, ts)]

    async def _hz(self, topic: str, ts: float) -> ProbeResult:
        try:
            hz_out = await self.runner.run_verb("topic_hz", [topic])
        except Ros2CommandError as e:
            return mk(topic, ts, Status.UNKNOWN, "话题状态未知：%s" % e)
        rate = parse_topic_hz(hz_out.stdout) if hz_out.ok else None
        if rate is None:
            return mk(topic, ts, Status.CRIT, "话题静默：--window 5 内未采到任何消息",
                      {"hz": None, "hz_min": self.hz_min},
                      raw={"stderr": (hz_out.stderr or "")[:500]})
        if rate < self.hz_min:
            return mk(topic, ts, Status.WARN, "话题频率偏低: %.3f Hz < %.3f Hz"
                      % (rate, self.hz_min), {"hz": round(rate, 4), "hz_min": self.hz_min})
        return mk(topic, ts, Status.OK, "频率正常: %.3f Hz" % rate,
                  {"hz": round(rate, 4), "hz_min": self.hz_min})
```

## 7. `probes/ros2_doctor.py`（完整代码）

```python
# probes/ros2_doctor.py
"""域级诊断探针：执行 `ros2 doctor --report`，提取摘要与异常行。"""
from __future__ import annotations
import re
import time
from ..core.models import ProbeResult, Severity, Status
from ..utils.ros2_runner import Ros2CommandError
from .base import Probe

PROBE_NAME = "ros2_doctor"
_BAD_RE = re.compile(r"\b(error|failed|failure|not\s+found|unable|cannot|mismatch)\b", re.IGNORECASE)
_WARN_RE = re.compile(r"\b(warn|warning|deprecat|not\s+published)\b", re.IGNORECASE)
_SECTION_RE = re.compile(r"^[A-Z][A-Z0-9 _\-]{4,}$")


def extract_issues(report: str, limit: int = 8) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)，均为去重后的行文本。"""
    errors, warns = [], []
    for line in (report or "").splitlines():
        s = line.strip()
        if not s or _SECTION_RE.match(s):
            continue
        if _BAD_RE.search(s):
            errors.append(s)
        elif _WARN_RE.search(s):
            warns.append(s)
    uniq = lambda seq: list(dict.fromkeys(seq))[:limit]                # noqa: E731
    return uniq(errors), uniq(warns)


class Ros2DoctorProbe(Probe):
    name = PROBE_NAME

    def __init__(self, runner, cfg, logger) -> None:
        self.runner, self.cfg, self.log = runner, cfg, logger

    def _res(self, ts: float, status: Status, message: str, metrics=None,
             raw=None) -> ProbeResult:
        # 域级异常多为环境问题而非节点已死：Status 可 CRIT 但 Severity 固定 WARN，
        # 以免夜间档位被噪音唤醒；PolicyEngine 只对节点/话题 CRIT 出 ros2_restart
        return ProbeResult(PROBE_NAME, "(domain)", status,
                           Severity.INFO if status is Status.OK else Severity.WARN,
                           message, metrics or {}, ts, raw)

    async def check(self) -> list[ProbeResult]:
        ts = time.time()
        try:
            out = await self.runner.run_verb("doctor")
        except Ros2CommandError as e:
            return [self._res(ts, Status.UNKNOWN, "doctor 无法执行：%s" % e)]
        if not out.ok:
            return [self._res(ts, Status.UNKNOWN,
                              "ros2 doctor 执行失败：%s" % out.to_action_result().detail,
                              {"rc": out.rc, "timed_out": out.timed_out},
                              raw={"stderr": (out.stderr or "")[:500]})]
        errors, warns = extract_issues(out.stdout)
        metrics = {"errors": errors, "warnings": warns, "error_count": len(errors),
                   "warning_count": len(warns), "report_lines": len(out.stdout.splitlines())}
        raw = {"report_head": out.stdout[:1500]}
        if errors:
            return [self._res(ts, Status.CRIT, "域诊断发现 %d 条异常：%s" % (len(errors), errors[0]),
                              metrics, raw=raw)]
        if warns:
            return [self._res(ts, Status.WARN, "域诊断 %d 条告警：%s" % (len(warns), warns[0]),
                              metrics, raw=raw)]
        return [self._res(ts, Status.OK, "域诊断无异常（%d 行报告）" % metrics["report_lines"],
                          metrics)]
```

## 8. `actions/ros2_actions.py`（完整代码）

`ros2.node_restart_commands` 为 `节点名 -> 命令字符串` 映射，三种形态：

```jsonc
{ "ros2": { "node_restart_commands": {
    "/camera_node": "systemctl --user restart ros2-camera.service",
    "/lidar_node":  "docker restart ros2-lidar",
    "/nav_node":    "ros2 lifecycle set /nav_node deactivate" } } }
```

| 形态 | 适用 | 优点 | 注意事项 |
| --- | --- | --- | --- |
| `systemctl [--user] restart <unit>` | 节点由 systemd 托管 | 有退出码、可查 journal | 容器内通常无 systemd，多用于 `local` 后端 |
| `docker restart <container>` | 一容器一节点 | 语义清晰、幂等 | 需 socket 权限；该容器全部节点一起断 |
| `ros2 lifecycle set <node> deactivate` | 生命周期节点 | 最轻量、不中断进程 | 仅执行配置中的单条命令；两步操作请指向已审计包装脚本，绝不在代码里拼接 |

```python
# actions/ros2_actions.py
"""ROS2 恢复动作。安全约束（B00 §10）：命令只能来自 ros2.node_restart_commands 白名单
（由 Ros2Runner.assert_allowed_restart 二次校验）；目标节点必须在 ros2.expected_nodes 内；
冷却/熔断/dry-run 由 SafetyGuard 前置裁决，本层透传不复实现。"""
from __future__ import annotations
import asyncio, shlex, time
from ..core.models import ActionResult, ActionSpec
from ..utils.ros2_runner import Ros2CommandError, parse_node_list
from .base import Action

KIND = "ros2_restart"
VERIFY_DELAY_SEC = 8.0          # 重启后等待节点重新注册到图
VERIFY_RETRIES, VERIFY_RETRY_GAP = 3, 4.0


class Ros2RestartNodeAction(Action):
    kind = KIND

    def __init__(self, runner, cfg, logger) -> None:
        self.runner, self.cfg, self.log = runner, cfg, logger
        self.expected = {str(x).strip() for x in (cfg.get("ros2.expected_nodes", []) or [])}
        self.commands = {str(k): str(v)
                         for k, v in (cfg.get("ros2.node_restart_commands", {}) or {}).items()}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _fail(t0: float, detail: str) -> ActionResult:
        return ActionResult(ok=False, detail=detail, elapsed_ms=int((time.monotonic() - t0) * 1000))

    async def execute(self, spec: ActionSpec) -> ActionResult:
        node, t0 = str(spec.target or "").strip(), time.monotonic()
        # 1) 目标校验：必须是期望节点，且配置了恢复命令
        if node not in self.expected:
            return self._fail(t0, "节点 %s 不在 ros2.expected_nodes 中，拒绝执行" % node)
        cmd = self.commands.get(node)
        if not cmd:
            return self._fail(t0, "节点 %s 未配置 ros2.node_restart_commands，拒绝执行" % node)
        # 2) 切分 + 白名单校验（双重防线）
        try:
            argv = shlex.split(cmd)
            self.runner.assert_allowed_restart(argv)
        except ValueError as e:
            return self._fail(t0, "恢复命令无法解析: %s" % e)
        except Ros2CommandError as e:
            return self._fail(t0, "白名单校验失败: %s" % e)
        # 3) dry-run：只回执不执行（B00 §10-5）
        if spec.dry_run or self.cfg.get("dry_run", True):
            self.log.info("[dry-run] 将执行: %s (node=%s)", " ".join(argv), node)
            return ActionResult(ok=True, detail="dry-run: 未执行，计划命令 %s" % " ".join(argv),
                                elapsed_ms=int((time.monotonic() - t0) * 1000),
                                output=" ".join(argv))
        # 4) 执行
        self.log.info("执行节点恢复: node=%s cmd=%s", node, " ".join(argv))
        out = await self.runner.run_whitelisted(argv,
                                                timeout=float(spec.params.get("timeout", 15.0)))
        if not out.ok:
            return out.to_action_result("恢复命令失败: node=%s" % node)
        # 5) 复核
        recovered, detail = await self.verify(node)
        elapsed = int((time.monotonic() - t0) * 1000)
        return ActionResult(ok=recovered, elapsed_ms=elapsed, output=out.stdout[:1000],
                            detail=("节点已恢复: %s" % node) if recovered
                            else "命令成功但复核未通过: %s (%s)" % (node, detail))

    async def verify(self, node: str) -> tuple[bool, str]:
        """重启后复核：等待节点重新出现在 `ros2 node list`。带并发锁避免重复复核。"""
        lock = self._locks.setdefault(node, asyncio.Lock())
        async with lock:
            await asyncio.sleep(VERIFY_DELAY_SEC)
            last = "no-output"
            for attempt in range(1, VERIFY_RETRIES + 1):
                out = await self.runner.run_verb("node_list")
                if out.ok:
                    nodes = parse_node_list(out.stdout)
                    if node in nodes:
                        return True, "attempt=%d" % attempt
                    last = "节点仍未出现（图内 %d 个节点）" % len(nodes)
                else:
                    last = out.to_action_result().detail
                if attempt < VERIFY_RETRIES:
                    await asyncio.sleep(VERIFY_RETRY_GAP)
            return False, last
```

## 9. 节点恢复后的复核与冷却

复核与冷却必须成对，否则形成「重启—未恢复—再重启」风暴（B00 R1）。

```
Finding(CRIT, target=/camera_node)
  └─ SafetyGuard.authorize(kind="ros2_restart", target="/camera_node")
       ├─ 冷却窗口内已有同目标动作?           → deny(suppress_reason="cooldown")
       ├─ 窗口内动作数 > max_actions_per_hour → deny("circuit_open") + FATAL 告警
       ├─ dry_run=true                        → allow(dry_run=True)
       └─ allow
  └─ Ros2RestartNodeAction.execute()
       ├─ 执行成功 → verify(): sleep 8s → node_list ×3（间隔 4s）
       │    ├─ 出现   → ActionResult(ok=True)  → Incident(recovered) → 通知「已恢复」
       │    └─ 未出现 → ActionResult(ok=False) → Incident(CRIT) → 升级 FATAL + 进入冷却
       └─ 执行失败 → 连续失败计数 +1；连续 3 次 → 只告警 + 切 maintenance（B00 §10-6）
  └─ 冷却：SafetyGuard 记录 (target, ts)，窗口 safety.action_cooldown_sec（默认 600s）
```

| 参数 | 来源 | 默认 | 作用 |
| --- | --- | --- | --- |
| 动作冷却 | `safety.action_cooldown_sec` | 600 | 同节点动作最小间隔 |
| 熔断窗口 | `safety.circuit_breaker_window_sec` | 1800 | 统计窗口 |
| 每小时上限 | `safety.max_actions_per_hour` | 6 | 超限熔断并升级 FATAL |
| 复核延迟 | 常量 `VERIFY_DELAY_SEC` | 8.0 | 等待节点重新注册 |
| 复核重试 | 常量 `VERIFY_RETRIES` / `VERIFY_RETRY_GAP` | 3 / 4.0s | 容忍 DDS 发现延迟 |

## 10. ROS2 与容器模式的共存

`mode=ros2` 只决定探针集，不排斥容器探针（B00 §4 已列 `docker_daemon` 为可选）。

| 组合 | 探针集 | 说明 |
| --- | --- | --- |
| 纯 ROS2 | `ros2_nodes` `ros2_topics` `ros2_doctor` | 节点由 systemd / 裸进程托管 |
| ROS2 + 容器 | 上述 + `docker_daemon` | 节点在容器内；先报容器，避免对已死容器刷节点告警 |
| ROS2 + 容器 + 内存 | 上述 + `container_memory` | 需同时配置 `containers.managed` 白名单 |

1. `docker_daemon` 不可用（socket 权限不足，B00 R2）时 ROS2 探针仍须独立工作，不因 Docker 探针异常整体跳过。
2. 节点所在容器为 `exited` 时，`ros2_nodes` 缺失告警降级为 `WARN` 并标注 `reason=container_down`，由容器侧恢复，避免两套动作互相打架。
3. `ros2_restart` 与 `restart_container` 冷却计数独立，但共享 `max_actions_per_hour` 熔断总量。

## 11. ROS2 域发现的前置条件

| # | 前置条件 | 检查方法 | 不满足的表现 |
| --- | --- | --- | --- |
| P1 | `ROS_DOMAIN_ID` 与业务侧一致 | 两侧 `env \| grep ROS_DOMAIN_ID` | 完全互不可见，`node list` 为空 |
| P2 | `ROS_LOCALHOST_ONLY=0` | 同上 | 仅回环发现，跨容器不可见 |
| P3 | `RMW_IMPLEMENTATION` 一致 | 同上 | 域号相同也互不可见 |
| P4 | 容器网络可达（推荐 `network_mode: host`） | `docker inspect` | 组播/单播被隔离，发现失败 |
| P5 | `/dev/shm` ≥ 64MB | `df -h /dev/shm` | 大消息传输失败、`hz` 抖动 |
| P6 | 容器内 ROS 环境已 source | `docker exec <c> bash -lc 'ros2 --version'` | 命令 rc=127，探针降级 `UNKNOWN` |
| P7 | `ros2` CLI 与节点版本一致 | 两侧 `ros2 --version` | 子命令参数不兼容（如 `hz --window`） |
| P8 | `docker_exec` 目标容器在运行 | `docker ps` | exec 报 404/409，动作失败 |

```yaml
services:
  astrbot:
    environment: [ROS_DOMAIN_ID=0, ROS_LOCALHOST_ONLY=0, RMW_IMPLEMENTATION=rmw_fastrtps_cpp]
    network_mode: host      # 节点容器用 host 网络时，astrbot 也需 host 才能直接发现
    ipc: host               # 共享 /dev/shm，配合 P5
    volumes: ["/var/run/docker.sock:/var/run/docker.sock"]
```

首次部署：保持 `dry_run=true`，用 `profile=aggressive`（20s）观察 3 个周期，确认 `graph_total` 稳定后再配置 `node_restart_commands` 并关闭 dry-run。

## 12. 常见故障与对策

| 现象 | 可能原因 | 判定信号 | 对策 |
| --- | --- | --- | --- |
| 全部期望节点同时缺失 | 域号/RMW/网络不一致 | `node_list` 为空 + `doctor` 报网络异常 | 按 §11 P1–P4 核对；探针已降级 `UNKNOWN` 避免误重启 |
| `ros2` rc=127 | 未 source ROS 环境 | `stderr` 含 `command not found` | 改用 `exec_backend=docker_exec`；或镜像加 `/etc/profile.d/ros.sh` |
| `hz` 一直无输出 | 事件型话题 / 发布者慢启动 | `parse_topic_hz` 返回 `None` | 从 `expected_topics` 移除，或 `topic_hz_min` 归零 |
| `hz` 抖动误报 | 采样窗口太短 | 相邻周期差异 > 50% | 已固定 `--window 5`；仍抖动则 `topic_hz_min` 设为期望值 60% |
| publisher=0 但节点在线 | 发布者崩溃 / 未初始化完 | `Publisher count: 0` | 配置该节点恢复命令；复核已含 8s+12s 等待 |
| `doctor` 大量 warning | QoS 不匹配 / 未发布话题 | `warning_count > 0` | 属 `WARN`，不触发动作，由 B08 按 `min_severity` 决定推送 |
| 命令成功但复核失败 | 注册慢 / 启动即崩 | `verify()` 三次仍未出现 | 查节点日志；冷却期内不再动作，升级 FATAL |
| docker exec 权限不足 | socket 未挂载 / group 不匹配 | `Permission denied` | 按 B13 配置 `group_add`；或切回 `local` |
| 节点反复抖动 | 节点自身不稳定 | OK/CRIT 相邻周期交替 | 夜间档位抑制噪音；对抖动节点提高冷却 |
| 话题名解析异常 | CLI 输出含 ANSI 颜色 | `parse_topic_list` 结果异常 | 解析已过滤非 `/` 行；可设 `NO_COLOR=1` |

## 13. 验收标准（对应 C07）

| # | 验收项 | 方法 | 判据 | 关联 |
| --- | --- | --- | --- | --- |
| A1 | 期望节点全在线 | 正常域跑一次 `run_once` | 每节点 `OK/INFO`，汇总 `missing=[]` | R-G1 |
| A2 | 缺失节点判定 | 杀掉一个期望节点 | 该节点 `CRIT/CRIT`，汇总含节点名 | R-G1 |
| A3 | 空图降级 | 改 `ROS_DOMAIN_ID` 为不一致值 | `UNKNOWN/WARN` + `hint=check_ros_domain`，**不**产生 `ros2_restart` Finding | §11 P1 |
| A4 | 话题存在性 | 移除 `expected_topics` 中一项 | `CRIT/CRIT`，`metrics.exists=False` | R-G2 |
| A5 | 频率告警 | `ros2 topic pub -r 1` 造低频 | `0<hz<min` → `WARN/WARN`；无消息 → `CRIT/CRIT` | R-G2 |
| A6 | 发布者数量 | 停发布者保留话题 | `metrics.publishers=0` → `CRIT/CRIT` | R-G3 |
| A7 | 白名单拒绝 | 构造非配置命令的 `ActionSpec` | 抛 `Ros2CommandError` 或 `ok=False`，审计含拒绝原因 | B00 §10-1 |
| A8 | 非期望节点拒绝 | target 不在 `expected_nodes` | `ok=False`，detail 含「不在 ros2.expected_nodes」 | B00 §10-2 |
| A9 | dry-run 零副作用 | `dry_run=true` 执行动作 | detail 含 `dry-run`，节点未被重启 | B00 §10-5 |
| A10 | 恢复复核 | 配置可恢复命令并触发 | 复核 `ok=True` 且 Incident 标记恢复；未恢复时 `ok=False` + 冷却生效 | §9 |
| A11 | 冷却抑制 | 冷却期内再触发同目标 | `Incident.suppressed=True`，`suppress_reason="cooldown"` | §9 |
| A12 | 熔断 | 1h 内累计超 `max_actions_per_hour` | 触发 `circuit_open` + FATAL，动作全部被拒 | §9 |
| A13 | doctor 摘要 | 构造带 warning 的域 | `warning_count>0` → `WARN/WARN`，不触发动作 | R-G5 |
| A14 | 双后端等价 | 同命令分别用 `local` / `docker_exec` | 解析结果一致（允许输出顺序差异） | §3 |
| A15 | 超时保护 | 白名单内注入 sleep 命令 | `timed_out=True`、rc=124，事件循环未阻塞 | B00 D9 |
| A16 | 与容器探针共存 | `mode=ros2` + `docker_daemon` | 两者结果出现在同一快照，互不影响 | §10 |

**Gate `G-ROS2`（B00 §13）**：期望节点缺失可告警；配置了恢复命令时可执行并复核。A1–A3、A7–A10 为必过项。
