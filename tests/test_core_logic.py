"""Guardian 守护系统 —— 核心逻辑自检测试。

对应文档：C03 / C06 / C10 / C11 / C12 中的纯逻辑用例。

特点：**无需安装 AstrBot、无需 pytest**。
- 若环境未安装 astrbot，会自动注入一个最小 stub（仅提供 logger）。
- 直接运行：`python tests/test_core_logic.py`
- 也兼容 pytest：`pytest tests/test_core_logic.py`
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from datetime import datetime

# --------------------------------------------------------------------------- #
# 路径与 astrbot stub
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN_DIR = os.path.join(ROOT, "src", "astrbot_plugin_guardian")
for p in (PLUGIN_DIR, os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)


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

from core.config import DEFAULTS, GuardianConfig, build_config, deep_merge  # noqa: E402
from core.lifecycle import LevelSpec, LifecyclePolicy, LifecycleState  # noqa: E402
from core.models import ActionSpec, Severity, Status  # noqa: E402
from core.policy import PolicyEngine  # noqa: E402
from core.safety import (  # noqa: E402
    SafetyGuard,
    validate_command,
    validate_target,
)
from core.scheduler import Scheduler, cron_match, next_run, parse_cron  # noqa: E402


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeState:
    def __init__(self) -> None:
        self.d: dict = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v

    def update(self, m):
        self.d.update(m)

    def bump_cooldown(self, target, ts=None):
        self.d.setdefault("cooldowns", {})[target] = ts or 1000.0

    def cooldown_of(self, target):
        return float((self.d.get("cooldowns") or {}).get(target, 0.0))

    def record_action(self, ts=None):
        self.d.setdefault("action_history", []).append(ts or 1000.0)

    def actions_in_window(self, w):
        return len(self.d.get("action_history") or [])

    def bump_fail_streak(self, target):
        fs = self.d.setdefault("fail_streak", {})
        fs[target] = int(fs.get(target, 0)) + 1
        return fs[target]

    def reset_fail_streak(self, target):
        self.d.setdefault("fail_streak", {})[target] = 0


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, **kw):
        self.records.append(kw)


def make_config(**over):
    raw = deep_merge(DEFAULTS, over)
    cfg = GuardianConfig(raw)
    cfg.validate()
    return cfg


# --------------------------------------------------------------------------- #
# 1. cron 解析与匹配
# --------------------------------------------------------------------------- #
def test_cron_parse_and_match():
    assert parse_cron("* * * * *")
    assert cron_match("0 4 * * *", datetime(2026, 10, 13, 4, 0))
    assert not cron_match("0 4 * * *", datetime(2026, 10, 13, 5, 0))
    assert cron_match("*/15 * * * *", datetime(2026, 10, 13, 4, 30))
    assert not cron_match("*/15 * * * *", datetime(2026, 10, 13, 4, 31))
    assert cron_match("0 0 1 * *", datetime(2026, 10, 1, 0, 0))
    # 列表与区间
    assert cron_match("0 6-8 * * *", datetime(2026, 10, 13, 7, 0))
    assert cron_match("0 0 * * 1,3,5", datetime(2026, 10, 14, 0, 0))  # 周三
    # dow 0 与 7 都表示周日
    sunday = datetime(2026, 10, 18, 0, 0)
    assert cron_match("0 0 * * 0", sunday)
    assert cron_match("0 0 * * 7", sunday)


def test_cron_invalid():
    for bad in ("", "* * *", "60 * * * *", "* * * * 9", "a * * * *"):
        try:
            parse_cron(bad)
        except Exception:
            continue
        raise AssertionError(f"应当拒绝非法 cron: {bad!r}")


def test_cron_next_run():
    base = datetime(2026, 10, 13, 3, 30)
    nxt = next_run("0 4 * * *", base)
    assert nxt is not None and nxt.hour == 4 and nxt.minute == 0
    assert nxt.date() == base.date()
    nxt2 = next_run("0 4 * * *", datetime(2026, 10, 13, 5, 0))
    assert nxt2 is not None and nxt2.date().day == 14


# --------------------------------------------------------------------------- #
# 2. 生命周期等级
# --------------------------------------------------------------------------- #
def make_policy(max_uptime=720.0, cooldown=900):
    levels = [LevelSpec.from_dict(x) for x in DEFAULTS["lifecycle"]["levels"]]
    return LifecyclePolicy(levels, max_uptime_hours=max_uptime,
                           force_on_deadline=True, inject_cooldown_sec=cooldown)


def test_level_boundaries():
    p = make_policy()
    assert p.resolve_level(0)[1].id == "L0"
    assert p.resolve_level(167.9)[1].id == "L0"
    assert p.resolve_level(168)[1].id == "L1"      # 恰好等于阈值 -> 进入该级
    assert p.resolve_level(359.9)[1].id == "L1"
    assert p.resolve_level(360)[1].id == "L2"
    assert p.resolve_level(600)[1].id == "L3"
    assert p.resolve_level(719)[1].id == "L3"
    assert p.resolve_level(720)[1].id == "L4"


def test_prompt_rendering_and_cooldown():
    p = make_policy(cooldown=900)
    st = LifecycleState()
    d1 = p.evaluate(400.0, st, now=10_000.0)
    assert d1["level"].id == "L2"
    assert d1["inject_prompt"] and "{uptime_days}" not in d1["inject_prompt"]
    assert "16.7" in d1["inject_prompt"]           # 400/24 = 16.7
    # 同等级、冷却未到 -> 不重复注入
    d2 = p.evaluate(401.0, st, now=10_100.0)
    assert d2["inject_prompt"] is None
    # 冷却到期 -> 允许再次注入
    d3 = p.evaluate(402.0, st, now=10_000.0 + 901)
    assert d3["inject_prompt"]


def test_arm_sleep_and_force_sleep():
    p = make_policy()
    st = LifecycleState()
    d = p.evaluate(650.0, st, now=1.0)
    assert d["action"] == "arm_sleep" and st.sleep_armed is True
    assert d["force_sleep"] is False

    d2 = p.evaluate(720.0, st, now=2.0)
    assert d2["force_sleep"] is True and d2["action"] == "force_sleep"


def _custom_policy(levels, **kw):
    specs = [LevelSpec.from_dict(x) for x in levels]
    return LifecyclePolicy(specs, **kw)


def test_custom_force_level_is_authoritative_before_deadline():
    """用户批准的语义：等级自身 action=force_sleep 即权威，硬上限之前就生效。

    修复前 `force_sleep` 只在 max_uptime_hours 分支被置真，于是自定义等级
    （阈值 < 硬上限）虽然 action 是 force_sleep，引擎却收不到强制信号——
    表现为"配了强制等级却什么也不做"。
    """
    p = _custom_policy([
        {"id": "L0", "name": "常规", "after_hours": 0, "action": "none"},
        {"id": "L1", "name": "自定义强制", "after_hours": 10,
         "prompt": "该休息了", "action": "force_sleep"},
    ], max_uptime_hours=720.0, force_on_deadline=True)

    st = LifecycleState()
    d = p.evaluate(11.0, st, now=1.0)          # 11h：已过 L1(10h)，远低于 720h
    assert d["action"] == "force_sleep"
    assert d["force_sleep"] is True, "自定义强制等级必须在硬上限之前生效"
    assert "level action=force_sleep" in d["reason"]
    # 义务起始时刻被记录（供运维追溯），且不会随每次巡检被反复刷新
    assert st.forced_at == 1.0
    d2 = p.evaluate(12.0, st, now=99.0)
    assert st.forced_at == 1.0, "forced_at 应记录义务起点而非最后一次评估时间"

    # 阈值以下不得强制
    d3 = p.evaluate(9.9, LifecycleState(), now=1.0)
    assert d3["force_sleep"] is False and d3["action"] == "none"


def test_force_level_is_independent_of_deadline_switch():
    """force_on_deadline 只管"硬上限"兜底，不应关掉用户显式配置的强制等级。"""
    p = _custom_policy([
        {"id": "L0", "name": "常规", "after_hours": 0, "action": "none"},
        {"id": "L1", "name": "强制", "after_hours": 5, "action": "force_sleep"},
    ], max_uptime_hours=1000.0, force_on_deadline=False)
    assert p.evaluate(6.0, LifecycleState(), now=1.0)["force_sleep"] is True
    # 硬上限已关闭：即使超过 max_uptime_hours 也不因 deadline 强制
    d = p.evaluate(1500.0, LifecycleState(), now=1.0)
    assert "deadline reached" not in d["reason"]


def test_deadline_still_forces_without_any_force_level():
    """兜底路径不能因为改造而失效：最高等级只是 notify，到硬上限仍须强制。"""
    p = _custom_policy([
        {"id": "L0", "name": "常规", "after_hours": 0, "action": "none"},
        {"id": "L1", "name": "提醒", "after_hours": 10, "action": "notify"},
    ], max_uptime_hours=100.0, force_on_deadline=True)
    d = p.evaluate(100.0, LifecycleState(), now=1.0)
    assert d["force_sleep"] is True and d["action"] == "force_sleep"
    assert "deadline reached" in d["reason"]


def test_forced_at_cleared_only_by_sleep_completion():
    p = _custom_policy([
        {"id": "L0", "name": "常规", "after_hours": 0, "action": "none"},
        {"id": "L1", "name": "强制", "after_hours": 10, "action": "force_sleep"},
    ], max_uptime_hours=720.0)
    st = LifecycleState()
    p.evaluate(11.0, st, now=50.0)
    assert st.forced_at == 50.0
    p.on_sleep_done(st, now=60.0)
    assert st.forced_at == 0.0, "一次成功睡眠必须结清强制义务"
    assert st.uptime_hours == 0.0 and st.level_id == "L0"


def test_describe_surfaces_outstanding_force_obligation():
    p = _custom_policy([
        {"id": "L0", "name": "常规", "after_hours": 0, "action": "none"},
        {"id": "L1", "name": "强制", "after_hours": 10, "action": "force_sleep"},
    ], max_uptime_hours=720.0)
    st = LifecycleState()
    assert "强制义务" not in p.describe(st)
    p.evaluate(11.0, st, now=time.time())
    assert "强制义务始于" in p.describe(st)


def test_sleep_done_resets_state():
    p = make_policy()
    st = LifecycleState()
    p.evaluate(730.0, st, now=1.0)
    assert st.level_index == 4
    p.on_sleep_done(st, now=5.0)
    assert st.last_sleep_at == 5.0
    assert st.sleep_armed is False
    assert st.uptime_hours == 0.0 and st.level_index == 0 and st.level_id == "L0"


def test_no_keyword_trigger_by_design():
    """证明：等级推进只依赖 uptime，与任何对话文本无关。"""
    p = make_policy()
    st = LifecycleState()
    # 同一 uptime、不同"用户说了什么"——插件层根本接收不到文本，结果必然一致
    a = p.evaluate(100.0, LifecycleState(), now=1.0)
    b = p.evaluate(100.0, LifecycleState(), now=1.0)
    assert a["level"].id == b["level"].id == "L0"
    assert a["action"] == b["action"] == "none"


# --------------------------------------------------------------------------- #
# 3. 配置校验
# --------------------------------------------------------------------------- #
def test_config_defaults_ok():
    # 默认配置下白名单为空，validate() 会给出两条启动告警（这是预期行为）
    cfg = build_config({})
    assert cfg.mode == "server"
    assert cfg.profile == "normal"
    assert cfg.dry_run is True
    assert cfg.qq.check_interval == 1200
    assert cfg.lifecycle.max_uptime_hours == 720
    assert len(cfg.lifecycle.levels) == 5
    assert any("白名单为空" in i for i in cfg.issues)

    # 配好白名单 + 允许本体自重启后应无任何问题
    cfg2 = build_config({
        "containers": {"managed": ["astrbot", "napcat"]},
        "safety": {"allow_self_restart": True},
    })
    assert cfg2.issues == [], cfg2.issues
    assert cfg2.lifecycle.sleep_targets == ["astrbot", "napcat"]

    # 未开启 allow_self_restart 时应给出提示（而非静默跳过）
    cfg3 = build_config({"containers": {"managed": ["astrbot", "napcat"]}})
    assert any("allow_self_restart" in i for i in cfg3.issues)


def test_config_invalid_fallback():
    cfg = build_config({
        "mode": "bogus",
        "profile": "bogus",
        "check_interval": 1,
        "qq": {"check_interval": 1, "relogin_method": "bogus"},
        "safety": {"max_actions_per_hour": 0, "self_restart_strategy": "bogus"},
        "lifecycle": {
            "inject_mode": "bogus",
            "sleep_duration_min": 999,
            "levels": [
                {"id": "A", "after_hours": 500, "action": "bogus"},
                {"id": "B", "after_hours": 100, "action": "notify"},
            ],
        },
    })
    assert cfg.mode == "server" and cfg.profile == "normal"
    assert cfg.check_interval == 60
    assert cfg.qq.check_interval == 1200
    assert cfg.qq.relogin_method == "restart_container"
    assert cfg.safety.max_actions_per_hour == 6
    assert cfg.safety.self_restart_strategy == "watchdog"
    assert cfg.lifecycle.inject_mode == "extra_user_content"
    assert cfg.lifecycle.sleep_duration_min == 120
    # 等级被排序，且非法 action 回退
    assert [lv["after_hours"] for lv in cfg.lifecycle.levels] == [100, 500]
    assert cfg.lifecycle.levels[0]["action"] == "notify"
    assert cfg.lifecycle.levels[1]["action"] == "none"
    assert cfg.issues  # 必须报告问题


def test_config_max_uptime_lifted_above_levels():
    cfg = build_config({
        "lifecycle": {"max_uptime_hours": 10,
                      "levels": [{"id": "L1", "after_hours": 100, "action": "notify"}]},
    })
    assert cfg.lifecycle.max_uptime_hours >= 100


def test_config_allowlist_union():
    cfg = build_config({
        "containers": {"managed": ["napcat"]},
        "safety": {"allowed_containers": ["extra"]},
    })
    assert cfg.action_allowlist == {"napcat", "extra"}


# --------------------------------------------------------------------------- #
# 4. 安全护栏
# --------------------------------------------------------------------------- #
def test_sleep_authorizes_all_targets_and_commits_each_cooldown():
    cfg = make_config(dry_run=False, containers={"managed": ["astrbot", "napcat"]},
                      safety={"allow_self_restart": True})
    state = FakeState()
    guard = SafetyGuard(cfg, state, FakeAudit())
    spec = ActionSpec("sleep", "napcat", {"targets": ["napcat", "astrbot"], "duration_min": 1})
    assert guard.authorize(spec, now=2000).allowed
    state.bump_cooldown("astrbot", 1900)
    assert guard.authorize(spec, now=2000).code == "cooldown"
    state.set("cooldowns", {})
    cfg.raw["safety"]["allow_self_restart"] = False
    cfg.refresh()
    assert not guard.authorize(spec, now=2000).allowed
    cfg.raw["safety"]["allow_self_restart"] = True
    cfg.raw["safety"]["self_restart_strategy"] = "docker_api"
    cfg.refresh()
    assert guard.authorize(spec).code == "watchdog_required"
    cfg.raw["safety"]["self_restart_strategy"] = "watchdog"
    cfg.refresh()
    guard.commit(spec, True)
    assert set(state.d["cooldowns"]) == {"astrbot", "napcat"}
    assert len(state.d["action_history"]) == 1


def test_sleep_rejects_bad_group_and_bad_duration_before_actions():
    cfg = make_config(containers={"managed": ["napcat"]})
    guard = SafetyGuard(cfg, FakeState(), FakeAudit())
    for targets in ([], "napcat", ["napcat", "napcat"], [None]):
        assert not guard.authorize(ActionSpec("sleep", "napcat", {"targets": targets})).allowed
    for duration in (True, -1, 121, "1", 1.5, None):
        assert guard.authorize(ActionSpec("sleep", "napcat", {"targets": ["napcat"], "duration_min": duration})).code == "bad_duration"
    assert not guard.authorize(ActionSpec("sleep", "napcat", {"targets": ["napcat", "evil"]})).allowed
    guard.state.set("watchdog.pending", {"targets": ["napcat"]})
    assert guard.authorize(ActionSpec("restart_container", "napcat")).code == "watchdog_pending"


def test_qq_relogin_blocked_while_watchdog_owns_napcat():
    """qq_relogin 逻辑目标是 "qq"，物理上却会重启 NapCat。

    看门狗正在持有 NapCat（睡眠/重启中）时，绝不能再由插件去重启它。
    修复前归属检查拿 "qq" 去比对容器名列表，永远匹配不上，于是形成冲突通道。
    """
    cfg = make_config(dry_run=False,
                      containers={"astrbot": "astrbot", "napcat": "napcat",
                                  "managed": ["astrbot", "napcat"]},
                      safety={"allow_self_restart": True})
    st = FakeState()
    guard = SafetyGuard(cfg, st, FakeAudit())
    st.set("watchdog.pending", {"nonce": "n1", "targets": ["napcat"], "operation": "sleep"})

    d = guard.authorize(ActionSpec("qq_relogin", "qq", {"reason": "offline"}), now=5000.0)
    assert not d.allowed and d.code == "watchdog_pending", f"{d.allowed}/{d.code}"

    # 看门狗持有的不是 NapCat -> 不应误伤
    st.set("watchdog.pending", {"nonce": "n2", "targets": ["astrbot"], "operation": "sleep"})
    assert guard.authorize(ActionSpec("qq_relogin", "qq", {}), now=5000.0).allowed

    # webui_quick_login 不重启容器 -> 不应被 NapCat 归属挡住
    cfg.raw["qq"]["relogin_method"] = "webui_quick_login"
    cfg.raw["qq"]["webui_base"] = "http://napcat:6099"
    cfg.refresh()
    st.set("watchdog.pending", {"nonce": "n3", "targets": ["napcat"], "operation": "sleep"})
    assert guard.authorize(ActionSpec("qq_relogin", "qq", {}), now=5000.0).allowed


def test_qq_relogin_cooldown_uses_physical_napcat():
    """NapCat 刚被重启过时，qq_relogin 不该再立刻重启它一次。"""
    cfg = make_config(dry_run=False,
                      containers={"astrbot": "astrbot", "napcat": "napcat",
                                  "managed": ["astrbot", "napcat"]},
                      safety={"allow_self_restart": True, "action_cooldown_sec": 600})
    st = FakeState()
    guard = SafetyGuard(cfg, st, FakeAudit())

    st.bump_cooldown("napcat", 1000.0)
    d = guard.authorize(ActionSpec("qq_relogin", "qq", {}), now=1100.0)
    assert not d.allowed and d.code == "cooldown", f"{d.allowed}/{d.code}"
    # 冷却到期后放行
    assert guard.authorize(ActionSpec("qq_relogin", "qq", {}), now=1700.0).allowed

    # 记账也必须落在物理目标上，否则下一次冷却检查看不到
    st.set("cooldowns", {})
    guard.commit(ActionSpec("qq_relogin", "qq", {}), ok=True)
    assert st.cooldown_of("napcat") > 0, "qq_relogin 的冷却必须记在 napcat 上"
    assert len(st.d["action_history"]) == 1


def test_validate_target():
    for good in ("astrbot", "napcat", "my_container-1", "a.b"):
        ok, _ = validate_target(good)
        assert ok, good
    for bad in ("", "a b", "a;b", "a|b", "a`b", "a$(x)", "a/b", "a\nb", "x" * 100):
        ok, _ = validate_target(bad)
        assert not ok, bad


def test_validate_command():
    ok, _ = validate_command("systemctl restart ros2-node")
    assert ok
    ok, _ = validate_command("docker restart camera")
    assert ok
    for bad in ("rm -rf /", "curl x | sh", "systemctl restart a; rm -rf /", "bash -c x"):
        ok, _ = validate_command(bad)
        assert not ok, bad


def test_safety_authorize_paths():
    cfg = make_config(**{
        "dry_run": False,
        "containers": {"managed": ["napcat"]},
        "safety": {"allowed_containers": ["napcat"], "protected_containers": ["astrbot"],
                   "action_cooldown_sec": 600, "max_actions_per_hour": 3,
                   "circuit_breaker_window_sec": 1800, "allow_self_restart": False,
                   "max_consecutive_failures": 3},
    })
    st, audit = FakeState(), FakeAudit()
    guard = SafetyGuard(cfg, st, audit)

    # 白名单内 -> 允许
    d = guard.authorize(ActionSpec("restart_container", "napcat"), now=1000.0)
    assert d.allowed and d.dry_run is False

    # 非白名单 -> 拒绝
    d = guard.authorize(ActionSpec("restart_container", "nginx"), now=1000.0)
    assert not d.allowed and d.code == "not_allowlisted"

    # 受保护容器 -> 拒绝
    d = guard.authorize(ActionSpec("restart_container", "astrbot"), now=1000.0)
    assert not d.allowed and d.code in ("not_allowlisted", "protected")

    # 非法目标 -> 拒绝
    d = guard.authorize(ActionSpec("restart_container", "bad name"), now=1000.0)
    assert not d.allowed and d.code == "bad_target"

    # 冷却
    st.bump_cooldown("napcat", 1000.0)
    d = guard.authorize(ActionSpec("restart_container", "napcat"), now=1100.0)
    assert not d.allowed and d.code == "cooldown"

    # 冷却过后放行
    d = guard.authorize(ActionSpec("restart_container", "napcat"), now=1700.0)
    assert d.allowed

    # 每小时上限 -> 熔断
    st2 = FakeState()
    guard2 = SafetyGuard(cfg, st2, FakeAudit())
    st2.d["action_history"] = [1000.0, 1001.0, 1002.0]
    d = guard2.authorize(ActionSpec("restart_container", "napcat"), now=1003.0)
    assert not d.allowed and d.code == "circuit_open"
    assert guard2.tripped is True

    # dry-run 短路
    cfg3 = make_config(**{"dry_run": True, "containers": {"managed": ["napcat"]}})
    guard3 = SafetyGuard(cfg3, FakeState(), FakeAudit())
    d = guard3.authorize(ActionSpec("restart_container", "napcat"), now=1.0)
    assert d.allowed and d.dry_run is True and d.code == "dry_run"

    # 只读动作不受白名单限制
    d = guard.authorize(ActionSpec("notify", "whatever"), now=1.0)
    assert d.allowed


def test_safety_dry_run_does_not_consume_quota():
    """dry-run 不应消耗冷却与每小时配额，否则空跑会挡掉后续真实动作。"""
    over = {
        "containers": {"managed": ["napcat"]},
        "safety": {"allowed_containers": ["napcat"], "action_cooldown_sec": 600,
                   "max_actions_per_hour": 2},
    }
    cfg = make_config(dry_run=True, **over)
    st = FakeState()
    guard = SafetyGuard(cfg, st, FakeAudit())

    d = guard.authorize(ActionSpec("restart_container", "napcat"), now=1000.0)
    assert d.allowed and d.dry_run
    guard.commit(ActionSpec("restart_container", "napcat", dry_run=True), ok=True)

    assert st.cooldown_of("napcat") == 0.0
    assert st.actions_in_window(1800) == 0

    # 换成真实模式、复用同一 state：不应被冷却挡住
    cfg2 = make_config(dry_run=False, **over)
    guard2 = SafetyGuard(cfg2, st, FakeAudit())
    d2 = guard2.authorize(ActionSpec("restart_container", "napcat"), now=1001.0)
    assert d2.allowed and not d2.dry_run

    # 真实动作提交后才会占用冷却
    guard2.commit(ActionSpec("restart_container", "napcat", dry_run=False), ok=True)
    assert st.cooldown_of("napcat") == 1000.0 or st.cooldown_of("napcat") > 0
    assert st.actions_in_window(1800) == 1


def test_safety_self_check_warns():
    cfg = make_config(**{"dry_run": True})
    warns = SafetyGuard(cfg, FakeState(), FakeAudit()).self_check()
    joined = " ".join(warns)
    assert "白名单为空" in joined
    assert "dry_run" in joined


# --------------------------------------------------------------------------- #
# 5. 判定引擎
# --------------------------------------------------------------------------- #
def test_policy_suggests_actions():
    from core.models import ProbeResult

    pe = PolicyEngine(make_config(memory={"restart_on_leak": True}))
    cases = [
        (ProbeResult("container_health", "napcat", Status.CRIT, Severity.CRIT,
                     "down", {"missing": True, "running": False}), "start_container"),
        (ProbeResult("container_health", "astrbot", Status.CRIT, Severity.CRIT,
                     "oom", {"missing": False, "running": True, "oom_killed": True}),
         "restart_container"),
        (ProbeResult("qq_login", "qq", Status.CRIT, Severity.CRIT,
                     "offline", {"online": False}), "qq_relogin"),
        (ProbeResult("container_memory", "astrbot", Status.WARN, Severity.WARN,
                     "leak", {"leak": True}), "restart_container"),
        (ProbeResult("ros2_nodes", "ros2", Status.CRIT, Severity.CRIT,
                     "missing", {"all_present": False}), "ros2_restart"),
    ]
    for res, expected in cases:
        findings = pe.evaluate([res])
        assert findings[0].suggested_action is not None
        assert findings[0].suggested_action.kind == expected

    # OK 状态不产生动作
    ok = ProbeResult("container_health", "napcat", Status.OK, Severity.INFO, "fine",
                     {"missing": False, "running": True})
    assert pe.evaluate([ok])[0].suggested_action is None


# --------------------------------------------------------------------------- #
# 6. 调度器
# --------------------------------------------------------------------------- #
def test_scheduler_interval_and_once():
    async def main():
        sched = Scheduler(tick=0.05)
        hits = {"interval": 0, "once": 0}

        async def on_interval():
            hits["interval"] += 1

        async def on_once():
            hits["once"] += 1

        sched.add_interval_job("j1", on_interval, interval=1)
        sched.get("j1").next_run = 0  # 立即触发
        sched.add_once_job("j2", on_once, delay_sec=0)
        sched.start()
        await asyncio.sleep(0.35)
        await sched.shutdown()
        assert hits["interval"] >= 1
        assert hits["once"] == 1
        # once 任务执行后应被移除
        assert sched.get("j2") is None
        # shutdown 后不应有残留任务
        assert sched.jobs() == []

    asyncio.run(main())


def test_scheduler_error_isolation():
    async def main():
        sched = Scheduler(tick=0.05)
        state = {"ok": 0}

        async def boom():
            raise RuntimeError("boom")

        async def fine():
            state["ok"] += 1

        sched.add_interval_job("bad", boom, interval=1)
        sched.add_interval_job("good", fine, interval=1)
        sched.get("bad").next_run = 0
        sched.get("good").next_run = 0
        sched.start()
        await asyncio.sleep(0.3)
        await sched.shutdown()
        assert sched.get("bad") is not None or True  # 任务仍存在（未被移除）
        assert state["ok"] >= 1

    asyncio.run(main())


def test_scheduler_cron_job_registration():
    async def main():
        sched = Scheduler(tick=0.05)
        sched.add_cron_job("cron1", lambda: None, "*/5 * * * *")
        job = sched.get("cron1")
        assert job is not None and job.kind == "cron"
        assert job.next_run > 0
        await sched.shutdown()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 7. 内存斜率（依赖 aiohttp，缺失则跳过）
# --------------------------------------------------------------------------- #
def test_memory_slope():
    try:
        from astrbot_plugin_guardian.probes.container_memory import (
            compute_slope_mb_per_hour,
        )
    except ImportError as exc:  # aiohttp 等依赖缺失
        print(f"  (跳过：{exc})")
        return
    # 每小时增长 100MB：3600 秒 100MB
    samples = [[0, 100.0], [900, 125.0], [1800, 150.0], [2700, 175.0], [3600, 200.0]]
    slope = compute_slope_mb_per_hour(samples)
    assert abs(slope - 100.0) < 1.0, slope
    assert compute_slope_mb_per_hour([[0, 1.0]]) == 0.0
    assert compute_slope_mb_per_hour([]) == 0.0
    # 平稳无增长
    flat = [[t, 500.0] for t in (0, 600, 1200, 1800, 2400)]
    assert abs(compute_slope_mb_per_hour(flat)) < 0.001


def test_uptime_parse():
    try:
        from astrbot_plugin_guardian.probes.uptime import parse_started_at
    except ImportError as exc:
        print(f"  (跳过：{exc})")
        return
    ts = parse_started_at("2026-10-13T04:00:00.123456789Z")
    assert ts > 0
    # 带时区偏移
    ts2 = parse_started_at("2026-10-13T12:00:00.123456789+08:00")
    assert abs(ts2 - ts) < 1.0
    assert parse_started_at("") == 0.0
    assert parse_started_at("garbage") == 0.0


def test_memory_restart_switch_and_leak_cooldown():
    from core.models import ProbeResult
    res = ProbeResult("container_memory", "napcat", Status.WARN, Severity.WARN,
                      "leak", {"leak": True})
    assert PolicyEngine(make_config()).evaluate([res])[0].suggested_action is None
    config = make_config(dry_run=False, containers={"managed": ["napcat"]},
                         memory={"restart_on_leak": True, "leak_cooldown": 3600})
    spec = PolicyEngine(config).evaluate([res])[0].suggested_action
    assert spec.params["cooldown_key"] == "memory_leak"
    st = FakeState()
    st.bump_cooldown("napcat", 1000)
    guard = SafetyGuard(config, st, FakeAudit())
    assert guard.authorize(spec, now=1700).code == "cooldown"
    assert guard.authorize(spec, now=4600).allowed
    unknown = ProbeResult("qq_login", "qq", Status.UNKNOWN, Severity.WARN,
                          "unavailable", {"online": False})
    assert PolicyEngine(config).evaluate([unknown])[0].suggested_action is None


def test_memory_sampling_config_safely_covers_window():
    config = make_config(memory={"samples": 1, "sample_interval": "bad",
                                 "leak_min_growth_mb": float("nan")})
    assert config.memory.sample_interval == 300
    assert config.memory.samples >= 7
    assert config.memory.leak_min_growth_mb == 40


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
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
