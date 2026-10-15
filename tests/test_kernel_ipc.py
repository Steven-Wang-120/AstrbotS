"""Guardian 守护系统 —— 内核 IPC 契约自检（B16 §10 / §13 K5/K7/K11/K12/K13）。

对应文档：B16 §6（IPC 与鉴权）、§10（接口契约）、§12.3.5（D4-a+ 阶梯）

本文件的定位：**M2 的验收判据**。对 §10 的每个 method 做五类断言
（请求 / 响应 / 错误 / 超时 / 断连），并专门验证两件最容易做假的事：

1. **断连必须显式失败**（I4）：内核不可用时工具绝不能"看起来成功"；
2. **自修复必须真的有界**（C1–C4）：不能退化成崩溃循环。

还有一条本测试独有的断言：`LocalBackend` 与 `IpcBackend` 对同一 method
必须给出**同一结果**——这是"inproc → ipc 只是换传输、不换逻辑"的可证伪证据。

直接运行：`python tests/test_kernel_ipc.py`
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from astrbot_plugin_guardian.core.config import build_config  # noqa: E402
from astrbot_plugin_guardian.ipc import backend as backend_mod  # noqa: E402
from astrbot_plugin_guardian.ipc import client as client_mod  # noqa: E402
from astrbot_plugin_guardian.ipc import methods as methods_mod  # noqa: E402
from astrbot_plugin_guardian.ipc import protocol as proto  # noqa: E402
from astrbot_plugin_guardian.ipc import server as server_mod  # noqa: E402
from astrbot_plugin_guardian.kernel_runtime import GuardianKernel  # noqa: E402


# =========================================================================== #
# 脚手架
# =========================================================================== #
class FakeDocker:
    """只读替身：daemon 正常、两个容器 running。"""

    async def negotiate(self):
        return "v1.44"

    async def ping(self):
        return True

    async def info(self):
        return {"Containers": 2, "ContainersRunning": 2}

    async def inspect(self, name):
        return {"State": {"Running": True, "RestartCount": 0, "OOMKilled": False,
                          "StartedAt": "2026-10-14T10:00:00.000000000Z",
                          "Status": "running"}}

    async def stats(self, name):
        return {"usage": 300 * 1024 * 1024, "limit": 4 * 1024 ** 3,
                "stats": {"inactive_file": 10 * 1024 * 1024}}

    async def close(self):
        return None


def make_kernel(tmp: str, **overrides) -> GuardianKernel:
    raw = {"containers": {"managed": ["astrbot", "napcat"]},
           "dry_run": True,
           "notify": {"umo_list": []}}
    raw.update(overrides)
    kernel = GuardianKernel(raw, data_dir=tmp)
    kernel.deps["docker"] = FakeDocker()
    kernel.engine.deps["docker"] = kernel.deps["docker"]
    return kernel


class Harness:
    """一个临时目录 + 一个内核 + 一个 TCP 服务端 + 一个已连接的客户端。"""

    def __init__(self, tmp: str, *, request_timeout: float = 3.0,
                 start_engine: bool = True) -> None:
        self.tmp = tmp
        self.kernel = make_kernel(tmp)
        self.start_engine = start_engine
        self.request_timeout = request_timeout
        self.server: server_mod.KernelIpcServer | None = None
        self.client: client_mod.KernelIpcClient | None = None

    async def __aenter__(self) -> "Harness":
        if self.start_engine:
            await self.kernel.engine.start()
        self.server = server_mod.KernelIpcServer(
            self.kernel, transport="tcp", tcp_host="127.0.0.1", tcp_port=0,
            shared_dir=self.tmp, heartbeat_interval=999)
        await self.server.start()
        self.client = client_mod.KernelIpcClient(
            transport="tcp", tcp_host="127.0.0.1",
            tcp_port=self.server.bound_port, shared_dir=self.tmp,
            request_timeout=self.request_timeout,
            reload_grace_sec=0.2, reload_reset_sec=0.3)
        await self.client.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        if self.client is not None:
            await self.client.close()
        if self.server is not None:
            await self.server.stop()


def _fast_backoff():
    """把 Stage 1 退避压到毫秒级，否则每个阶梯用例要跑 15s。"""
    original = client_mod.RECONNECT_BACKOFF
    client_mod.RECONNECT_BACKOFF = (0.01, 0.01, 0.01, 0.01, 0.01)
    return original


# =========================================================================== #
# 1. 方法表完整性
# =========================================================================== #
def test_method_table_is_complete():
    kernel = make_kernel(tempfile.mkdtemp())
    handlers = methods_mod.build_handlers(kernel)
    missing = methods_mod.missing_methods(handlers)
    extra = methods_mod.extra_methods(handlers)
    assert not missing, f"方法表缺少 B16 §10 要求的 method: {missing}"
    assert not extra, f"方法表出现了未登记到 METHOD_NAMES 的 method: {extra}"
    # §10 表里点名的 method 必须都在
    for name in ("health_report", "lifecycle_status", "run_check", "diagnose",
                 "list_incidents", "request_sleep", "restart_container",
                 "set_mode", "set_profile", "level_prompt"):
        assert name in handlers, f"§10 契约要求的 {name} 缺失"


def test_no_retry_set_covers_all_mutating_methods():
    """破坏性动作清单只能有一处真源，否则会漂移出"静默重试"。"""
    assert client_mod.NO_RETRY_METHODS == methods_mod.MUTATING_METHODS
    for name in ("request_sleep", "restart_container", "run_check"):
        assert name in client_mod.NO_RETRY_METHODS, name


# =========================================================================== #
# 2. 请求 / 响应（含两后端一致性）
# =========================================================================== #
READONLY_CALLS = [
    ("ping", {}),
    ("status_text", {}),
    ("lifecycle_text", {}),
    ("lifecycle_status", {}),
    ("health_report", {"scope": "all"}),
    ("diagnose", {"target": "all"}),
    ("list_incidents", {"limit": 5}),
    ("allowlist", {}),
    ("safety_warnings", {}),
    ("jobs_text", {}),
    ("level_prompt", {}),
    ("sleep_armed", {}),
    ("runtime_flags", {}),
    ("status_context", {}),
    ("injection", {}),
]


def test_all_readonly_methods_respond_over_ipc():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                for method, params in READONLY_CALLS:
                    result = await h.client.call(method, params, timeout=5)
                    assert result is not None or method in ("level_prompt",), \
                        f"{method} 返回了 None"
    asyncio.run(main())


def test_local_and_ipc_backends_agree():
    """同表双后端一致性——inproc 与 ipc 的差异只能来自传输。"""
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                local = backend_mod.LocalBackend(h.kernel)
                await local.start()
                for method, params in READONLY_CALLS:
                    if method == "ping":
                        continue          # 含 pid/ts，天然不同
                    a = await local.call(method, params)
                    b = await h.client.call(method, params, timeout=5)
                    assert a == b, (
                        f"{method} 在两个后端结果不一致：\n  inproc={a!r}\n  ipc={b!r}")
    asyncio.run(main())


def test_mutating_methods_roundtrip_shape():
    """有副作用 method 的返回必须是 {ok, detail}（适配层据此还原元组）。"""
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                for method, params in (
                    ("set_mode", {"mode": "server"}),
                    ("set_profile", {"profile": "normal"}),
                    ("restart_container", {"container": "napcat"}),
                    ("request_sleep", {"reason": "测试", "duration_min": 1}),
                ):
                    result = await h.client.call(method, params, timeout=5)
                    assert isinstance(result, dict), (method, result)
                    assert set(result) == {"ok", "detail"}, (method, result)
                    assert isinstance(result["ok"], bool), (method, result)
                # dry_run=true 时重启不应真的执行，但回执必须明确
                allow = await h.client.call("allowlist", {})
                assert "napcat" in allow
    asyncio.run(main())


def test_run_check_returns_jsonable_structure():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                result = await h.client.call("run_check", {"scope": "all"}, timeout=20)
                assert isinstance(result, dict)
                assert result.get("skipped") is False, result
                assert result["results"], "巡检没有产出任何结果"
                # 结果必须真的能过 JSON（协议层不允许带回不可序列化对象）
                json.dumps(result, ensure_ascii=False)
    asyncio.run(main())


# =========================================================================== #
# 3. 错误
# =========================================================================== #
def test_unknown_method_is_rejected_explicitly():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                try:
                    await h.client.call("no_such_method", {}, timeout=5)
                except proto.IpcError as exc:
                    assert exc.code == proto.E_UNKNOWN_METHOD, exc.code
                else:
                    raise AssertionError("未知 method 必须报错，不能静默成功")
    asyncio.run(main())


def test_bad_params_return_bad_request():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                # restart_container 缺 container
                try:
                    await h.client.call("restart_container", {}, timeout=5)
                except proto.IpcError as exc:
                    assert exc.code == proto.E_BAD_REQUEST, exc.code
                else:
                    raise AssertionError("缺少必填参数必须报 bad_request")
                # params 不是对象
                try:
                    await h.client.call("ping", ["not", "a", "dict"], timeout=5)
                except proto.IpcError as exc:
                    assert exc.code == proto.E_BAD_REQUEST, exc.code
                else:
                    raise AssertionError("params 非对象必须报 bad_request")
    asyncio.run(main())


def test_illegal_params_are_clamped_not_crashed():
    """LLM/JSON 会送 inf/NaN/字符串；内核必须夹取而不是抛异常击穿。"""
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                for bad in (float("inf"), float("nan"), "abc", None, -5):
                    result = await h.client.call(
                        "request_sleep", {"reason": "x", "duration_min": bad},
                        timeout=5)
                    assert set(result) == {"ok", "detail"}, (bad, result)
                result = await h.client.call("list_incidents", {"limit": float("inf")},
                                             timeout=5)
                assert isinstance(result, str)
    asyncio.run(main())


def test_tcp_requires_valid_token():
    """TCP 模式必须鉴权；token 不对一律 unauthorized，绝不"默认放行"。"""
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                # 正常路径能通
                await h.client.call("ping", {}, timeout=5)
                # 篡改 token 文件后必须被拒
                token_path = Path(h.client.token_file)
                token_path.write_text("wrong-token", encoding="utf-8")
                try:
                    await h.client.call("ping", {}, timeout=5)
                except proto.IpcError as exc:
                    assert exc.code == proto.E_UNAUTHORIZED, exc.code
                else:
                    raise AssertionError("错误 token 必须被拒绝")
    asyncio.run(main())


def test_socket_never_binds_non_loopback():
    """安全红线：内核绝不监听 0.0.0.0（B16 §11-2）。"""
    cfg = build_config({"kernel": {"tcp_host": "0.0.0.0"}}, data_dir=tempfile.mkdtemp())
    assert cfg.kernel.tcp_host == "127.0.0.1", cfg.kernel.tcp_host
    assert any("非 loopback" in i for i in cfg.issues), cfg.issues


# =========================================================================== #
# 4. 超时
# =========================================================================== #
def test_slow_handler_maps_to_kernel_timeout():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, request_timeout=0.2) as h:
                async def slow(_params):
                    await asyncio.sleep(1.0)
                    return "late"

                h.server._handlers["ping"] = slow
                try:
                    await h.client.call("ping", {}, timeout=0.2)
                except proto.IpcError as exc:
                    assert exc.code == proto.E_KERNEL_TIMEOUT, exc.code
                    assert exc.self_healable is True
                    assert "last_heartbeat" in exc.diagnostics
                else:
                    raise AssertionError("超时必须报 kernel_timeout")
    asyncio.run(main())


# =========================================================================== #
# 5. 断连（I4：失败不静默）
# =========================================================================== #
def test_disconnect_returns_explicit_error():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.client.call("ping", {}, timeout=5)
                await h.server.stop()
                await asyncio.sleep(0.3)      # 让读循环观察到 EOF
                assert not h.client.connected
                try:
                    await h.client.call("ping", {}, timeout=5)
                except proto.IpcError as exc:
                    assert exc.code == proto.E_KERNEL_UNAVAILABLE, exc.code
                    assert exc.self_healable is True
                    d = exc.diagnostics
                    assert "endpoint" in d and "last_heartbeat" in d, d
                else:
                    raise AssertionError("断连必须显式报错，不能静默成功")
                h.server = None               # 已停，避免重复 stop
    asyncio.run(main())


def test_never_connected_client_reports_unavailable_with_diagnostics():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            client = client_mod.KernelIpcClient(
                transport="tcp", tcp_host="127.0.0.1", tcp_port=1,
                shared_dir=tmp, request_timeout=1)
            try:
                await client.connect()
            except proto.IpcError as exc:
                assert exc.code in (proto.E_KERNEL_UNAVAILABLE,
                                    proto.E_KERNEL_TIMEOUT), exc.code
                assert exc.diagnostics.get("endpoint"), exc.diagnostics
            else:
                raise AssertionError("连不上的端口必须报错")
    asyncio.run(main())


def test_adapter_events_stream_is_written_on_disconnect():
    """L3：断连事件必须落到适配层自己的 adapter_events.jsonl（内核写不了）。"""
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.client.call("ping", {}, timeout=5)
                await h.server.stop()
                await asyncio.sleep(0.3)
                path = Path(h.client.adapter_events_path)
                assert path.exists(), "未生成 adapter_events.jsonl"
                kinds = [json.loads(x)["kind"]
                         for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
                assert "ipc_disconnected" in kinds, kinds
                h.server = None
    asyncio.run(main())


# =========================================================================== #
# 6. D4-a+ 阶梯有界性（C1–C4）
# =========================================================================== #
def _ladder_client(tmp: str, **kw) -> client_mod.KernelIpcClient:
    defaults = dict(transport="tcp", tcp_host="127.0.0.1", tcp_port=1,
                    shared_dir=tmp, request_timeout=0.2,
                    max_auto_reloads=1, reload_cooldown=600,
                    reload_grace_sec=0.05, reload_reset_sec=0.1)
    defaults.update(kw)
    return client_mod.KernelIpcClient(**defaults)


def test_self_heal_ladder_is_bounded():
    """C1 + C3：额度用尽后不再自动重载，直接进入 kernel_degraded。"""
    async def main():
        original = _fast_backoff()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = _ladder_client(tmp)
                try:
                    await client.call_with_recovery("ping", {})
                except proto.IpcError as exc:
                    assert exc.code == proto.E_KERNEL_DEGRADED, exc.code
                    assert exc.self_healable is False, "降级后不得再宣称可自愈"
                else:
                    raise AssertionError("连不上内核必须显式失败")

                req = Path(client.reload_request_path)
                assert req.exists(), "Stage 2 应写出 kernel_reload_request.json"
                payload = json.loads(req.read_text(encoding="utf-8"))
                assert payload["attempt"] == 1, payload
                assert payload["nonce"], payload

                # 第二次故障：C1 已用尽 + C2 冷却中 → 不得再写请求
                before = payload["nonce"]
                try:
                    await client.call_with_recovery("ping", {})
                except proto.IpcError:
                    pass
                after = json.loads(req.read_text(encoding="utf-8"))["nonce"]
                assert after == before, "额度已用尽，不得再写重载请求"
                assert client.health.attempt == 1, client.health.as_dict()
                assert client.health.degraded is True
        finally:
            client_mod.RECONNECT_BACKOFF = original
    asyncio.run(main())


def test_reload_cooldown_blocks_second_attempt():
    """C2：冷却未过时即便还有额度也不能重载。"""
    health = client_mod.KernelHealth(os.path.join(tempfile.mkdtemp(), "h.json"))
    ok, _ = health.can_reload(max_auto_reloads=5, cooldown=600)
    assert ok is True
    health.note_reload()
    ok, reason = health.can_reload(max_auto_reloads=5, cooldown=600)
    assert ok is False and "冷却" in reason, reason


def test_max_auto_reloads_zero_disables_self_heal():
    """C1 边界：额度为 0 时一次都不该重载。"""
    async def main():
        original = _fast_backoff()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = _ladder_client(tmp, max_auto_reloads=0)
                try:
                    await client.call_with_recovery("ping", {})
                except proto.IpcError as exc:
                    assert exc.code == proto.E_KERNEL_DEGRADED, exc.code
                assert not Path(client.reload_request_path).exists(), \
                    "max_auto_reloads=0 时不得写出重载请求"
        finally:
            client_mod.RECONNECT_BACKOFF = original
    asyncio.run(main())


def test_quota_resets_after_stable_window():
    """C4：稳定满窗口后额度自动清零，否则"一次"会变成永久一次性。"""
    tmp = tempfile.mkdtemp()
    health = client_mod.KernelHealth(os.path.join(tmp, "h.json"))
    health.note_reload(now=100.0)
    assert health.attempt == 1
    # 刚连上：只记稳定起点，不复位
    assert health.note_connected(reset_sec=300, now=110.0) is False
    assert health.attempt == 1
    # 稳定不满窗口：仍不复位
    assert health.note_connected(reset_sec=300, now=200.0) is False
    assert health.attempt == 1
    # 稳定满窗口：复位
    assert health.note_connected(reset_sec=300, now=420.0) is True
    assert health.attempt == 0
    assert health.degraded is False


def test_manual_reset_clears_quota_and_degradation():
    tmp = tempfile.mkdtemp()
    health = client_mod.KernelHealth(os.path.join(tmp, "h.json"))
    health.note_reload()
    health.mark_degraded()
    assert health.degraded is True
    health.reset()
    assert health.attempt == 0 and health.degraded is False


def test_kernel_health_file_has_only_whitelisted_fields():
    """I2 修正版：适配层只能写这一个受限计数文件，字段固定。"""
    tmp = tempfile.mkdtemp()
    health = client_mod.KernelHealth(os.path.join(tmp, "h.json"))
    health.note_reload()
    raw = json.loads(Path(health.path).read_text(encoding="utf-8"))
    assert set(raw) == set(client_mod.KernelHealth.FIELDS), raw


def test_health_file_is_reloaded_across_restart():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "h.json")
    a = client_mod.KernelHealth(path)
    a.note_reload()
    a.mark_degraded()
    b = client_mod.KernelHealth(path)
    assert b.attempt == 1 and b.degraded is True


def test_mutating_method_is_never_auto_retried():
    """§6.4：超时后不得重试破坏性动作（避免重复执行）。"""
    async def main():
        original = _fast_backoff()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                # 起一个真服务端，但让 request_sleep 超时
                kernel = make_kernel(tmp)
                await kernel.engine.start()
                srv = server_mod.KernelIpcServer(
                    kernel, transport="tcp", tcp_host="127.0.0.1", tcp_port=0,
                    shared_dir=tmp, heartbeat_interval=999)
                await srv.start()
                client = client_mod.KernelIpcClient(
                    transport="tcp", tcp_host="127.0.0.1", tcp_port=srv.bound_port,
                    shared_dir=tmp, request_timeout=0.2)
                await client.connect()
                calls = {"n": 0}

                async def slow(_params):
                    calls["n"] += 1
                    await asyncio.sleep(1.0)
                    return {"ok": True, "detail": "should not be retried"}

                srv._handlers["request_sleep"] = slow
                try:
                    await client.call_with_recovery(
                        "request_sleep", {"reason": "x", "duration_min": 1})
                except proto.IpcError as exc:
                    assert exc.code in (proto.E_KERNEL_TIMEOUT,
                                        proto.E_KERNEL_DEGRADED), exc.code
                assert calls["n"] == 1, f"破坏性动作被重试了 {calls['n']} 次"
                await client.close()
                await srv.stop()
        finally:
            client_mod.RECONNECT_BACKOFF = original
    asyncio.run(main())


def test_readonly_method_is_retried_after_silent_reconnect():
    """只读 method 在 Stage 1 静默重连成功后应重试一次（用户无感恢复）。"""
    async def main():
        original = _fast_backoff()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                async with Harness(tmp) as h:
                    await h.client.call("ping", {}, timeout=5)
                    # 强制断开传输层，但保留服务端
                    await h.client._teardown_transport()
                    h.client._connected = False
                    result = await h.client.call_with_recovery("status_text", {})
                    assert isinstance(result, str) and result
        finally:
            client_mod.RECONNECT_BACKOFF = original
    asyncio.run(main())


# =========================================================================== #
# 7. 心跳 / 事件推送
# =========================================================================== #
def test_heartbeat_file_is_written_on_start():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                path = Path(h.server.heartbeat_path)
                assert path.exists(), "内核启动必须写 kernel_heartbeat.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                for key in ("ts", "pid", "transport", "endpoint"):
                    assert key in payload, payload
                assert abs(time.time() - payload["ts"]) < 30, payload
                assert h.client.last_heartbeat() > 0
    asyncio.run(main())


def test_notify_event_is_pushed_to_connected_client():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                got: list[tuple[str, dict]] = []
                h.client.on_event(lambda m, p: got.append((m, p)))
                await h.server._kernel_sender("umo-1", "内核告警文本")
                await asyncio.sleep(0.2)
                assert got, "内核通知必须作为 IPC 事件推给适配层"
                method, params = got[0]
                assert method == proto.EV_NOTIFY, method
                assert params["umo"] == "umo-1"
                assert params["text"] == "内核告警文本"
    asyncio.run(main())


def test_kernel_ready_event_is_broadcast_on_start():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            kernel = make_kernel(tmp)
            await kernel.engine.start()
            srv = server_mod.KernelIpcServer(
                kernel, transport="tcp", tcp_host="127.0.0.1", tcp_port=0,
                shared_dir=tmp, heartbeat_interval=999)
            await srv.start()
            client = client_mod.KernelIpcClient(
                transport="tcp", tcp_host="127.0.0.1", tcp_port=srv.bound_port,
                shared_dir=tmp, request_timeout=2)
            got: list[str] = []
            client.on_event(lambda m, p: got.append(m))
            await client.connect()
            sent = await srv.broadcast_event(proto.EV_KERNEL_READY,
                                            {"version": "test"})
            await asyncio.sleep(0.2)
            assert sent == 1, sent
            assert proto.EV_KERNEL_READY in got, got
            await client.close()
            await srv.stop()
    asyncio.run(main())


def test_duplicate_unix_socket_is_refused():
    """陈旧 socket 要能清掉，但**活着**的 socket 必须拒绝重复绑定。"""
    if not server_mod.unix_socket_supported():
        return   # Windows：TCP 回退路径，本用例不适用
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            kernel = make_kernel(tmp)
            sock = os.path.join(tmp, "guardian", "kernel.sock")
            a = server_mod.KernelIpcServer(kernel, transport="unix",
                                           socket_path=sock, shared_dir=tmp,
                                           heartbeat_interval=999)
            await a.start()
            b = server_mod.KernelIpcServer(kernel, transport="unix",
                                           socket_path=sock, shared_dir=tmp,
                                           heartbeat_interval=999)
            try:
                await b.start()
            except proto.IpcError as exc:
                assert "拒绝重复启动" in exc.message, exc.message
            else:
                raise AssertionError("已有实例在监听时必须拒绝重复启动")
            finally:
                await a.stop()
    asyncio.run(main())


# =========================================================================== #
# 8. 配置校验
# =========================================================================== #
def test_kernel_config_defaults_are_valid():
    cfg = build_config({}, data_dir=tempfile.mkdtemp())
    assert cfg.kernel.mode == "inproc"
    assert cfg.kernel.transport in ("unix", "tcp")
    assert cfg.kernel.max_auto_reloads == 1
    assert cfg.kernel.reload_cooldown == 600
    assert cfg.kernel.reload_reset_sec == 300
    # 默认配置本身可能因"白名单为空"产生 issue（这是既有的、正确的告警），
    # 但 kernel 段不得产生任何 issue —— 默认值必须是自洽的。
    kernel_issues = [i for i in cfg.issues if "kernel." in i]
    assert not kernel_issues, kernel_issues


def test_kernel_config_illegal_values_are_normalized():
    cfg = build_config({
        "kernel": {"mode": "bogus", "transport": "carrier-pigeon",
                   "tcp_host": "0.0.0.0", "tcp_port": 99999,
                   "request_timeout": float("inf"), "max_auto_reloads": 999,
                   "heartbeat_interval": 100, "heartbeat_timeout": 30},
    }, data_dir=tempfile.mkdtemp())
    assert cfg.kernel.mode == "inproc"
    assert cfg.kernel.transport in ("unix", "tcp")
    assert cfg.kernel.tcp_host == "127.0.0.1"
    assert 1 <= int(cfg.kernel.tcp_port) <= 65535
    assert int(cfg.kernel.request_timeout) >= 1
    assert int(cfg.kernel.max_auto_reloads) <= 10
    assert int(cfg.kernel.heartbeat_timeout) > int(cfg.kernel.heartbeat_interval)
    assert cfg.issues, "非法配置必须产生可上报的 issues"


def test_kernel_config_issues_are_surfaced_by_self_check():
    """归一化 issues 必须能被运维看见（否则等于静默改写）。"""
    from astrbot_plugin_guardian.core.audit import AuditLog
    from astrbot_plugin_guardian.core.safety import SafetyGuard
    from astrbot_plugin_guardian.core.state import StateStore

    tmp = tempfile.mkdtemp()
    cfg = build_config({"kernel": {"tcp_host": "0.0.0.0"}}, data_dir=tmp)
    state = StateStore(tmp)
    state.load()
    warns = SafetyGuard(cfg, state, AuditLog(tmp)).self_check()
    assert any("loopback" in w for w in warns), warns


def test_ipc_mode_with_zero_autoreload_is_flagged():
    cfg = build_config({"kernel": {"mode": "ipc", "max_auto_reloads": 0}},
                       data_dir=tempfile.mkdtemp())
    assert any("max_auto_reloads=0" in i for i in cfg.issues), cfg.issues


def test_sidecar_mode_alias_normalizes_to_ipc():
    """B00 §7 写 `sidecar`、B16 §9 写 `ipc`，指的是同一件事；两个值都必须能填。"""
    cfg = build_config({"kernel": {"mode": "sidecar"}}, data_dir=tempfile.mkdtemp())
    assert cfg.kernel.mode == "ipc", cfg.kernel.mode
    assert not [i for i in cfg.issues if "kernel.mode" in i], cfg.issues


# =========================================================================== #
# 9. 适配层边界（I2）
# =========================================================================== #
def test_adapter_in_ipc_mode_blocks_kernel_state_access():
    """ipc 模式下适配层**不得**能直接读内核业务状态（I2），且报错要可读。"""
    class FakeCtx:
        async def send_message(self, umo, chain):
            return None

    from astrbot_plugin_guardian.adapter_runtime import GuardianAdapter

    with tempfile.TemporaryDirectory() as tmp:
        adapter = GuardianAdapter.__new__(GuardianAdapter)   # 绕过 astrbot 数据目录解析
        adapter.context = FakeCtx()
        adapter.raw_config = {}
        adapter.data_dir = tmp
        adapter.kernel_mode = "ipc"
        adapter.kernel = None
        adapter._cfg_view = build_config({}, data_dir=tmp)
        adapter.shared_dir = tmp
        adapter._outage_task = None
        adapter._last_note_ts = 0.0
        adapter._backend = backend_mod.LocalBackend(make_kernel(tmp))
        try:
            adapter.state
        except AttributeError as exc:
            assert "ipc" in str(exc) and "I2" in str(exc), str(exc)
        else:
            raise AssertionError("ipc 模式下访问内核状态必须报错")
        try:
            adapter.engine
        except AttributeError as exc:
            assert "I2" in str(exc), str(exc)
        else:
            raise AssertionError("ipc 模式下访问 engine 必须报错")


def test_note_event_is_coalesced():
    """每条用户消息都发一次 IPC 太贵；必须合并到窗口内一次。"""
    async def main():
        class Ctx:
            async def send_message(self, umo, chain):
                return None

        from astrbot_plugin_guardian.adapter_runtime import GuardianAdapter

        with tempfile.TemporaryDirectory() as tmp:
            adapter = GuardianAdapter.__new__(GuardianAdapter)
            adapter.context = Ctx()
            adapter.raw_config = {}
            adapter.data_dir = tmp
            adapter.kernel_mode = "inproc"
            adapter._cfg_view = build_config({}, data_dir=tmp)
            adapter.shared_dir = tmp
            adapter._outage_task = None
            adapter._last_note_ts = 0.0
            kernel = make_kernel(tmp)
            adapter.kernel = kernel
            adapter._backend = backend_mod.LocalBackend(kernel)

            calls = {"n": 0}
            original = kernel.note_event

            def counting_note_event():
                calls["n"] += 1
                original()

            kernel.note_event = counting_note_event
            for _ in range(20):
                await adapter.note_event()
            assert calls["n"] == 1, f"20 次调用应合并为 1 次，实际 {calls['n']}"
            # 窗口外必须再发一次（否则"最近活跃时间"会长期不更新）
            adapter._last_note_ts = time.time() - 100
            await adapter.note_event()
            assert calls["n"] == 2, calls["n"]
    asyncio.run(main())


# =========================================================================== #
# 9. M3 验收：独立进程真的能起来、能心跳、能被 IPC 调用（B16 §13 K1）
# =========================================================================== #
def test_independent_kernel_process_serves_ipc_and_heartbeat():
    """**这是 M3 的验收判据**：内核必须是"另一个进程"，不是同进程的伪装。

    做法：把 `python -m astrbot_plugin_guardian.kernel` 当**子进程**拉起来，
    然后用一个**独立客户端**连它的 IPC，断言：
      ① 进程还活着且没退出（独立存活）；
      ② `kernel_heartbeat.json` 被写出（域 C 看门狗据此判活）；
      ③ `ping` / `status_text` 能拿到响应（IPC 服务端真在跑）。
    """
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
        env["GUARDIAN_CONFIG_JSON"] = json.dumps({
            "containers": {"managed": ["astrbot", "napcat"]},
            "dry_run": True,
            "check_interval": 3600,
            "kernel": {"mode": "ipc", "transport": "tcp", "tcp_host": "127.0.0.1",
                       "tcp_port": 0, "shared_dir": tmp,
                       "heartbeat_interval": 5},
        })
        proc = subprocess.Popen(
            [sys.executable, "-m", "astrbot_plugin_guardian.kernel"],
            env=env, cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            hb = Path(tmp) / "guardian" / "kernel_heartbeat.json"
            deadline = time.time() + 30
            payload = None
            while time.time() < deadline:
                if proc.poll() is not None:
                    out = (proc.stdout.read() or b"").decode("utf-8", "ignore")
                    raise AssertionError(
                        f"独立内核进程提前退出（rc={proc.returncode}）：\n{out[-2000:]}")
                if hb.exists():
                    try:
                        payload = json.loads(hb.read_text(encoding="utf-8"))
                        break
                    except (json.JSONDecodeError, OSError):
                        pass
                time.sleep(0.2)
            assert payload, "30s 内没有写出 kernel_heartbeat.json"
            assert payload["transport"] == "tcp"
            port = int(payload["tcp_port"])
            assert port > 0, payload
            assert proc.poll() is None, "独立内核进程不应自己退出"

            async def probe() -> tuple[dict, str]:
                client = client_mod.KernelIpcClient(
                    transport="tcp", tcp_host="127.0.0.1", tcp_port=port,
                    shared_dir=tmp, request_timeout=10)
                await client.connect()
                try:
                    return (await client.call("ping", {}),
                            await client.call("status_text", {}))
                finally:
                    await client.close()

            pong, status = asyncio.run(probe())
            assert pong.get("pid") == payload["pid"], (pong, payload)
            assert pong["pid"] != os.getpid(), "内核必须跑在另一个进程里"
            assert isinstance(status, str) and status
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
                proc.wait(timeout=5)


def test_kernel_service_selftest_exits_zero():
    """`--selftest` 是部署期的冒烟开关，必须能独立跑通并返回 0。"""
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
        env["GUARDIAN_CONFIG_JSON"] = json.dumps({
            "containers": {"managed": ["astrbot", "napcat"]},
            "dry_run": True,
            "kernel": {"mode": "ipc", "transport": "tcp", "tcp_port": 0,
                       "shared_dir": tmp},
        })
        proc = subprocess.run(
            [sys.executable, "-m", "astrbot_plugin_guardian.kernel", "--selftest"],
            env=env, cwd=ROOT, capture_output=True, timeout=120)
        out = (proc.stdout + proc.stderr).decode("utf-8", "ignore")
        assert proc.returncode == 0, f"rc={proc.returncode}\n{out[-2000:]}"


def test_kernel_print_config_reports_normalization_issues():
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["GUARDIAN_CONFIG_JSON"] = json.dumps({"kernel": {"tcp_host": "0.0.0.0"}})
    proc = subprocess.run(
        [sys.executable, "-m", "astrbot_plugin_guardian.kernel", "--print-config"],
        env=env, cwd=ROOT, capture_output=True, timeout=60)
    out = (proc.stdout + proc.stderr).decode("utf-8", "ignore")
    assert proc.returncode == 0, out[-1000:]
    assert "非 loopback" in out, out[-1000:]


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
