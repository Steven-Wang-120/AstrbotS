"""Guardian 本次 debug 修复专项验证。

在真实 AstrBot 容器内运行：
    docker exec -i astrbot-local python - < tests/verify_fixes.py

覆盖本次 debug 发现并修复的 5 个缺陷：
  F1 astrbot.api.event 里 filter/EventMessageType 的导入位置
  F2 AstrMessageEvent.message_str -> get_message_str()
  F3 _conf_schema.json 不支持的配置类型 "dict"
  F4 AuditLog.append(incident=...) 的 kind 必填
  F5 SafetyGuard 把逻辑目标(qq/ros2节点)当容器名卡白名单
  F6 QqLoginProbe 把"探测不可用"误判为"QQ 掉线"
"""

import asyncio
import importlib
import io
import json
import os
import sys

PLUGIN = "/AstrBot/data/plugins/astrbot_plugin_guardian"
ROOT = os.path.dirname(PLUGIN)
for p in (PLUGIN, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
PKG = "astrbot_plugin_guardian"

OK, FAIL = [], []


def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  <- {detail}" if detail else ""))


def main():  # noqa: ANN001
    print("=" * 72)
    print("F1  导入位置")
    print("=" * 72)
    try:
        from astrbot.api.event import filter as f  # noqa: F401
        from astrbot.api.event.filter import EventMessageType as E  # noqa: F401
        from astrbot.api.event import AstrMessageEvent as AME  # noqa: F401
        check("filter/EventMessageType/AstrMessageEvent 可导入", True)
        check("EventMessageType.ALL 可用", E.ALL is not None)
    except Exception as exc:  # noqa: BLE001
        check("filter/EventMessageType 可导入", False, str(exc))

    print()
    print("=" * 72)
    print("F2  main 模块整体导入 + 处理器收集")
    print("=" * 72)
    try:
        mod = importlib.import_module(f"{PKG}.main")
        check("main 导入成功", True)
        check("_event_text 存在", hasattr(mod, "_event_text"))
        cls = mod.GuardianPlugin
        from astrbot.api.star import Star
        check("GuardianPlugin 继承 Star", issubclass(cls, Star))
        names = {n for n in dir(cls) if not n.startswith("_")}
        need = {"cmd_guardian", "on_astrbot_loaded", "on_plugin_loaded",
                "observe_message", "inject_guardian_context",
                "guardian_health_report", "guardian_request_sleep",
                "guardian_restart_container", "terminate"}
        missing = need - names
        check("关键处理器齐全", not missing, f"缺 {missing}" if missing else "")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("main 导入成功", False, str(exc))
        return

    print()
    print("=" * 72)
    print("F3  _conf_schema.json 配置类型")
    print("=" * 72)
    schema = json.load(io.open(os.path.join(PLUGIN, "_conf_schema.json"),
                               encoding="utf-8"))
    from astrbot.core.config.astrbot_config import AstrBotConfig

    try:
        cfg = AstrBotConfig(config_path="/tmp/_guardian_probe_cfg.json", schema=schema)
        check("AstrBotConfig 可用 schema 构造", True)
        check("ros2.node_restart_commands 默认为 {}",
              cfg.get("ros2", {}).get("node_restart_commands") == {})
        check("lifecycle.levels 有默认等级",
              len(cfg.get("lifecycle", {}).get("levels") or []) == 5,
              f"长度={len(cfg.get('lifecycle', {}).get('levels') or [])}")
    except Exception as exc:  # noqa: BLE001
        check("AstrBotConfig 可用 schema 构造", False, str(exc))

    bad_types = [t for t in _walk_types(schema) if t == "dict"]
    check("schema 不含 'dict' 类型", not bad_types, str(bad_types))

    print()
    print("=" * 72)
    print("F4  AuditLog.append(incident=...)")
    print("=" * 72)
    audit_mod = importlib.import_module(f"{PKG}.core.audit")
    AuditLog = audit_mod.AuditLog
    models = importlib.import_module(f"{PKG}.core.models")
    Inc, Spec = models.Incident, models.ActionSpec
    Status, Severity = models.Status, models.Severity
    log = AuditLog("/tmp/_guardian_audit_probe")
    inc = Inc(probe="p", target="t", status=Status.CRIT, severity=Severity.CRIT,
              message="m", trigger="test")
    try:
        log.append(incident=inc)
        log.append(kind="unit.probe", target="t", detail="d")
        lines = io.open(log.path, encoding="utf-8").read().strip().splitlines()
        kinds = [json.loads(x).get("kind") for x in lines]
        check("append(incident=...) 不抛异常", True)
        check("incident 分支 kind 落盘为 incident", "incident" in kinds, str(kinds))
        check("append(kind=...) 仍然可用", "unit.probe" in kinds, str(kinds))
    except Exception as exc:  # noqa: BLE001
        check("append(incident=...) 不抛异常", False, str(exc))

    print()
    print("=" * 72)
    print("F5  逻辑目标授权（qq_relogin / ros2_restart）")
    print("=" * 72)
    safety_mod = importlib.import_module(f"{PKG}.core.safety")
    SafetyGuard = safety_mod.SafetyGuard

    class FakeState:
        def __init__(self):
            self._d = {}

        def get(self, k, default=None):
            return self._d.get(k, default)

        def set(self, k, v):
            self._d[k] = v

        def cooldown_of(self, t):
            return 0.0

        def record_action(self):
            pass

        def actions_in_window(self, w):
            return 0

        def bump_fail_streak(self, t):
            return 0

        def reset_fail_streak(self, t):
            pass

    cfg_mod = importlib.import_module(f"{PKG}.core.config")
    raw = {
        "containers": {"astrbot": "astrbot-local", "napcat": "napcat-local",
                       "managed": ["astrbot-local", "napcat-local"]},
        "safety": {"allowed_containers": ["astrbot-local", "napcat-local"],
                   "protected_containers": ["astrbot-local"]},
        "lifecycle": {"sleep_targets": ["napcat-local"],
                      "uptime_targets": ["napcat-local"]},
    }
    cfg = cfg_mod.build_config(raw)
    guard = SafetyGuard(cfg, FakeState(), AuditLog("/tmp/_guardian_audit_probe"))

    d = guard.authorize(Spec("qq_relogin", "qq", params={}, reason="t"), now=1000.0)
    check("qq_relogin(target='qq') 不被容器白名单误拒",
          not (not d.allowed and d.code == "not_allowlisted"),
          f"allowed={d.allowed} code={d.code} reason={d.reason}")

    d = guard.authorize(Spec("restart_container", "napcat-local", params={}), now=1000.0)
    check("restart_container(白名单内容器) 放行", d.allowed, f"{d.reason}/{d.code}")

    d = guard.authorize(Spec("restart_container", "evil", params={}), now=1000.0)
    check("restart_container(非白名单) 仍被拒",
          (not d.allowed) and d.code == "not_allowlisted", d.code)

    d = guard.authorize(Spec("ros2_restart", "/node_x", params={}), now=1000.0)
    check("ros2_restart(未配恢复命令) 被拒",
          (not d.allowed) and d.code == "ros2_no_command", d.code)

    raw2 = json.loads(json.dumps(raw))
    raw2["ros2"] = {"node_restart_commands": {"/node_x": ["docker", "restart", "x"]}}
    cfg2 = cfg_mod.build_config(raw2)
    guard2 = SafetyGuard(cfg2, FakeState(), AuditLog("/tmp/_guardian_audit_probe"))
    d = guard2.authorize(Spec("ros2_restart", "/node_x", params={}), now=1000.0)
    check("ros2_restart(已配恢复命令) 放行", d.allowed, f"{d.reason}/{d.code}")

    print()
    print("=" * 72)
    print("F6  QQ 探针：探测不可用不得判为掉线")
    print("=" * 72)

    napcat_mod = importlib.import_module(f"{PKG}.utils.napcat")
    NapCatUnavailable = napcat_mod.NapCatUnavailable

    class FakeNapcat:
        async def get_status(self):
            raise NapCatUnavailable("unreachable")

        async def get_login_info(self):
            raise NapCatUnavailable("unreachable")

    probe_mod = importlib.import_module(f"{PKG}.probes.qq_login")
    QqLoginProbe = probe_mod.QqLoginProbe

    class FakeProbeState:
        def __init__(self):
            self._d = {}

        def get(self, k, default=None):
            return self._d.get(k, default)

        def set(self, k, v):
            self._d[k] = v

    probe = QqLoginProbe(cfg, {"napcat": None, "state": FakeProbeState()}, "qq")
    probe.napcat = FakeNapcat()

    async def run_probe():
        return await probe.check()

    res = asyncio.run(run_probe())
    check("全部证据不可达 -> UNKNOWN",
          str(getattr(res.status, "value", res.status)) == "unknown",
          str(res.status))
    check("不进入掉线计数", probe.state.get("qq_fail_streak", 0) == 0)

    class FakeNapcatOnline:
        async def get_status(self):
            return {"data": {"online": True, "good": True}}

        async def get_login_info(self):
            return {"data": {"user_id": "2186059326", "nickname": "t"}}

    probe.napcat = FakeNapcatOnline()
    res = asyncio.run(run_probe())
    check("明确在线 -> OK",
          str(getattr(res.status, "value", res.status)) == "ok", str(res.status))

    class FakeNapcatOffline:
        async def get_status(self):
            return {"data": {"online": False}}

        async def get_login_info(self):
            return {"data": {"user_id": "", "nickname": ""}}

    probe.state = FakeProbeState()
    probe.napcat = FakeNapcatOffline()
    r1 = asyncio.run(run_probe())
    r2 = asyncio.run(run_probe())
    check("明确离线 2 轮 -> CRIT",
          str(getattr(r2.status, "value", r2.status)) == "crit", str(r2.status))
    check("掉线计数按轮累加", probe.state.get("qq_fail_streak") == 2)

    print()
    print("=" * 72)
    print(f"汇总：PASS {len(OK)} / FAIL {len(FAIL)}")
    if FAIL:
        print("未通过：")
        for n in FAIL:
            print("  -", n)
    print("=" * 72)
    return 0 if not FAIL else 1


def _walk_types(node):
    out = []
    if isinstance(node, dict):
        if isinstance(node.get("type"), str):
            out.append(node["type"])
        for v in node.values():
            out.extend(_walk_types(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk_types(v))
    return out


if __name__ == "__main__":
    sys.exit(main())
