"""Guardian 守护系统 —— 探针端到端自检（使用 fixtures 假数据）。

对应文档：C03 / C04 / C05 / C06 / C07

需要 `aiohttp`（探针模块会 import 它）。无需 AstrBot、无需 Docker。
直接运行：`python tests/test_probes_e2e.py`
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
FIXTURES = os.path.join(HERE, "fixtures")
# 只把 src 加入路径，以便以包名导入（保证插件内部的相对导入可用）
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _install_astrbot_stub() -> None:
    try:
        import astrbot  # noqa: F401
        return
    except ImportError:
        pass
    pkg = types.ModuleType("astrbot")
    pkg.__path__ = []  # type: ignore[attr-defined]
    api = types.ModuleType("astrbot.api")

    class _Logger:
        def _noop(self, *a, **k):
            return None
        debug = info = warning = error = critical = exception = _noop

    api.logger = _Logger()  # type: ignore[attr-defined]
    pkg.api = api  # type: ignore[attr-defined]
    sys.modules["astrbot"] = pkg
    sys.modules["astrbot.api"] = api


_install_astrbot_stub()

try:
    import aiohttp  # noqa: F401
except ImportError as exc:  # pragma: no cover
    print(f"需要 aiohttp 才能运行本测试：{exc}")
    raise SystemExit(0)

from astrbot_plugin_guardian.core.config import DEFAULTS, GuardianConfig, deep_merge  # noqa: E402
from astrbot_plugin_guardian.core.models import Status  # noqa: E402
from astrbot_plugin_guardian.core.registry import all_probe_names  # noqa: E402
from astrbot_plugin_guardian.utils.dockerclient import DockerError, DockerUnavailable  # noqa: E402
from astrbot_plugin_guardian.utils.napcat import NapCatError, NapCatUnavailable  # noqa: E402
import astrbot_plugin_guardian.probes as probes_pkg  # noqa: E402,F401


def fixture(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


class FakeState:
    def __init__(self) -> None:
        self.d: dict = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class FakeDocker:
    """可编程的 Docker 假对象。"""

    def __init__(self, inspect_map=None, stats_map=None, missing=(), unreachable=False):
        self.inspect_map = inspect_map or {}
        self.stats_map = stats_map or {}
        self.missing = set(missing)
        self.unreachable = unreachable
        self.calls: list[tuple] = []
        self.api_version = "v1.44"

    def _guard(self):
        if self.unreachable:
            raise DockerUnavailable("fake daemon down")

    async def negotiate(self):
        self._guard()
        return self.api_version

    async def ping(self):
        return not self.unreachable

    async def info(self):
        self._guard()
        return {"Containers": 2, "ContainersRunning": 2}

    async def inspect(self, name):
        self.calls.append(("inspect", name))
        self._guard()
        if name in self.missing:
            raise DockerError(f"no such container: {name}", 404)
        if name not in self.inspect_map:
            raise DockerError(f"no such container: {name}", 404)
        return self.inspect_map[name]

    async def stats(self, name):
        self.calls.append(("stats", name))
        self._guard()
        return self.stats_map.get(name, {})

    async def restart(self, name, timeout=10):
        self.calls.append(("restart", name))
        self._guard()

    async def start(self, name):
        self.calls.append(("start", name))
        self._guard()

    async def stop(self, name, timeout=10):
        self.calls.append(("stop", name))
        self._guard()


class FakeNapCat:
    def __init__(self, status=None, login=None, status_exc=None, login_exc=None):
        self._status = status or {}
        self._login = login or {}
        self.status_exc = status_exc
        self.login_exc = login_exc

    async def get_status(self):
        if self.status_exc:
            raise self.status_exc
        return self._status

    async def get_login_info(self):
        if self.login_exc:
            raise self.login_exc
        return self._login


class FakeRos2:
    def __init__(self, nodes="", topics="", hz=None, doctor="", restart_ok=True):
        self._nodes = nodes
        self._topics = topics
        self._hz = hz or {}
        self._doctor = doctor
        self.restart_ok = restart_ok
        self.restarted: list[str] = []

    class _Out:
        def __init__(self, rc, text, timed_out=False):
            self.rc = rc
            self.text = text
            self.timed_out = timed_out

        @property
        def ok(self):
            return self.rc == 0 and not self.timed_out

        def lines(self):
            return [ln.strip() for ln in self.text.splitlines() if ln.strip()]

    async def node_list(self):
        return self._Out(0, self._nodes)

    async def topic_list(self):
        return self._Out(0, self._topics)

    async def topic_hz(self, topic, window=5):
        return self._Out(0, self._hz.get(topic, "no new messages"))

    async def doctor_report(self):
        return self._Out(0, self._doctor)

    async def restart_node(self, node):
        self.restarted.append(node)
        return self._Out(0 if self.restart_ok else 1, "ok" if self.restart_ok else "fail")


def cfg(**over) -> GuardianConfig:
    c = GuardianConfig(deep_merge(DEFAULTS, over))
    c.validate()
    return c


def deps(docker=None, napcat=None, ros2=None, state=None) -> dict:
    return {
        "docker": docker or FakeDocker(),
        "napcat": napcat or FakeNapCat(),
        "ros2": ros2,
        "state": state or FakeState(),
    }


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 探针注册
# --------------------------------------------------------------------------- #
def test_probe_registry_complete():
    names = set(all_probe_names())
    for expected in ("docker_daemon", "container_health", "container_memory",
                     "qq_login", "self_health", "uptime",
                     "ros2_nodes", "ros2_topics", "ros2_doctor"):
        assert expected in names, expected


# --------------------------------------------------------------------------- #
# docker_daemon
# --------------------------------------------------------------------------- #
def test_docker_daemon_ok_and_down():
    from astrbot_plugin_guardian.probes.docker_probes import DockerDaemonProbe

    p = DockerDaemonProbe(cfg(), deps(), "docker")
    res = run(p.check())
    assert res.status == Status.OK

    p2 = DockerDaemonProbe(cfg(), deps(docker=FakeDocker(unreachable=True)), "docker")
    res2 = run(p2.check())
    assert res2.status == Status.UNKNOWN          # 降级为 UNKNOWN，不动作
    assert res2.metrics.get("daemon_ok") is False


# --------------------------------------------------------------------------- #
# container_health
# --------------------------------------------------------------------------- #
def test_container_health_running_ok():
    from astrbot_plugin_guardian.probes.docker_probes import ContainerHealthProbe

    d = FakeDocker(inspect_map={"napcat": fixture("docker_inspect_running.json")})
    p = ContainerHealthProbe(cfg(), deps(docker=d), "napcat")
    res = run(p.check())
    assert res.status == Status.OK
    assert res.metrics["running"] is True
    assert res.metrics["oom_killed"] is False


def test_container_health_oom_is_crit():
    from astrbot_plugin_guardian.probes.docker_probes import ContainerHealthProbe

    d = FakeDocker(inspect_map={"astrbot": fixture("docker_inspect_oom.json")})
    p = ContainerHealthProbe(cfg(), deps(docker=d), "astrbot")
    res = run(p.check())
    assert res.status == Status.CRIT
    assert res.metrics["oom_killed"] is True
    assert res.metrics["running"] is False


def test_container_health_missing_is_crit():
    from astrbot_plugin_guardian.probes.docker_probes import ContainerHealthProbe

    d = FakeDocker(missing={"ghost"})
    p = ContainerHealthProbe(cfg(), deps(docker=d), "ghost")
    res = run(p.check())
    assert res.status == Status.CRIT
    assert res.metrics["missing"] is True


def test_container_health_restart_growth_warns():
    from astrbot_plugin_guardian.probes.docker_probes import ContainerHealthProbe

    st = FakeState()
    st.set("restart_counts", {"napcat": 1})
    d = FakeDocker(inspect_map={"napcat": fixture("docker_inspect_running.json")})
    p = ContainerHealthProbe(cfg(), deps(docker=d, state=st), "napcat")
    res = run(p.check())
    # fixture 的 RestartCount = 2 > 上次记录的 1
    assert res.status == Status.WARN
    assert res.metrics["restart_grew"] is True


def test_container_health_daemon_down_is_unknown():
    from astrbot_plugin_guardian.probes.docker_probes import ContainerHealthProbe

    p = ContainerHealthProbe(cfg(), deps(docker=FakeDocker(unreachable=True)), "napcat")
    res = run(p.check())
    assert res.status == Status.UNKNOWN


# --------------------------------------------------------------------------- #
# container_memory
# --------------------------------------------------------------------------- #
def test_memory_probe_thresholds():
    from astrbot_plugin_guardian.probes.container_memory import ContainerMemoryProbe

    # 500MB - 100MB cache = 400MB -> OK
    d = FakeDocker(stats_map={"napcat": fixture("docker_stats.json")},
                   inspect_map={"napcat": fixture("docker_inspect_running.json")})
    p = ContainerMemoryProbe(cfg(), deps(docker=d), "napcat")
    res = run(p.check())
    assert res.status == Status.OK
    assert 390 < res.metrics["mem_mb"] < 410

    # 触发 warn
    c_warn = cfg(memory={"warn_mb": 300, "crit_mb": 1000})
    p2 = ContainerMemoryProbe(c_warn, deps(docker=d), "napcat")
    assert run(p2.check()).status == Status.WARN

    # 触发 crit
    c_crit = cfg(memory={"warn_mb": 100, "crit_mb": 200})
    p3 = ContainerMemoryProbe(c_crit, deps(docker=d), "napcat")
    assert run(p3.check()).status == Status.CRIT


def test_memory_probe_leak_detection():
    from astrbot_plugin_guardian.probes.container_memory import ContainerMemoryProbe
    from unittest.mock import patch

    class GrowingDocker(FakeDocker):
        def __init__(self):
            super().__init__(inspect_map={"napcat": fixture("docker_inspect_running.json")})
            self.n = 0

        async def stats(self, name):
            self.n += 1
            usage = (300 + self.n * 100) * 1024 * 1024  # 每轮 +100MB
            return {"memory_stats": {"usage": usage, "limit": 4 * 1024**3,
                                     "stats": {"cache": 0}}}

    st = FakeState()
    d = GrowingDocker()
    c = cfg(memory={"warn_mb": 100000, "crit_mb": 100000,
                    "growth_warn_mb_per_hour": 10, "samples": 10})
    p = ContainerMemoryProbe(c, deps(docker=d, state=st), "napcat")
    last = None
    for i in range(9):
        with patch("astrbot_plugin_guardian.probes.container_memory.time.time", return_value=10000 + i * 300):
            last = run(p.check())
    assert last.metrics["leak"] is True
    assert last.status == Status.WARN


# --------------------------------------------------------------------------- #
# qq_login
# --------------------------------------------------------------------------- #
def test_qq_online():
    from astrbot_plugin_guardian.probes.qq_login import QqLoginProbe

    nc = FakeNapCat(status=fixture("napcat_status_online.json"),
                    login=fixture("napcat_login_ok.json"))
    p = QqLoginProbe(cfg(), deps(napcat=nc), "qq")
    res = run(p.check())
    assert res.status == Status.OK
    assert res.metrics["user_id"] == "10001"
    assert res.metrics["online"] is True


def test_qq_offline_needs_confirm_rounds():
    from astrbot_plugin_guardian.probes.qq_login import QqLoginProbe

    st = FakeState()
    nc = FakeNapCat(status=fixture("napcat_status_offline.json"),
                    login=fixture("napcat_login_empty.json"))
    c = cfg(qq={"offline_confirm_rounds": 2})
    p = QqLoginProbe(c, deps(napcat=nc, state=st), "qq")

    r1 = run(p.check())
    assert r1.status == Status.WARN          # 第 1 轮：疑似，不动作
    assert r1.metrics["fail_streak"] == 1

    r2 = run(p.check())
    assert r2.status == Status.CRIT          # 第 2 轮：确认掉线
    assert r2.metrics["online"] is False


def test_qq_unreachable_does_not_confirm_immediately():
    """探测通道不可用 != QQ 掉线（debug 修复 F6）。

    历史上这里会得到 WARN + fail_streak=1，累积到阈值后升级为 CRIT 并触发
    qq_relogin 重启动作——即用一个破坏性动作去处理一个"无法确认"的问题。
    现在一律 UNKNOWN，且不计入掉线轮数。
    """
    from astrbot_plugin_guardian.probes.qq_login import QqLoginProbe

    st = FakeState()
    nc = FakeNapCat(status_exc=NapCatUnavailable("conn refused"),
                    login_exc=NapCatUnavailable("conn refused"))
    p = QqLoginProbe(cfg(qq={"offline_confirm_rounds": 3}),
                     deps(napcat=nc, state=st), "qq")
    r = run(p.check())
    assert r.status == Status.UNKNOWN, r.status
    assert r.metrics.get("detection_degraded") is True
    assert r.metrics["fail_streak"] == 0
    assert st.get("qq_fail_streak", 0) == 0


def test_qq_recovery_resets_streak():
    from astrbot_plugin_guardian.probes.qq_login import QqLoginProbe

    st = FakeState()
    st.set("qq_fail_streak", 5)
    nc = FakeNapCat(status=fixture("napcat_status_online.json"),
                    login=fixture("napcat_login_ok.json"))
    p = QqLoginProbe(cfg(), deps(napcat=nc, state=st), "qq")
    res = run(p.check())
    assert res.status == Status.OK
    assert st.get("qq_fail_streak") == 0


# --------------------------------------------------------------------------- #
# uptime
# --------------------------------------------------------------------------- #
def test_uptime_probe():
    from astrbot_plugin_guardian.probes.uptime import UptimeProbe

    d = FakeDocker(inspect_map={
        "astrbot": fixture("docker_inspect_running.json"),
        "napcat": fixture("docker_inspect_running.json"),
    })
    c = cfg(lifecycle={"uptime_targets": ["astrbot", "napcat"]})
    p = UptimeProbe(c, deps(docker=d), "system")
    res = run(p.check())
    assert res.status in (Status.OK, Status.WARN)
    if res.status == Status.OK:
        assert res.metrics["uptime_hours"] > 0


def test_uptime_probe_no_data_is_unknown():
    from astrbot_plugin_guardian.probes.uptime import UptimeProbe

    d = FakeDocker(missing={"astrbot", "napcat"})
    p = UptimeProbe(cfg(), deps(docker=d), "system")
    assert run(p.check()).status == Status.UNKNOWN


# --------------------------------------------------------------------------- #
# ros2
# --------------------------------------------------------------------------- #
def test_ros2_nodes_missing_is_crit():
    from astrbot_plugin_guardian.probes.ros2_nodes import Ros2NodesProbe

    r = FakeRos2(nodes="/camera_node\n/arm_node\n")
    c = cfg(ros2={"expected_nodes": ["/camera_node", "/lidar_node"]})
    p = Ros2NodesProbe(c, deps(ros2=r), "ros2")
    res = run(p.check())
    assert res.status == Status.CRIT
    assert res.metrics["missing"] == ["/lidar_node"]
    assert res.metrics["all_present"] is False


def test_ros2_nodes_all_present():
    from astrbot_plugin_guardian.probes.ros2_nodes import Ros2NodesProbe

    r = FakeRos2(nodes="/camera_node\n/arm_node\n")
    c = cfg(ros2={"expected_nodes": ["/camera_node"]})
    p = Ros2NodesProbe(c, deps(ros2=r), "ros2")
    assert run(p.check()).status == Status.OK


def test_ros2_topics_hz():
    from astrbot_plugin_guardian.probes.ros2_topics import Ros2TopicsProbe

    r = FakeRos2(topics="/cmd_vel\n/odom\n",
                 hz={"/cmd_vel": "average rate: 9.998\n", "/odom": "no new messages"})
    c = cfg(ros2={"expected_topics": ["/cmd_vel", "/odom"], "topic_hz_min": 5.0})
    p = Ros2TopicsProbe(c, deps(ros2=r), "ros2")
    res = run(p.check())
    assert res.status == Status.WARN
    assert res.metrics["dead"] == ["/odom"]
    assert abs(res.metrics["hz"]["/cmd_vel"] - 9.998) < 0.01


def test_ros2_doctor_warnings():
    from astrbot_plugin_guardian.probes.ros2_doctor import Ros2DoctorProbe

    r = FakeRos2(doctor="All OK\nWARNING: something odd\n")
    p = Ros2DoctorProbe(cfg(), deps(ros2=r), "ros2")
    assert run(p.check()).status == Status.WARN


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #
def test_restart_action_dry_run_and_real():
    from astrbot_plugin_guardian.actions.container_actions import RestartContainerAction
    from astrbot_plugin_guardian.core.models import ActionSpec

    d = FakeDocker(inspect_map={"napcat": fixture("docker_inspect_running.json")})
    act = RestartContainerAction(cfg(), deps(docker=d))

    r = run(act.execute(ActionSpec("restart_container", "napcat", dry_run=True)))
    assert r.ok and "[dry-run]" in r.detail
    assert ("restart", "napcat") not in d.calls      # dry-run 不产生副作用

    r2 = run(act.execute(ActionSpec("restart_container", "napcat", dry_run=False)))
    assert r2.ok
    assert ("restart", "napcat") in d.calls
    assert ("inspect", "napcat") in d.calls          # 重启后复核


def test_restart_action_failure_reported():
    from astrbot_plugin_guardian.actions.container_actions import RestartContainerAction
    from astrbot_plugin_guardian.core.models import ActionSpec

    d = FakeDocker(unreachable=True)
    act = RestartContainerAction(cfg(), deps(docker=d))
    r = run(act.execute(ActionSpec("restart_container", "napcat", dry_run=False)))
    assert not r.ok and "失败" in r.detail


def test_ros2_restart_action_whitelist():
    from astrbot_plugin_guardian.actions.ros2_actions import Ros2RestartNodeAction
    from astrbot_plugin_guardian.core.models import ActionSpec

    r = FakeRos2(nodes="/camera_node\n")
    c = cfg(ros2={"node_restart_commands": {"/camera_node": "systemctl restart camera"}})
    act = Ros2RestartNodeAction(c, deps(ros2=r))
    res = run(act.execute(ActionSpec("ros2_restart", "/camera_node",
                                     params={"node": "/camera_node"}, dry_run=False)))
    assert res.ok
    assert r.restarted == ["/camera_node"]

    # 未配置恢复命令 -> 失败
    act2 = Ros2RestartNodeAction(cfg(), deps(ros2=r))
    res2 = run(act2.execute(ActionSpec("ros2_restart", "/unknown",
                                       params={"node": "/unknown"}, dry_run=False)))
    assert not res2.ok


def test_sleep_action_dry_run():
    from astrbot_plugin_guardian.actions.sleep import SleepAction
    from astrbot_plugin_guardian.core.models import ActionSpec

    d = FakeDocker()
    from astrbot_plugin_guardian.core.safety import SafetyGuard
    from unittest.mock import Mock
    c = cfg(containers={"managed": ["astrbot", "napcat"]}, safety={"allow_self_restart": True})
    state = FakeState()
    state.cooldown_of = lambda target: 0
    state.actions_in_window = lambda window: 0
    engine = types.SimpleNamespace(safety=SafetyGuard(c, state, Mock()))
    action_deps = deps(docker=d)
    action_deps["engine"] = engine
    act = SleepAction(c, action_deps)
    res = run(act.execute(ActionSpec("sleep", "astrbot", dry_run=True,
                                     params={"targets": ["astrbot", "napcat"],
                                             "duration_min": 0})))
    assert res.ok and "[dry-run]" in res.detail
    assert d.calls == []


# --------------------------------------------------------------------------- #
# 模块
# --------------------------------------------------------------------------- #
def test_mode_adapters_build_probes():
    from astrbot_plugin_guardian.modes.factory import create_mode

    c = cfg(containers={"managed": ["astrbot", "napcat"]})
    d = deps(docker=FakeDocker(inspect_map={
        "astrbot": fixture("docker_inspect_running.json"),
        "napcat": fixture("docker_inspect_running.json")}))

    server = create_mode("server", c, d)
    names = {p.name for p in server.build_probes()}
    assert {"docker_daemon", "container_health", "container_memory",
            "qq_login", "self_health", "uptime"} <= names

    ros2 = create_mode("ros2", c, d)
    names2 = {p.name for p in ros2.build_probes()}
    assert {"docker_daemon", "ros2_nodes", "ros2_topics", "ros2_doctor",
            "uptime"} <= names2


def test_notifier_dedup_and_severity():
    from astrbot_plugin_guardian.core.audit import AuditLog
    from astrbot_plugin_guardian.core.models import Severity
    from astrbot_plugin_guardian.notify.notifier import Notifier

    sent: list[str] = []

    async def sender(umo, text):
        sent.append(text)

    c = cfg(notify={"umo_list": ["umo://a"], "min_severity": "warn",
                    "rate_limit_sec": 60})
    st = FakeState()
    st.set("profile", "normal")
    audit = AuditLog(os.path.join(ROOT, "tests", "_tmp"))
    n = Notifier(c, st, audit, sender)

    async def main():
        # INFO 低于 min_severity -> 不推
        assert await n.push("info msg", severity=Severity.INFO) is False
        # WARN -> 推
        assert await n.push("warn msg", severity=Severity.WARN) is True
        # 同样内容在限流窗口内 -> 去重
        assert await n.push("warn msg", severity=Severity.WARN) is False
        # force 绕过
        assert await n.push("warn msg", severity=Severity.WARN, force=True) is True

    run(main())
    assert len(sent) == 2


def test_notifier_profile_mute():
    from astrbot_plugin_guardian.core.audit import AuditLog
    from astrbot_plugin_guardian.core.models import Severity
    from astrbot_plugin_guardian.notify.notifier import Notifier

    sent: list[str] = []

    async def sender(umo, text):
        sent.append(text)

    c = cfg(notify={"umo_list": ["umo://a"], "min_severity": "info"})
    st = FakeState()
    audit = AuditLog(os.path.join(ROOT, "tests", "_tmp"))
    n = Notifier(c, st, audit, sender)

    async def main():
        st.set("profile", "maintenance")
        assert await n.push("x", severity=Severity.FATAL) is False   # 维护档全静默
        st.set("profile", "night")
        assert await n.push("y", severity=Severity.INFO) is False    # 夜间静默低级别
        assert await n.push("z", severity=Severity.CRIT) is True

    run(main())


# --------------------------------------------------------------------------- #
def test_memory_cgroup_v1_v2_and_zero_cache():
    from astrbot_plugin_guardian.probes.container_memory import memory_working_set
    mb = 1024 ** 2
    cases = [
        ({"usage": 500 * mb, "stats": {"total_inactive_file": 100 * mb, "cache": 200 * mb}}, 400 * mb),
        ({"usage": 500 * mb, "stats": {"inactive_file": 80 * mb, "anon": 300 * mb}}, 420 * mb),
        ({"usage": 500 * mb, "stats": {"inactive_file": 0, "cache": 200 * mb}}, 500 * mb),
        ({"usage": 500 * mb, "stats": {"rss": 350 * mb}}, 500 * mb),
        ({"usage": 500 * mb, "stats": {"inactive_file": 600 * mb}}, 500 * mb),
    ]
    for mem, expected in cases:
        assert memory_working_set(mem)[0] == expected


def test_memory_short_window_and_duplicate_checks_cannot_confirm():
    from astrbot_plugin_guardian.probes.container_memory import ContainerMemoryProbe
    from unittest.mock import patch
    st = FakeState()
    d = FakeDocker(inspect_map={"napcat": fixture("docker_inspect_running.json")})
    p = ContainerMemoryProbe(cfg(memory={"warn_mb": 100000, "crit_mb": 200000}),
                             deps(docker=d, state=st), "napcat")
    last = None
    for i in range(30):
        d.stats_map["napcat"] = {"memory_stats": {"usage": (100 + i * 10) * 1024 ** 2,
                                                "stats": {"inactive_file": 0}}}
        with patch("astrbot_plugin_guardian.probes.container_memory.time.time", return_value=10000 + i * 20):
            last = run(p.check())
    assert last.metrics["leak"] is False
    assert last.metrics["samples"] == 2
    assert last.metrics["leak_streak"] == 0


def test_memory_new_instance_and_old_metric_history_are_reset():
    from astrbot_plugin_guardian.probes.container_memory import ContainerMemoryProbe
    from unittest.mock import patch
    st = FakeState()
    st.set("samples", {"napcat": [[9000, 10], [9300, 100], [9600, 300]]})
    d = FakeDocker(stats_map={"napcat": fixture("docker_stats.json")},
                   inspect_map={"napcat": fixture("docker_inspect_running.json")})
    p = ContainerMemoryProbe(cfg(), deps(docker=d, state=st), "napcat")
    with patch("astrbot_plugin_guardian.probes.container_memory.time.time", return_value=10000):
        assert run(p.check()).metrics["samples"] == 1
    d.inspect_map["napcat"] = {"State": {"StartedAt": "2026-10-14T08:00:00Z"}}
    with patch("astrbot_plugin_guardian.probes.container_memory.time.time", return_value=10300):
        assert run(p.check()).metrics["samples"] == 1


def test_qq_unknown_breaks_confirmation_and_stale_account_does_not_override_offline():
    from astrbot_plugin_guardian.probes.qq_login import QqLoginProbe
    st = FakeState()
    st.set("qq_fail_streak", 45)
    nc = FakeNapCat(status_exc=NapCatUnavailable("no channel"),
                    login_exc=NapCatUnavailable("no channel"))
    p = QqLoginProbe(cfg(), deps(napcat=nc, state=st), "qq")
    assert run(p.check()).status == Status.UNKNOWN
    assert st.get("qq_fail_streak") == 0
    nc.status_exc = nc.login_exc = None
    nc._status = fixture("napcat_status_offline.json")
    nc._login = fixture("napcat_login_ok.json")  # 缓存账号不能证明在线
    assert run(p.check()).metrics["fail_streak"] == 1
    assert run(p.check()).status == Status.CRIT
    nc._status = nc._login = {}  # 空 JSON 不是明确掉线回答
    assert run(p.check()).status == Status.UNKNOWN
    assert st.get("qq_fail_streak") == 0


def test_napcat_reverse_ws_query_normalizes_api_errors():
    from astrbot_plugin_guardian.utils.napcat import NapCatClient, NapCatError
    calls = []
    async def caller(action):
        calls.append(action)
        return {"online": True} if action == "get_status" else {"user_id": 10001}
    client = NapCatClient("http://unused", action_caller=caller)
    async def main():
        assert (await client.get_status())["online"] is True
        assert (await client.get_login_info())["user_id"] == 10001
        assert client._session is None  # 反向连接不能偷偷发 HTTP
        async def failed(action):
            return {"status": "failed", "retcode": 1400, "data": {}}
        client._action_caller = failed
        try:
            await client.get_status()
        except NapCatError:
            pass
        else:
            raise AssertionError("API 失败未归一化")
    run(main())
    assert calls == ["get_status", "get_login_info"]


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
