"""Guardian 守护系统 —— 对抗性自检（专门用来"把问题试出来"）。

对应文档：C12 / C13 / C15

本文件的定位与既有测试**不同**：既有测试验证"设计好的路径能走通"，本文件专门
构造**边界、非法、自相矛盾、状态泄漏**的输入，去戳破"看起来通过"的假象。

约定：每个断言失败都必须在输出里带上**可复现的最小输入**，否则不算找到 bug。

直接运行：`python tests/test_adversarial.py`
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
# 只把 src 加入路径，以包名导入，保证插件内部相对导入可用
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

from astrbot_plugin_guardian.core.config import DEFAULTS, GuardianConfig, deep_merge  # noqa: E402
from astrbot_plugin_guardian.core.lifecycle import LevelSpec, LifecyclePolicy, LifecycleState  # noqa: E402
from astrbot_plugin_guardian.core.models import ActionSpec, Status  # noqa: E402
from astrbot_plugin_guardian.core.policy import PolicyEngine  # noqa: E402
from astrbot_plugin_guardian.core.safety import SafetyGuard, validate_target  # noqa: E402
from astrbot_plugin_guardian.core.scheduler import cron_match, next_run, parse_cron  # noqa: E402
from astrbot_plugin_guardian.probes.container_memory import (  # noqa: E402
    compute_slope_mb_per_hour,
    memory_working_set,
)
from astrbot_plugin_guardian.probes.uptime import parse_started_at  # noqa: E402


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
        self.d.setdefault("cooldowns", {})[target] = ts if ts is not None else 1000.0

    def cooldown_of(self, target):
        return float((self.d.get("cooldowns") or {}).get(target, 0.0))

    def record_action(self, ts=None):
        self.d.setdefault("action_history", []).append(ts if ts is not None else 1000.0)

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


def make_safety(**over):
    cfg = make_config(**over)
    st = FakeState()
    au = FakeAudit()
    return SafetyGuard(cfg, st, au), cfg, st, au


# =========================================================================== #
# A. 生命周期等级 / 强制睡眠
# =========================================================================== #
def test_force_level_is_authoritative_before_deadline():
    """用户已批准：被选中等级 action=force_sleep 时立即强制，不等硬上限。"""
    pol = LifecyclePolicy(
        levels=[LevelSpec("L0", "常规", 0, "", "none"),
                LevelSpec("L9", "强制", 10, "", "force_sleep")],
        max_uptime_hours=720, force_on_deadline=True)
    st = LifecycleState()
    d = pol.evaluate(11.0, st, now=1000.0)
    assert d["force_sleep"] is True, d
    assert st.forced_at == 1000.0, st.forced_at


def test_force_level_ignores_deadline_switch():
    """force_sleep_on_deadline=false 只关硬上限兜底，不能关掉显式配置的等级。"""
    pol = LifecyclePolicy(
        levels=[LevelSpec("L0", "常规", 0, "", "none"),
                LevelSpec("L9", "强制", 10, "", "force_sleep")],
        max_uptime_hours=720, force_on_deadline=False)
    st = LifecycleState()
    d = pol.evaluate(11.0, st, now=1.0)
    assert d["force_sleep"] is True, d


def test_deadline_fallback_when_no_force_level():
    pol = LifecyclePolicy(
        levels=[LevelSpec("L0", "常规", 0, "", "none")],
        max_uptime_hours=100, force_on_deadline=True)
    st = LifecycleState()
    assert pol.evaluate(99.0, st)["force_sleep"] is False
    assert pol.evaluate(100.0, st)["force_sleep"] is True


def test_deadline_disabled_means_no_force():
    pol = LifecyclePolicy(
        levels=[LevelSpec("L0", "常规", 0, "", "none")],
        max_uptime_hours=100, force_on_deadline=False)
    st = LifecycleState()
    assert pol.evaluate(1000.0, st)["force_sleep"] is False


def test_level_boundary_is_inclusive_at_threshold():
    pol = LifecyclePolicy(levels=[LevelSpec("L0", "a", 0, "", "none"),
                                  LevelSpec("L1", "b", 168, "", "notify")],
                          max_uptime_hours=720)
    assert pol.resolve_level(167.999)[0] == 0
    assert pol.resolve_level(168.0)[0] == 1


def test_level_boundary_negative_uptime():
    """负 uptime（时钟回拨/坏数据）必须落到最低等级，不能 IndexError。"""
    pol = LifecyclePolicy(levels=[LevelSpec("L0", "a", 0, "", "none"),
                                  LevelSpec("L1", "b", 168, "", "notify")],
                          max_uptime_hours=720)
    idx, spec = pol.resolve_level(-5.0)
    assert idx == 0 and spec.id == "L0"


def test_empty_levels_falls_back_to_single_level():
    pol = LifecyclePolicy(levels=[], max_uptime_hours=720)
    assert len(pol.levels) == 1
    assert pol.resolve_level(0)[0] == 0


def test_duplicate_level_thresholds_pick_last():
    """相同阈值时取"最后命中"的一个——必须确定，不能随机。"""
    pol = LifecyclePolicy(levels=[LevelSpec("A", "a", 5, "", "none"),
                                  LevelSpec("B", "b", 5, "", "notify")],
                          max_uptime_hours=720)
    assert pol.resolve_level(5)[1].id == "B"


def test_sleep_armed_is_cleared_by_sleep_done():
    pol = LifecyclePolicy(levels=[LevelSpec("L0", "a", 0, "", "none"),
                                  LevelSpec("L3", "c", 600, "", "arm_sleep")],
                          max_uptime_hours=720)
    st = LifecycleState()
    pol.evaluate(600.0, st)
    assert st.sleep_armed is True
    pol.on_sleep_done(st, now=1234.0)
    assert st.sleep_armed is False
    assert st.forced_at == 0.0
    assert st.uptime_hours == 0.0
    assert st.level_id == "L0"


def test_sleep_armed_is_released_when_uptime_falls():
    """B16 交接文档点名的开放项：uptime 回落（容器被重启）后武装状态必须解除。

    修复前 `evaluate()` 只单向置真（`if action == "arm_sleep": sleep_armed = True`），
    等级已回落仍长期持有"免确认通行证"；现改为每轮按当前等级重算。
    """
    pol = LifecyclePolicy(levels=[LevelSpec("L0", "a", 0, "", "none"),
                                  LevelSpec("L3", "c", 600, "", "arm_sleep")],
                          max_uptime_hours=720)
    st = LifecycleState()
    pol.evaluate(600.0, st)
    assert st.sleep_armed is True
    pol.evaluate(1.0, st)          # uptime 归零（容器重启）
    assert st.sleep_armed is False, "武装状态必须随等级回落而解除，不得粘滞"


def test_render_prompt_replaces_all_placeholders():
    pol = LifecyclePolicy(levels=[LevelSpec("L0", "a", 0, "", "none"),
                                  LevelSpec("L1", "b", 168, "", "notify")],
                          max_uptime_hours=720)
    spec = pol.levels[1]
    spec.prompt = "{uptime_hours}/{uptime_days}/{level_name}/{next_level_hours}/{remaining_hours}/{last_sleep_at}"
    text = pol.render_prompt(spec, 200.0, 1, 0.0)
    assert "{" not in text, f"提示词占位符未被全部替换: {text!r}"


def test_inject_cooldown_suppresses_repeat_same_level():
    pol = LifecyclePolicy(levels=[LevelSpec("L0", "a", 0, "", "none"),
                                  LevelSpec("L1", "b", 168, "hi", "notify")],
                          max_uptime_hours=720, inject_cooldown_sec=900)
    st = LifecycleState()
    assert pol.evaluate(200.0, st, now=1000.0)["inject_prompt"] is not None
    assert pol.evaluate(200.0, st, now=1100.0)["inject_prompt"] is None
    assert pol.evaluate(200.0, st, now=2000.0)["inject_prompt"] is not None


def test_on_sleep_done_resets_inject_cooldown_so_next_level_can_speak():
    pol = LifecyclePolicy(levels=[LevelSpec("L0", "a", 0, "", "none"),
                                  LevelSpec("L1", "b", 168, "hi", "notify")],
                          max_uptime_hours=720)
    st = LifecycleState()
    pol.evaluate(200.0, st, now=1000.0)
    pol.on_sleep_done(st, now=1100.0)
    assert pol.evaluate(200.0, st, now=1101.0)["inject_prompt"] is not None


# =========================================================================== #
# B. 安全护栏
# =========================================================================== #
def test_sleep_group_rejects_duplicate_targets():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]}, safety={"allowed_containers": ["napcat"]})
    spec = ActionSpec("sleep", "napcat", params={"targets": ["napcat", "napcat"],
                                                "duration_min": 0})
    d = guard.authorize(spec)
    assert d.allowed is False and d.code == "bad_targets", d


def test_sleep_group_rejects_target_not_matching_primary():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]}, safety={"allowed_containers": ["napcat"]})
    spec = ActionSpec("sleep", "napcat", params={"targets": ["other"],
                                                "duration_min": 0})
    d = guard.authorize(spec)
    assert d.allowed is False and d.code == "bad_targets", d


def test_sleep_group_rejects_over_ten_targets():
    names = [f"c{i}" for i in range(11)]
    guard, cfg, st, au = make_safety(
        containers={"managed": names}, safety={"allowed_containers": names})
    spec = ActionSpec("sleep", names[0],
                      params={"targets": names, "duration_min": 0})
    assert guard.authorize(spec).code == "bad_targets"


def test_sleep_duration_rejects_bool_and_float():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]}, safety={"allowed_containers": ["napcat"]})
    for bad in (True, 1.5, "5", None):
        spec = ActionSpec("sleep", "napcat",
                          params={"targets": ["napcat"], "duration_min": bad})
        d = guard.authorize(spec)
        assert d.allowed is False and d.code == "bad_duration", (bad, d)


def test_sleep_duration_accepts_zero_and_120():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]}, safety={"allowed_containers": ["napcat"]})
    for good in (0, 120):
        spec = ActionSpec("sleep", "napcat",
                          params={"targets": ["napcat"], "duration_min": good})
        assert guard.authorize(spec).allowed is True, good


def test_non_allowlisted_container_is_denied():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]}, safety={"allowed_containers": []})
    spec = ActionSpec("restart_container", "evil-box")
    d = guard.authorize(spec)
    assert d.allowed is False and d.code == "not_allowlisted", d


def test_protected_non_self_container_needs_self_restart_flag():
    """受保护容器（非本体）重启：当前实现要求 allow_self_restart。

    这是一个语义可疑点：allow_self_restart 的语义是"允许重启本体"，
    却被用来放行**任意**受保护容器。此处把行为钉死，供决策。
    """
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]},
        safety={"allowed_containers": [], "protected_containers": ["astrbot", "napcat"],
                "allow_self_restart": True})
    d = guard.authorize(ActionSpec("restart_container", "napcat"))
    assert d.allowed is True, f"开启 allow_self_restart 后受保护的非本体容器被放行: {d}"


def test_cooldown_blocks_second_action_on_same_target():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]},
        safety={"allowed_containers": [], "action_cooldown_sec": 600,
                "dry_run_off": True})
    cfg.raw["dry_run"] = False
    cfg.refresh()
    st.bump_cooldown("napcat", ts=1000.0)
    d = guard.authorize(ActionSpec("restart_container", "napcat"), now=1100.0)
    assert d.allowed is False and d.code == "cooldown", d


def test_memory_leak_cooldown_key_uses_longer_window():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]},
        safety={"allowed_containers": [], "action_cooldown_sec": 600},
        memory={"leak_cooldown": 3600})
    cfg.raw["dry_run"] = False
    cfg.refresh()
    st.bump_cooldown("napcat", ts=1000.0)
    spec = ActionSpec("restart_container", "napcat",
                      params={"cooldown_key": "memory_leak"})
    d = guard.authorize(spec, now=2000.0)   # 1000s 后：普通冷却已过，泄漏冷却未过
    assert d.allowed is False and d.code == "cooldown", d


def test_tripped_state_blocks_everything_until_manual_reset():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]}, safety={"allowed_containers": []})
    guard.trip("test")
    assert guard.authorize(ActionSpec("restart_container", "napcat")).code == "circuit_open"
    guard.reset_trip()
    assert guard.authorize(ActionSpec("restart_container", "napcat")).allowed is True


def test_circuit_breaker_trips_at_limit():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]},
        safety={"allowed_containers": [], "max_actions_per_hour": 2,
                "circuit_breaker_window_sec": 1800})
    cfg.raw["dry_run"] = False
    cfg.refresh()
    for _ in range(2):
        st.record_action()
    d = guard.authorize(ActionSpec("restart_container", "napcat"))
    assert d.allowed is False and d.code == "circuit_open", d
    assert guard.tripped is True


def test_validate_target_accepts_ros2_node_only_for_logical_kinds():
    ok, _ = validate_target("/camera_node", kind="ros2_restart")
    assert ok is True
    ok2, _ = validate_target("/camera_node", kind="restart_container")
    assert ok2 is False, "容器名不该接受前导斜杠"


def test_validate_target_rejects_injection_and_empty():
    for bad in ("", "a b", "a;b", "a`b", "a$b", "x" * 64, None, 123):
        ok, _ = validate_target(bad, kind="restart_container")
        assert ok is False, f"应拒绝 {bad!r}"


def test_watchdog_pending_blocks_physical_target_of_qq_relogin():
    """逻辑目标 qq 的重登会真重启 napcat；napcat 已被看门狗持有时必须拒绝。"""
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"], "napcat": "napcat"},
        qq={"relogin_method": "restart_container"},
        safety={"allowed_containers": []})
    st.set("watchdog.pending", {"nonce": "n", "targets": ["napcat"], "operation": "sleep"})
    d = guard.authorize(ActionSpec("qq_relogin", "qq"))
    assert d.allowed is False and d.code == "watchdog_pending", d


def test_commit_dry_run_does_not_consume_quota_or_cooldown():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]}, safety={"allowed_containers": []})
    spec = ActionSpec("restart_container", "napcat")
    spec.dry_run = True
    guard.commit(spec, True)
    assert st.get("action_history") in (None, [])
    assert st.cooldown_of("napcat") == 0.0


def test_commit_failure_bumps_streak_and_downgrades_after_threshold():
    guard, cfg, st, au = make_safety(
        containers={"managed": ["napcat"]},
        safety={"allowed_containers": [], "max_consecutive_failures": 2})
    cfg.raw["dry_run"] = False
    cfg.refresh()
    spec = ActionSpec("restart_container", "napcat")
    guard.commit(spec, False)
    guard.commit(spec, False)
    d = guard.authorize(spec)
    assert d.allowed is False and d.code == "fail_streak", d


# =========================================================================== #
# C. 配置校验
# =========================================================================== #
def test_config_rejects_bool_sleep_duration():
    cfg = make_config(lifecycle={"sleep_duration_min": True})
    assert cfg.raw["lifecycle"]["sleep_duration_min"] == 0
    assert any("sleep_duration_min" in i for i in cfg.issues)


def test_config_clips_sleep_duration_to_range():
    cfg = make_config(lifecycle={"sleep_duration_min": 999})
    assert cfg.raw["lifecycle"]["sleep_duration_min"] == 120
    cfg2 = make_config(lifecycle={"sleep_duration_min": -5})
    assert cfg2.raw["lifecycle"]["sleep_duration_min"] == 0


def test_config_sorts_levels_and_reports():
    cfg = make_config(lifecycle={"levels": [
        {"id": "B", "after_hours": 100, "action": "notify"},
        {"id": "A", "after_hours": 10, "action": "none"},
    ]})
    ids = [lv["id"] for lv in cfg.raw["lifecycle"]["levels"]]
    assert ids == ["A", "B"], ids
    assert any("升序" in i for i in cfg.issues)


def test_config_drops_non_object_levels():
    cfg = make_config(lifecycle={"levels": [
        "not-a-dict", {"id": "A", "after_hours": 0, "action": "none"}]})
    assert len(cfg.raw["lifecycle"]["levels"]) == 1


def test_config_raises_max_uptime_above_biggest_level():
    cfg = make_config(lifecycle={
        "levels": [{"id": "L0", "after_hours": 0, "action": "none"},
                   {"id": "L1", "after_hours": 5000, "action": "none"}],
        "max_uptime_hours": 720})
    assert cfg.raw["lifecycle"]["max_uptime_hours"] == 5000


def test_config_invalid_level_action_falls_back_to_none():
    cfg = make_config(lifecycle={"levels": [
        {"id": "L0", "after_hours": 0, "action": "explode"}]})
    assert cfg.raw["lifecycle"]["levels"][0]["action"] == "none"


def test_config_memory_samples_expanded_to_cover_span():
    cfg = make_config(memory={"samples": 5, "sample_interval": 300,
                              "leak_min_span_sec": 1800})
    assert cfg.raw["memory"]["samples"] >= math.ceil(1800 / 300) + 1


def test_config_negative_thresholds_rejected():
    cfg = make_config(memory={"growth_warn_mb_per_hour": -1})
    assert cfg.raw["memory"]["growth_warn_mb_per_hour"] == 80.0
    cfg2 = make_config(safety={"action_cooldown_sec": -10})
    assert cfg2.raw["safety"]["action_cooldown_sec"] == 600


def test_config_sleep_targets_pruned_to_allowlist():
    cfg = make_config(
        containers={"managed": ["napcat"]},
        lifecycle={"sleep_targets": ["napcat", "ghost"]})
    assert cfg.raw["lifecycle"]["sleep_targets"] == ["napcat"]


def test_config_unknown_attribute_raises_clearly():
    cfg = make_config()
    try:
        cfg.no_such_key
    except AttributeError as exc:
        assert "no_such_key" in str(exc)
    else:
        raise AssertionError("未知配置项应抛 AttributeError")


def test_config_profile_interval_bounds():
    cfg = make_config(check_interval=60)
    assert cfg.profile_interval("normal") == 60
    assert cfg.profile_interval("night") == 180
    assert cfg.profile_interval("aggressive") == 20
    assert cfg.profile_interval("maintenance") == 0
    cfg2 = make_config(check_interval=5)
    assert cfg2.profile_interval("aggressive") == 10


# =========================================================================== #
# D. 调度器 / cron
# =========================================================================== #
def test_cron_dow_seven_equals_sunday():
    from datetime import datetime
    # 2026-10-18 是周日
    sunday = datetime(2026, 10, 18, 3, 0)
    assert sunday.weekday() == 6
    assert cron_match("0 3 * * 7", sunday) is True
    assert cron_match("0 3 * * 0", sunday) is True


def test_cron_dom_and_dow_use_or_semantics():
    from datetime import datetime
    # 2026-10-05 是周一（day=5），dow=1
    d = datetime(2026, 10, 5, 3, 0)
    assert cron_match("0 3 5 * 1", d) is True     # 两者都命中
    assert cron_match("0 3 6 * 1", d) is True     # 仅 dow 命中 -> OR
    assert cron_match("0 3 5 * 2", d) is True     # 仅 dom 命中 -> OR
    assert cron_match("0 3 6 * 2", d) is False    # 都不命中


def test_cron_step_and_range():
    assert parse_cron("*/15 * * * *").minute == {0, 15, 30, 45}
    assert parse_cron("0 9-11 * * *").hour == {9, 10, 11}
    assert parse_cron("0 0 * * 1-5").dow == {1, 2, 3, 4, 5}


def test_cron_rejects_malformed():
    for bad in ("", "* * * *", "* * * * * *", "60 * * * *", "0 24 * * *",
                "0 0 0 * *", "a * * * *", "*/0 * * * *"):
        try:
            parse_cron(bad)
        except Exception:
            continue
        raise AssertionError(f"应拒绝非法 cron: {bad!r}")


def test_next_run_is_strictly_after_input():
    from datetime import datetime
    base = datetime(2026, 10, 14, 4, 0)
    nxt = next_run("0 4 * * *", base)
    assert nxt is not None and nxt > base
    assert (nxt.day, nxt.hour, nxt.minute) == (15, 4, 0), nxt


# =========================================================================== #
# E. 内存口径与趋势
# =========================================================================== #
def test_working_set_prefers_total_inactive_file_then_inactive_then_cache():
    mem = {"usage": 100 * 1024 * 1024,
           "stats": {"total_inactive_file": 10 * 1024 * 1024,
                     "inactive_file": 20 * 1024 * 1024,
                     "cache": 30 * 1024 * 1024}}
    ws, cache, source = memory_working_set(mem)
    assert source == "usage-total_inactive_file"
    assert abs(ws - 90 * 1024 * 1024) < 1


def test_working_set_does_not_subtract_rss():
    """cgroup v2 上 rss 恒为 0，绝不能把 rss 当缓存扣除。"""
    mem = {"usage": 100 * 1024 * 1024, "stats": {"rss": 0, "anon": 60 * 1024 * 1024}}
    ws, cache, source = memory_working_set(mem)
    assert source == "usage", source
    assert cache == 0.0
    assert abs(ws - 100 * 1024 * 1024) < 1


def test_working_set_rejects_negative_or_nonfinite_usage():
    for bad in ({"usage": -1}, {"usage": float("nan")}, {"usage": float("inf")}):
        try:
            memory_working_set({**bad, "stats": {}})
        except ValueError:
            continue
        raise AssertionError(f"应拒绝 usage={bad['usage']!r}")


def test_working_set_zero_cache_field_is_authoritative():
    """字段存在且为 0 时不得回退到别的键（避免口径静默切换）。"""
    mem = {"usage": 100, "stats": {"total_inactive_file": 0, "inactive_file": 50}}
    ws, cache, source = memory_working_set(mem)
    assert source == "usage-total_inactive_file"
    assert ws == 100.0


def test_slope_needs_at_least_three_points():
    assert compute_slope_mb_per_hour([]) == 0.0
    assert compute_slope_mb_per_hour([[0, 1], [1, 2]]) == 0.0


def test_slope_flat_is_zero_and_rising_is_positive():
    flat = [[0, 100], [300, 100], [600, 100]]
    assert compute_slope_mb_per_hour(flat) == 0.0
    rising = [[0, 100], [1800, 200], [3600, 300]]   # +200MB over 1h
    assert abs(compute_slope_mb_per_hour(rising) - 200.0) < 1e-6


def test_slope_handles_duplicate_timestamps():
    assert compute_slope_mb_per_hour([[5, 1], [5, 2], [5, 3]]) == 0.0


def test_parse_started_at_handles_nanoseconds_and_z():
    a = parse_started_at("2026-10-14T10:35:24.123456789Z")
    b = parse_started_at("2026-10-14T10:35:24.123456Z")
    assert a > 0 and b > 0 and abs(a - b) < 0.001


def test_parse_started_at_handles_offset():
    z = parse_started_at("2026-10-14T10:35:24Z")
    off = parse_started_at("2026-10-14T18:35:24+08:00")
    assert abs(z - off) < 0.001, (z, off)


def test_parse_started_at_bad_input_returns_zero():
    for bad in ("", "not-a-date", "2026-13-45T99:99:99Z", None):
        assert parse_started_at(bad) == 0.0, bad


# =========================================================================== #
# F. 判定策略
# =========================================================================== #
def test_memory_restart_only_when_switch_on():
    from astrbot_plugin_guardian.core.models import ProbeResult, Severity
    off = PolicyEngine(make_config(memory={"restart_on_leak": False}))
    on = PolicyEngine(make_config(memory={"restart_on_leak": True}))
    res = ProbeResult(probe="container_memory", target="napcat", status=Status.WARN,
                      severity=Severity.WARN, message="leak", metrics={"leak": True})
    assert off.evaluate([res])[0].suggested_action is None
    act = on.evaluate([res])[0].suggested_action
    assert act is not None and act.kind == "restart_container"
    assert act.params.get("cooldown_key") == "memory_leak"


def test_unknown_and_ok_never_suggest_action():
    from astrbot_plugin_guardian.core.models import ProbeResult, Severity
    pol = PolicyEngine(make_config())
    for st in (Status.OK, Status.UNKNOWN):
        res = ProbeResult(probe="container_health", target="napcat", status=st,
                          severity=Severity.WARN, message="x",
                          metrics={"running": False, "missing": True})
        assert pol.evaluate([res])[0].suggested_action is None, st


def test_container_health_missing_beats_running_false_order():
    from astrbot_plugin_guardian.core.models import ProbeResult, Severity
    pol = PolicyEngine(make_config())
    res = ProbeResult(probe="container_health", target="ghost", status=Status.CRIT,
                      severity=Severity.CRIT, message="gone",
                      metrics={"missing": True, "running": False})
    act = pol.evaluate([res])[0].suggested_action
    assert act is not None and act.kind == "start_container"


# =========================================================================== #
# G. 已定位缺陷的回归（每一个都对应一次真实修复）
# =========================================================================== #
def test_write_atomic_honours_prebuilt_payload():
    """缺陷：flush 让工作线程自己去遍历 self._data，与事件循环的 set() 竞争，
    会抛 `RuntimeError: dictionary changed size during iteration`。

    修复要求 `_write_atomic` 只写**传进来的**快照。这里确定性地验证：
    快照之后修改内存态，落盘的仍必须是快照内容。
    """
    import json
    import tempfile
    from astrbot_plugin_guardian.core.state import StateStore

    with tempfile.TemporaryDirectory() as d:
        st = StateStore(d)
        st.set("mode", "server")
        payload = st.snapshot()
        st.set("mode", "ros2")        # 快照之后又改
        st._write_atomic(payload)
        written = json.loads(st.path.read_text(encoding="utf-8"))
        assert written["mode"] == "server", written


def test_flush_builds_snapshot_in_caller_thread():
    """把修复本身钉死：flush 必须先在事件循环线程取好快照，再交给工作线程。"""
    import inspect
    from astrbot_plugin_guardian.core.state import StateStore

    src = inspect.getsource(StateStore.flush)
    assert "payload = self.snapshot()" in src, src
    assert "to_thread(self._write_atomic, payload)" in src, src


def test_state_flush_survives_concurrent_mutation():
    """端到端：事件循环里一边高频改状态、一边落盘，不得抛任何异常。"""
    import tempfile
    from astrbot_plugin_guardian.core.state import StateStore

    async def scenario():
        with tempfile.TemporaryDirectory() as d:
            st = StateStore(d, flush_interval=0.0)
            for i in range(3000):
                st.set(f"pre_{i}", i)
            done = asyncio.Event()
            errors: list[BaseException] = []

            async def mutator():
                i = 0
                while not done.is_set():
                    st.set(f"new_{i}", i)
                    i += 1
                    if i % 200 == 0:
                        await asyncio.sleep(0)
                    if i > 200000:
                        break

            task = asyncio.create_task(mutator())
            try:
                for _ in range(40):
                    await st.flush(force=True)
                    await asyncio.sleep(0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                done.set()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return errors

    errors = asyncio.run(scenario())
    assert not errors, errors


def test_safe_int_rejects_nonfinite_without_raising():
    """缺陷：int(float('inf')) 抛 OverflowError，只捕 ValueError 会击穿工具层。"""
    from astrbot_plugin_guardian.tools.llm_tools import safe_int

    assert safe_int(float("inf"), 7) == 7
    assert safe_int(float("-inf"), 7) == 7
    assert safe_int(float("nan"), 7) == 7
    assert safe_int(None, 7) == 7
    assert safe_int("abc", 7) == 7
    assert safe_int(1e400, 7) == 7          # 字面量溢出 -> inf
    assert safe_int(12.9, 7) == 12
    assert safe_int("42", 7) == 42
    assert safe_int(999, 7, low=1, high=50) == 50
    assert safe_int(-5, 7, low=1, high=50) == 1


def test_request_sleep_with_infinite_duration_is_handled():
    """缺陷：LLM 传 duration_min=Infinity 时工具层抛 OverflowError。"""
    import astrbot_plugin_guardian.tools.llm_tools as lt

    seen = {}

    class Cfg:
        class lifecycle:
            enabled = True
        dry_run = False

    class RT:
        config = Cfg()

        async def request_sleep(self, reason, dur, **kw):
            seen["dur"] = dur
            return True, "ok"

    out = asyncio.run(lt.request_sleep(RT(), "reason", float("inf"),
                                      is_admin=True, armed=True))
    assert seen["dur"] == 0, seen
    assert "已开始睡眠" in out


def test_list_incidents_with_infinite_limit_is_handled():
    import astrbot_plugin_guardian.tools.llm_tools as lt

    class RT:
        def incidents_text(self, limit):
            return f"limit={limit}"

    assert lt.list_incidents(RT(), float("inf")) == "limit=10"
    assert lt.list_incidents(RT(), float("nan")) == "limit=10"
    assert lt.list_incidents(RT(), 999) == "limit=50"
    assert lt.list_incidents(RT(), 0) == "limit=1"


def test_exec_run_timeout_maps_to_docker_unavailable():
    """缺陷：exec_run 未捕 asyncio.TimeoutError，原始 TimeoutError 击穿降级契约。"""
    from astrbot_plugin_guardian.utils.dockerclient import DockerClient, DockerUnavailable

    class FakeCM:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def post(self, *a, **k):
            return FakeCM()

    async def scenario():
        client = DockerClient()

        async def fake_request(*a, **k):
            return {"Id": "exec-id"}

        async def fake_ensure():
            return FakeSession()

        client._request = fake_request
        client._ensure_session = fake_ensure
        try:
            await client.exec_run("c1", ["ros2", "node", "list"], timeout=1)
        except DockerUnavailable:
            return "unavailable"
        except BaseException as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"
        return "no-raise"

    assert asyncio.run(scenario()) == "unavailable"


def test_force_source_distinguishes_level_from_deadline():
    """缺陷：两种 force_sleep 来源在日志/审计里都写成"硬上限触发"。"""
    pol = LifecyclePolicy(
        levels=[LevelSpec("L0", "常规", 0, "", "none"),
                LevelSpec("L9", "强制", 10, "", "force_sleep")],
        max_uptime_hours=720)
    d = pol.evaluate(11.0, LifecycleState())
    assert d["force_sleep"] is True and d["force_source"] == "level", d

    pol2 = LifecyclePolicy(levels=[LevelSpec("L0", "常规", 0, "", "none")],
                           max_uptime_hours=100)
    d2 = pol2.evaluate(100.0, LifecycleState())
    assert d2["force_source"] == "deadline", d2

    d3 = pol2.evaluate(1.0, LifecycleState())
    assert d3["force_sleep"] is False and d3["force_source"] == "", d3


# ---- 引擎级：生命周期 tick 的两个修复 -------------------------------------- #
class FakeNotifier:
    def __init__(self):
        self.pushed: list[tuple] = []

    async def push(self, text, **kw):
        self.pushed.append((text, kw))
        return True

    async def push_incidents(self, incidents, **kw):
        return None


def make_engine(**over):
    from astrbot_plugin_guardian.core.bus import EventBus
    from astrbot_plugin_guardian.core.engine import HealthEngine
    from astrbot_plugin_guardian.core.lifecycle import build_policy

    cfg = make_config(**over)
    st = FakeState()
    au = FakeAudit()
    eng = HealthEngine(cfg, st, au, EventBus(), FakeNotifier(),
                       SafetyGuard(cfg, st, au), PolicyEngine(cfg),
                       build_policy(cfg), {})
    return eng, st, au


def test_lifecycle_tick_ignores_untrusted_uptime():
    """缺陷：uptime 探针 WARN（时钟回拨）时 metrics 无 uptime_hours，
    旧代码取 0.0 把等级静默降回 L0 并清掉注入提示词。"""
    from astrbot_plugin_guardian.core.models import ProbeResult, Severity

    eng, st, _ = make_engine()
    # 先建立一份"可信的 L2 状态"
    st.set("lifecycle.state", {"uptime_hours": 400.0, "level_id": "L2",
                               "level_name": "II 级 · 建议休息", "level_index": 2})
    eng.lifecycle_state = LifecycleState.from_dict(st.get("lifecycle.state"))

    eng._last_results = {"uptime:system": ProbeResult(
        probe="uptime", target="system", status=Status.WARN, severity=Severity.WARN,
        message="检测到时钟回拨", metrics={"clock_skew": True})}
    asyncio.run(eng._lifecycle_tick("interval"))

    assert eng.lifecycle_state.level_id == "L2", eng.lifecycle_state.level_id
    assert eng.lifecycle_state.uptime_hours == 400.0, eng.lifecycle_state.uptime_hours


def test_lifecycle_tick_reports_level_force_as_level_not_deadline():
    from astrbot_plugin_guardian.core.models import ProbeResult, Severity

    eng, st, au = make_engine(lifecycle={
        "levels": [{"id": "L0", "name": "常规", "after_hours": 0, "action": "none"},
                   {"id": "L9", "name": "强制", "after_hours": 10, "action": "force_sleep"}],
        "max_uptime_hours": 720})
    calls = []

    async def fake_request_sleep(reason, duration_min=0, **kw):
        calls.append((reason, kw.get("trigger")))
        return True, "ok"

    eng.request_sleep = fake_request_sleep
    eng._last_results = {"uptime:system": ProbeResult(
        probe="uptime", target="system", status=Status.OK, severity=Severity.INFO,
        message="ok", metrics={"uptime_hours": 11.0})}
    asyncio.run(eng._lifecycle_tick("interval"))

    assert len(calls) == 1, calls
    reason, trigger = calls[0]
    assert trigger == "level", calls
    assert "硬上限" not in reason, reason
    assert "等级自身" in reason, reason


def test_lifecycle_tick_reports_deadline_force_as_deadline():
    from astrbot_plugin_guardian.core.models import ProbeResult, Severity

    eng, st, au = make_engine(lifecycle={
        "levels": [{"id": "L0", "name": "常规", "after_hours": 0, "action": "none"}],
        "max_uptime_hours": 100})
    calls = []

    async def fake_request_sleep(reason, duration_min=0, **kw):
        calls.append((reason, kw.get("trigger")))
        return True, "ok"

    eng.request_sleep = fake_request_sleep
    eng._last_results = {"uptime:system": ProbeResult(
        probe="uptime", target="system", status=Status.OK, severity=Severity.INFO,
        message="ok", metrics={"uptime_hours": 101.0})}
    asyncio.run(eng._lifecycle_tick("interval"))

    assert len(calls) == 1, calls
    reason, trigger = calls[0]
    assert trigger == "deadline", calls
    assert "硬上限" in reason, reason


def test_lifecycle_tick_no_uptime_result_is_a_noop():
    eng, st, _ = make_engine()
    eng._last_results = {}
    asyncio.run(eng._lifecycle_tick("interval"))
    assert st.get("lifecycle.state") is None


# =========================================================================== #
# runner
# =========================================================================== #
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
