"""Guardian 守护系统 —— 插件装载集成测试。

对应文档：C01（安装与启动冒烟）、C09（LLM 工具注册）

用最小 stub 模拟 AstrBot 的 API 表面，验证：
- main.py 可被导入，装饰器可正常应用，插件类可实例化
- 命令处理器与 9 个 LLM 工具均已注册
- 生命周期提示词注入走 extra_user_content_parts 而非 system_prompt
- 运行时装配成功且核心查询可用

直接运行：`python tests/test_plugin_load.py`
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# --------------------------------------------------------------------------- #
# AstrBot API stub
# --------------------------------------------------------------------------- #
REGISTERED: dict[str, list] = {"commands": [], "tools": [], "hooks": [], "events": []}


def _install_astrbot_stub() -> None:
    try:
        import astrbot  # noqa: F401
        return
    except ImportError:
        pass

    def mk(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = m
        return m

    astrbot = mk("astrbot")
    api = mk("astrbot.api")
    api_event = mk("astrbot.api.event")
    api_star = mk("astrbot.api.star")
    api_provider = mk("astrbot.api.provider")
    core = mk("astrbot.core")
    core_agent = mk("astrbot.core.agent")
    core_agent_message = mk("astrbot.core.agent.message")

    class _Logger:
        def _noop(self, *a, **k):
            return None
        debug = info = warning = error = critical = exception = _noop

    api.logger = _Logger()  # type: ignore[attr-defined]

    # ---- filter ----
    class _Filter:
        def _deco(self, bucket, *a, **k):
            def inner(fn):
                REGISTERED[bucket].append((getattr(fn, "__name__", "?"), a, k))
                return fn
            return inner

        def command(self, *a, **k):
            return self._deco("commands", *a, **k)

        def command_group(self, *a, **k):
            return self._deco("commands", *a, **k)

        def llm_tool(self, *a, **k):
            return self._deco("tools", *a, **k)

        def event_message_type(self, *a, **k):
            return self._deco("events", *a, **k)

        def platform_adapter_type(self, *a, **k):
            return self._deco("events", *a, **k)

        def permission_type(self, *a, **k):
            return self._deco("events", *a, **k)

        def regex(self, *a, **k):
            return self._deco("events", *a, **k)

        def __getattr__(self, name):
            if name.startswith("on_") or name.startswith("after_"):
                return lambda *a, **k: self._deco("hooks", *a, **k)
            raise AttributeError(name)

    flt = _Filter()
    # 真实 AstrBot 4.2x 中 astrbot.api.event.filter 是一个模块（不是对象），
    # 装饰器与 EventMessageType 都在其下。桩必须按同样的布局造，否则测不出导入错位。
    api_event_filter = mk("astrbot.api.event.filter")
    for _attr in ("command", "command_group", "llm_tool", "event_message_type",
                  "platform_adapter_type", "permission_type", "regex"):
        setattr(api_event_filter, _attr, getattr(flt, _attr))
    for _hook in ("on_astrbot_loaded", "on_llm_request", "on_using_llm_tool",
                  "on_llm_tool_respond", "on_plugin_loaded", "on_plugin_unloaded"):
        setattr(api_event_filter, _hook, flt.__getattr__(_hook))

    class EventMessageType:
        ALL = "all"
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"
        OTHER_MESSAGE = "other"

    api_event_filter.EventMessageType = EventMessageType  # type: ignore[attr-defined]
    api_event.EventMessageType = EventMessageType  # type: ignore[attr-defined]
    api_event.filter = api_event_filter  # type: ignore[attr-defined]
    api_event.PlatformAdapterType = type("PAT", (), {"AIOCQHTTP": "aiocqhttp"})
    api_event.PermissionType = type("PT", (), {"ADMIN": "admin"})

    class AstrMessageEvent:
        def __init__(self, text="", admin=True):
            self.message_str = text
            self.unified_msg_origin = "umo://test"
            self._admin = admin

        def get_message_str(self):
            # 真实 4.x 是方法；插件曾直接读 .message_str 属性导致 AttributeError
            return self.message_str

        def get_sender_name(self):
            return "tester"

        def is_admin(self):
            return self._admin

        def plain_result(self, text):
            return ("plain", text)

    api_event.AstrMessageEvent = AstrMessageEvent  # type: ignore[attr-defined]

    # ---- star ----
    class Context:
        def __init__(self):
            self.sent: list[tuple] = []

        async def send_message(self, umo, chain):
            self.sent.append((umo, chain))

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*a, **k):
        def deco(cls):
            cls._register_args = a
            return cls
        return deco

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name=None):
            d = os.path.join(ROOT, "tests", "_tmp", "data")
            os.makedirs(d, exist_ok=True)
            return d

    api_star.Context = Context  # type: ignore[attr-defined]
    api_star.Star = Star  # type: ignore[attr-defined]
    api_star.register = register  # type: ignore[attr-defined]
    api_star.StarTools = StarTools  # type: ignore[attr-defined]

    api_provider.ProviderRequest = type("ProviderRequest", (), {})
    api_provider.LLMResponse = type("LLMResponse", (), {})

    class TextPart:
        def __init__(self, text=""):
            self.text = text

    core_agent_message.TextPart = TextPart  # type: ignore[attr-defined]

    astrbot.api = api  # type: ignore[attr-defined]
    api.event = api_event  # type: ignore[attr-defined]
    api.star = api_star  # type: ignore[attr-defined]
    api.provider = api_provider  # type: ignore[attr-defined]
    astrbot.core = core  # type: ignore[attr-defined]
    core.agent = core_agent  # type: ignore[attr-defined]
    core_agent.message = core_agent_message  # type: ignore[attr-defined]


_install_astrbot_stub()

try:
    import aiohttp  # noqa: F401
except ImportError as exc:  # pragma: no cover
    print(f"需要 aiohttp 才能运行本测试：{exc}")
    raise SystemExit(0)

from astrbot.api.event import AstrMessageEvent  # noqa: E402
from astrbot.api.star import Context  # noqa: E402

import astrbot_plugin_guardian.main as plugin_main  # noqa: E402
from astrbot_plugin_guardian.main import GuardianPlugin  # noqa: E402

EXPECTED_TOOLS = {
    "guardian_health_report",
    "guardian_lifecycle_status",
    "guardian_run_check",
    "guardian_diagnose",
    "guardian_list_incidents",
    "guardian_request_sleep",
    "guardian_restart_container",
    "guardian_set_mode",
    "guardian_set_profile",
}


def run(coro):
    return asyncio.run(coro)


def make_plugin(admin=True):
    ctx = Context()
    p = GuardianPlugin(ctx, {"containers": {"managed": ["astrbot", "napcat"]},
                             "dry_run": True})
    return p, ctx


# --------------------------------------------------------------------------- #
def test_plugin_class_registered():
    assert plugin_main.PLUGIN_NAME == "astrbot_plugin_guardian"
    assert getattr(GuardianPlugin, "_register_args", None) is not None
    args = GuardianPlugin._register_args
    assert len(args) == 4, f"@register 需要 4 个参数，实际 {len(args)}"


def test_tools_registered():
    names = {n for n, _a, _k in REGISTERED["tools"]}
    missing = EXPECTED_TOOLS - names
    assert not missing, f"缺少 LLM 工具: {missing}"
    # 工具方法确实存在于类上
    for tool in EXPECTED_TOOLS:
        assert hasattr(GuardianPlugin, tool), tool


def test_commands_and_hooks_registered():
    cmds = {n for n, _a, _k in REGISTERED["commands"]}
    assert "cmd_guardian" in cmds
    hooks = {n for n, _a, _k in REGISTERED["hooks"]}
    for h in ("on_astrbot_loaded", "inject_guardian_context",
              "on_using_tool", "on_tool_respond"):
        assert h in hooks, f"缺少钩子 {h}"
    events = {n for n, _a, _k in REGISTERED["events"]}
    assert "observe_message" in events


def test_runtime_assembly_and_status():
    p, _ctx = make_plugin()
    assert p.runtime.config.dry_run is True
    assert set(p.runtime.allowlist()) == {"astrbot", "napcat"}
    text = p.runtime.status_text()
    assert "守护系统健康快照" in text
    assert "生命周期" in text
    assert "尚未执行过巡检" in text


def test_start_and_stop_are_clean():
    async def main():
        p, _ctx = make_plugin()
        await p.runtime.start()
        assert p.runtime._started is True
        jobs = {j.name for j in p.runtime.scheduler.jobs()}
        assert "guardian:patrol" in jobs
        assert "guardian:qq" in jobs, "QQ 独立巡检任务必须存在"
        qq_job = p.runtime.scheduler.get("guardian:qq")
        assert qq_job.interval == 1200, qq_job.interval
        await p.terminate()
        assert p.runtime._started is False
        assert p.runtime.scheduler.jobs() == [], "terminate 后不应残留任务"

    run(main())


def test_command_dispatch_help_and_status():
    async def main():
        p, _ctx = make_plugin()
        out = []
        async for chunk in p.cmd_guardian(AstrMessageEvent("/guardian help")):
            out.append(chunk[1])
        assert out and "守护系统 Guardian" in out[0]

        out2 = []
        async for chunk in p.cmd_guardian(AstrMessageEvent("/guardian status")):
            out2.append(chunk[1])
        assert "守护系统健康快照" in out2[0]

        out3 = []
        async for chunk in p.cmd_guardian(AstrMessageEvent("/guardian level")):
            out3.append(chunk[1])
        assert "运行时长" in out3[0]

        # 非管理员被拒
        out4 = []
        async for chunk in p.cmd_guardian(
                AstrMessageEvent("/guardian restart napcat", admin=False)):
            out4.append(chunk[1])
        assert "管理员" in out4[0]

    run(main())


def test_restart_command_whitelist_enforced():
    async def main():
        p, _ctx = make_plugin()
        out = []
        async for chunk in p.cmd_guardian(
                AstrMessageEvent("/guardian restart nginx")):
            out.append(chunk[1])
        assert "白名单" in out[0]

    run(main())


def test_llm_tool_sleep_is_dry_run_safe():
    async def main():
        p, _ctx = make_plugin()
        out = []
        async for chunk in p.guardian_request_sleep(
                AstrMessageEvent("x"), "有点累了", 0):
            out.append(chunk[1])
        # dry_run 下不应真正重启
        assert out and ("dry-run" in out[0] or "未真正执行" in out[0]), out

    run(main())


def test_lifecycle_prompt_injection_uses_extra_parts():
    async def main():
        p, _ctx = make_plugin()
        # 手动放入一条等级提示词
        p.runtime.state.set("lifecycle.inject_prompt", "你已经工作很久了。")
        p.runtime.state.set("lifecycle.inject_level", "L2")

        class Req:
            def __init__(self):
                self.system_prompt = "BASE"
                self.extra_user_content_parts = []

        req = Req()
        await p.inject_guardian_context(AstrMessageEvent("hi"), req)
        assert req.system_prompt == "BASE", "不得污染 system_prompt"
        assert len(req.extra_user_content_parts) >= 1
        joined = "".join(getattr(x, "text", "") for x in req.extra_user_content_parts)
        assert "lifecycle_guard" in joined
        assert "你已经工作很久了。" in joined

    run(main())


def test_llm_tool_admin_gate():
    async def main():
        p, _ctx = make_plugin()
        out = []
        async for chunk in p.guardian_restart_container(
                AstrMessageEvent("x", admin=False), "napcat"):
            out.append(chunk[1])
        assert "管理员" in out[0]

        out2 = []
        async for chunk in p.guardian_set_mode(
                AstrMessageEvent("x", admin=False), "ros2"):
            out2.append(chunk[1])
        assert "管理员" in out2[0]

    run(main())


def test_destructive_tool_confirm_gate_and_armed_bypass():
    """B09 §6.2 / §179-201、B15 §9 A3：破坏性工具二次确认 + armed 全放行语义。

    - `confirm=false`：只回确认提示，**绝不**进入安全链/执行
    - `confirm=true` ：执行
    - `armed=true`   ：**全放行**（免管理员校验 + 免二次确认）——有意设计，非缺陷
    """
    async def main():
        p, _ctx = make_plugin()
        rt = p.runtime
        rt.config.raw["dry_run"] = False
        rt.config.raw["safety"]["allowed_containers"] = ["napcat"]
        rt.config.raw["containers"]["managed"] = ["napcat"]
        rt.config.raw["lifecycle"]["sleep_targets"] = ["napcat"]
        rt.config.refresh()

        calls: list[tuple] = []

        async def rec_restart(name, **kw):
            calls.append(("restart", name))
            return True, "ok"

        async def rec_sleep(reason, dur, **kw):
            calls.append(("sleep", dur, kw.get("bypass_confirm")))
            return True, "ok"

        rt.request_restart = rec_restart
        rt.request_sleep = rec_sleep

        async def tool_text(fn, *a, **kw):
            out = []
            async for c in fn(*a, **kw):
                out.append(c[1])
            return out[0]

        # 1) restart confirm=false -> 提示且不执行
        t = await tool_text(p.guardian_restart_container, AstrMessageEvent("x"), "napcat")
        assert "确认执行" in t and calls == [], (t, calls)

        # 2) restart confirm=true -> 执行
        t = await tool_text(p.guardian_restart_container, AstrMessageEvent("x"),
                            "napcat", confirm=True)
        assert calls == [("restart", "napcat")], (t, calls)
        calls.clear()

        # 3) sleep：非管理员且未武装 -> 管理员校验拒绝
        t = await tool_text(p.guardian_request_sleep,
                            AstrMessageEvent("x", admin=False), "累", 0)
        assert "管理员" in t and calls == [], (t, calls)

        # 4) sleep：管理员 confirm=false -> 二次确认提示且不执行
        t = await tool_text(p.guardian_request_sleep, AstrMessageEvent("x"), "累", 0)
        assert "确认执行" in t and calls == [], (t, calls)

        # 5) sleep：管理员 confirm=true -> 执行（bypass_confirm=True 下传引擎）
        t = await tool_text(p.guardian_request_sleep, AstrMessageEvent("x"),
                            "累", 0, True)
        assert calls == [("sleep", 0, True)], (t, calls)
        calls.clear()

        # 6) sleep：armed=true + **非管理员** -> 全放行（B15 §9 A3 有意设计）
        rt.engine.lifecycle_state.sleep_armed = True
        t = await tool_text(p.guardian_request_sleep,
                            AstrMessageEvent("x", admin=False), "累", 0)
        assert calls == [("sleep", 0, True)], (t, calls)

    run(main())


def test_engine_request_sleep_enforces_confirmation_gate():
    """引擎层强制闸门（纵深防御）：非权威来源未确认时必须拒绝并写审计。"""
    async def main():
        p, _ctx = make_plugin()
        rt = p.runtime
        ok, detail = await rt.engine.request_sleep("x", 0, actor="llm", trigger="llm")
        assert ok is False and "二次确认" in detail, (ok, detail)
        assert any(e.get("kind") == "sleep.confirm_required"
                   for e in rt.audit.tail(50)), "缺少 sleep.confirm_required 审计"

    run(main())


def test_lifecycle_hard_deadline_forces_sleep_in_dry_run():
    """30 天硬上限：dry-run 下应记录但不真正重启；本体受保护时优雅降级。"""
    async def main():
        p, _ctx = make_plugin()
        rt = p.runtime
        rt.config.raw["safety"]["allowed_containers"] = ["astrbot", "napcat"]
        rt.config.raw["lifecycle"]["sleep_targets"] = ["astrbot", "napcat"]
        rt.config.refresh()

        # 策略层：uptime 达到硬上限 -> force_sleep
        decision = rt.lifecycle_policy.evaluate(720.0, rt.engine.lifecycle_state, now=1.0)
        assert decision["force_sleep"] is True
        assert decision["action"] == "force_sleep"

        # 默认 allow_self_restart=False -> 本体被跳过，napcat 仍参与睡眠
        ok, detail = await rt.request_sleep("硬上限测试", 0, actor="lifecycle",
                                            trigger="deadline", bypass_confirm=True)
        assert ok, detail
        assert "dry-run" in detail
        assert "napcat" in detail
        assert "已跳过" in detail and "astrbot" in detail
        assert rt.docker._session is None, "dry-run 不应建立 Docker 连接"

        # 开启 allow_self_restart 后，本体也参与睡眠
        rt.config.raw["safety"]["allow_self_restart"] = True
        rt.config.refresh()
        ok2, detail2 = await rt.request_sleep("硬上限测试2", 0, actor="lifecycle",
                                              trigger="deadline", bypass_confirm=True)
        assert ok2, detail2
        assert "astrbot" in detail2 and "napcat" in detail2
        assert "已跳过" not in detail2

    run(main())


def test_sleep_denied_when_no_usable_target():
    """白名单为空且本体受保护时，睡眠应明确拒绝并给出指引。"""
    async def main():
        p, _ctx = make_plugin()
        rt = p.runtime
        rt.config.raw["safety"]["allowed_containers"] = []
        rt.config.raw["containers"]["managed"] = []
        ok, detail = await rt.request_sleep("测试", 0, actor="llm", trigger="llm",
                                            bypass_confirm=True)
        assert ok is False
        assert "allow_self_restart" in detail

    run(main())


def test_no_keyword_trigger_wiring():
    """证明：插件内部没有任何关键词匹配逻辑。"""
    import inspect

    src = inspect.getsource(plugin_main)
    for bad in ("我要睡觉", "晚安", "contains(", "startswith(\"睡觉", "regex("):
        assert bad not in src, f"main.py 出现了疑似关键词触发逻辑: {bad}"
    # 触发源仅三类：调度器 / 生命周期 / 工具调用
    assert "observe_message" in src  # 仅观测活跃度
    assert "request_sleep" in src


def test_reverse_ws_adapter_routing_is_explicit():
    async def main():
        p, ctx = make_plugin()
        calls = []
        class Client:
            async def call_action(self, action, **params):
                calls.append((action, params))
                return {"online": True}
        class Adapter:
            def meta(self):
                return types.SimpleNamespace(name="aiocqhttp", id="qq-main")
            def get_client(self):
                return Client()
        ctx.platform_manager = types.SimpleNamespace(platform_insts=[Adapter()])
        assert (await p.runtime.napcat.get_status())["online"] is True
        assert calls == [("get_status", {})]
        ctx.platform_manager.platform_insts.append(Adapter())
        from astrbot_plugin_guardian.utils.napcat import NapCatUnavailable
        try:
            await p.runtime.napcat.get_status()
        except NapCatUnavailable:
            pass
        else:
            raise AssertionError("多个适配器必须拒绝猜测")
    run(main())


def test_disabled_runtime_does_not_start_jobs():
    async def main():
        p, _ctx = make_plugin()
        p.runtime.config.raw["enabled"] = False
        p.runtime.config.refresh()
        await p.runtime.start()
        assert not p.runtime._started
        assert p.runtime.scheduler.jobs() == []
    run(main())


def test_scoped_patrol_preserves_other_probe_results():
    from astrbot_plugin_guardian.core.models import ProbeResult, Status, Severity
    async def main():
        p, _ctx = make_plugin()
        rt = p.runtime
        batch = [ProbeResult("container_memory", "napcat", Status.OK, Severity.INFO,
                             "normal", {"mem_mb": 100}),
                 ProbeResult("qq_login", "qq", Status.OK, Severity.INFO,
                             "online", {"online": True})]
        async def fake_probes(probes):
            return list(batch)
        rt.engine._run_probes = fake_probes
        await rt.engine.run_once(scope="all")
        batch[:] = [ProbeResult("qq_login", "qq", Status.UNKNOWN, Severity.WARN,
                                "unknown", {"online": None})]
        await rt.engine.run_once(scope="qq")
        results = rt.engine.snapshot()["results"]
        assert {r["probe"] for r in results} == {"container_memory", "qq_login"}
        assert next(r for r in results if r["probe"] == "qq_login")["status"] == "unknown"
    run(main())


def test_host_sleep_handoff_persists_and_does_not_mark_completed():
    import json
    import tempfile
    from pathlib import Path
    from astrbot_plugin_guardian.core.state import StateStore
    from astrbot_plugin_guardian.utils.watchdog import reconcile
    async def main():
        with tempfile.TemporaryDirectory(dir=os.path.join(HERE, "_tmp")) as directory:
            p, _ctx = make_plugin()
            rt = p.runtime
            state = StateStore(directory)
            state.load()
            rt.state = rt.safety.state = rt.engine.state = state
            rt.config.raw["dry_run"] = False
            rt.config.raw["safety"]["allow_self_restart"] = True
            request = Path(directory) / "restart_request.json"
            result = Path(directory) / "restart_result.json"
            rt.config.raw["safety"]["watchdog_request_path"] = str(request)
            rt.config.raw["safety"]["watchdog_result_path"] = str(result)
            rt.config.raw["lifecycle"]["sleep_targets"] = ["astrbot", "napcat"]
            rt.config.refresh()
            async def forbidden(*args, **kwargs):
                raise AssertionError("self sleep must never call plugin Docker")
            rt.docker.stop = rt.docker.start = rt.docker.restart = forbidden
            ok, detail = await rt.request_sleep("host test", 1, bypass_confirm=True)
            assert ok and "尚未确认" in detail
            payload = json.loads(request.read_text(encoding="utf-8"))
            assert payload["version"] == 2 and payload["targets"] == ["astrbot", "napcat"]
            assert payload["duration_min"] == 1
            pending = state.get("watchdog.pending")
            assert pending["nonce"] == payload["nonce"]
            assert json.loads(state.path.read_text())["watchdog.pending"] == pending
            assert set(state.get("cooldowns")) == {"astrbot", "napcat"}
            assert len(state.get("action_history")) == 1
            assert rt.engine.lifecycle_state.last_sleep_at == 0
            assert not (await rt.request_sleep("duplicate", 1, bypass_confirm=True))[0]
            result.write_text(json.dumps({"nonce": "other", "status": "completed", "ok": True,
                                          "targets": payload["targets"], "operation": "sleep"}))
            await reconcile(rt.engine)
            assert state.get("watchdog.pending") is not None
            receipt = {"nonce": payload["nonce"], "targets": payload["targets"], "operation": "sleep",
                       "status": "sleeping", "ok": False, "detail": "stopped"}
            result.write_text(json.dumps(receipt))
            await reconcile(rt.engine)
            assert rt.engine.lifecycle_state.last_sleep_at == 0
            receipt.update(status="completed", ok=True, detail="awakened")
            result.write_text(json.dumps(receipt))
            await asyncio.gather(reconcile(rt.engine), reconcile(rt.engine))
            assert state.get("watchdog.pending") is None
            assert rt.engine.lifecycle_state.last_sleep_at > 0
    run(main())


def test_host_submission_never_overwrites_queued_request():
    import tempfile
    from pathlib import Path
    from astrbot_plugin_guardian.core.state import StateStore
    async def main():
        with tempfile.TemporaryDirectory(dir=os.path.join(HERE, "_tmp")) as directory:
            p, _ctx = make_plugin()
            rt = p.runtime
            state = StateStore(directory)
            state.load()
            rt.state = rt.safety.state = rt.engine.state = state
            rt.config.raw["dry_run"] = False
            rt.config.raw["safety"]["allow_self_restart"] = True
            request = Path(directory) / "request.json"
            request.write_text("original queued request")
            rt.config.raw["safety"]["watchdog_request_path"] = str(request)
            rt.config.raw["lifecycle"]["sleep_targets"] = ["astrbot", "napcat"]
            rt.config.refresh()
            ok, detail = await rt.request_sleep("no clobber", 1, bypass_confirm=True)
            assert not ok and "提交失败" in detail
            assert request.read_text() == "original queued request"
            assert state.get("watchdog.pending") is None
    run(main())


def test_nonself_sleep_start_failure_is_not_success():
    from unittest.mock import patch
    from astrbot_plugin_guardian.utils.dockerclient import DockerError
    async def main():
        p, _ctx = make_plugin()
        rt = p.runtime
        rt.state.set("watchdog.pending", None)
        rt.state.set("cooldowns", {})
        rt.state.set("action_history", [])
        rt.config.raw["dry_run"] = False
        rt.config.raw["lifecycle"]["sleep_targets"] = ["napcat"]
        rt.config.refresh()
        calls = []
        async def stop(name, **kw):
            calls.append(("stop", name))
        async def start(name):
            calls.append(("start", name))
            raise DockerError("failed start")
        async def no_wait(seconds):
            pass
        rt.docker.stop, rt.docker.start = stop, start
        before = rt.engine.lifecycle_state.last_sleep_at
        with patch("astrbot_plugin_guardian.actions.sleep.asyncio.sleep", no_wait):
            ok, detail = await rt.request_sleep("failure", 1, bypass_confirm=True)
        assert not ok and "恢复失败" in detail
        assert calls == [("stop", "napcat"), ("start", "napcat")]
        assert rt.engine.lifecycle_state.last_sleep_at == before
    run(main())


def test_custom_force_level_triggers_sleep_before_deadline():
    """自定义等级 action=force_sleep 必须在硬上限之前就让引擎真的发出睡眠请求。

    修复前策略层返回的 force_sleep 恒为 False（只在 max_uptime_hours 分支置真），
    于是自定义强制等级只会注入提示词，永远不会触发睡眠——"配了却不生效"。
    """
    from astrbot_plugin_guardian.core.lifecycle import LifecycleState, build_policy
    from astrbot_plugin_guardian.core.models import ProbeResult, Severity, Status

    async def main():
        p, _ctx = make_plugin()
        rt = p.runtime
        rt.config.raw["lifecycle"]["levels"] = [
            {"id": "L0", "name": "常规", "after_hours": 0, "prompt": "", "action": "none"},
            {"id": "L1", "name": "自定义强制", "after_hours": 10,
             "prompt": "该休息了", "action": "force_sleep"},
        ]
        rt.config.refresh()
        rt.lifecycle_policy = build_policy(rt.config)
        rt.engine.lifecycle_policy = rt.lifecycle_policy
        rt.engine.lifecycle_state = LifecycleState()

        calls: list[dict] = []

        async def spy(**kw):
            calls.append(kw)
            return True, "[dry-run]"

        rt.engine.request_sleep = spy
        rt.engine._last_results = {"uptime:system": ProbeResult(
            "uptime", "system", Status.OK, Severity.INFO, "up", {"uptime_hours": 11.0})}

        await rt.engine._lifecycle_tick("interval")
        assert len(calls) == 1, f"自定义强制等级应发出睡眠请求，实际 {len(calls)} 次"
        assert calls[0]["actor"] == "lifecycle"
        assert "force_sleep" in calls[0]["reason"], calls[0]["reason"]
        assert rt.engine.lifecycle_state.forced_at > 0

        # 阈值以下（9h < 10h）不得触发
        rt.engine.lifecycle_state = LifecycleState()
        rt.engine._last_results["uptime:system"] = ProbeResult(
            "uptime", "system", Status.OK, Severity.INFO, "up", {"uptime_hours": 9.0})
        await rt.engine._lifecycle_tick("interval")
        assert len(calls) == 1, "未达阈值不得触发强制睡眠"

    run(main())


def test_failed_watchdog_publication_records_failure_not_success():
    """发布失败 = 动作从未到达宿主机：必须如实记失败，不能把目标失败连击清零。

    submit_sleep 为了"宿主机可能立刻杀掉本进程"而在发布前先记账，
    但修复前用的是 commit(spec, True)——于是发布失败也被记成成功，
    目标的 fail_streak 被静默清零，连续失败降级机制失效。
    """
    import tempfile
    from pathlib import Path
    from astrbot_plugin_guardian.core.state import StateStore

    async def main():
        with tempfile.TemporaryDirectory(dir=os.path.join(HERE, "_tmp")) as directory:
            p, _ctx = make_plugin()
            rt = p.runtime
            state = StateStore(directory)
            state.load()
            rt.state = rt.safety.state = rt.engine.state = state
            rt.config.raw["dry_run"] = False
            rt.config.raw["safety"]["allow_self_restart"] = True
            request = Path(directory) / "request.json"
            request.write_text("queued request")     # 触发 no-clobber 失败
            rt.config.raw["safety"]["watchdog_request_path"] = str(request)
            rt.config.raw["lifecycle"]["sleep_targets"] = ["astrbot", "napcat"]
            rt.config.refresh()
            state.set("fail_streak", {"astrbot": 2, "napcat": 2})

            ok, detail = await rt.request_sleep("fail-path", 1, bypass_confirm=True)
            assert not ok and "提交失败" in detail, detail
            assert state.get("watchdog.pending") is None
            assert request.read_text() == "queued request", "不得覆盖排队中的请求"
            # 失败必须登记：2 -> 3，而不是被乐观记账清零
            assert state.get("fail_streak") == {"astrbot": 3, "napcat": 3}, \
                state.get("fail_streak")
            # 一次真实动作尝试消耗一个配额，并对物理目标加冷却；且不得重复记账
            assert state.actions_in_window(1800) == 1, state.actions_in_window(1800)
            assert state.cooldown_of("astrbot") > 0 and state.cooldown_of("napcat") > 0

    run(main())


def test_cancelled_publication_keeps_pending_obligation():
    """发布期间被取消：无法确定请求是否落盘，必须保守保留待确认义务。

    这是刻意的保守行为——宁可留下一个需要人工/超时确认的 pending，
    也不能因为"没等到结果"就假定什么都没发生（可能宿主机已经接手）。
    """
    import tempfile
    from pathlib import Path
    from unittest import mock
    from astrbot_plugin_guardian.core.state import StateStore

    def slow_link(src, dst):
        time.sleep(0.5)          # 让线程真正进入"发布中"，再制造取消窗口
        raise OSError("simulated link failure")

    async def main():
        with tempfile.TemporaryDirectory(dir=os.path.join(HERE, "_tmp")) as directory:
            p, _ctx = make_plugin()
            rt = p.runtime
            state = StateStore(directory)
            state.load()
            rt.state = rt.safety.state = rt.engine.state = state
            rt.config.raw["dry_run"] = False
            rt.config.raw["safety"]["allow_self_restart"] = True
            request = Path(directory) / "request.json"
            rt.config.raw["safety"]["watchdog_request_path"] = str(request)
            rt.config.raw["lifecycle"]["sleep_targets"] = ["astrbot", "napcat"]
            rt.config.refresh()

            with mock.patch("os.link", slow_link):
                task = asyncio.create_task(
                    rt.request_sleep("cancel-path", 1, bypass_confirm=True))
                await asyncio.sleep(0.15)     # 发布线程已启动
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            assert not request.exists(), "发布未完成，请求文件不应出现"
            assert state.get("watchdog.pending") is not None, \
                "取消后必须保守保留待确认义务，不能假定什么都没发生"
            assert state.get("watchdog.pending")["targets"] == ["astrbot", "napcat"]

    run(main())


def test_log_facade_forwards_to_astrbot_when_present():
    """在 AstrBot 内运行时，日志门面必须转发给 astrbot.api.logger（行为不变）。"""
    from astrbot_plugin_guardian.core.log import in_astrbot, logger
    import astrbot.api as host_api
    assert in_astrbot is True, "桩环境下门面应识别到 astrbot"
    assert logger is host_api.logger, "门面必须转发到宿主 logger，而不是自建一个"


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
