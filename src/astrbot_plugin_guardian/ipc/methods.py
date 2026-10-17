"""Guardian 守护系统 —— 内核方法分发表（B16 §10）。

**这张表是内核对外能力的唯一定义处。** 两条消费路径共用它：

- `LocalBackend`（`kernel.mode=inproc`）：直接在当前进程里调用内核；
- `KernelIpcServer`（`kernel.mode=ipc`）：把同一张表暴露到 Unix socket / TCP。

共用同一张表是刻意的：这样 inproc 与 ipc 的行为差异**只可能来自传输层**，
不可能来自"两条实现各自漂移"。契约测试 `tests/test_kernel_ipc.py` 也正是靠
这一点，对同一个 method 在两种后端上断言同一结果。

约束：本模块不得 `import astrbot`（I6），且**不得**做任何安全判定——
安全判定在 `SafetyGuard` / `Engine` 里，这里只做参数形状校验。
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import (
    E_BAD_REQUEST,
    E_INTERNAL,
    E_UNKNOWN_METHOD,
    IpcError,
)

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


# --------------------------------------------------------------------------- #
# 参数归一化（与 tools/llm_tools.safe_int 同语义：LLM/JSON 会送 inf/NaN/字符串）
# --------------------------------------------------------------------------- #
def _as_int(value: Any, default: int, *, low: int, high: int) -> int:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(low, min(high, int(parsed)))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _as_str(value: Any, default: str = "", *, limit: int = 200) -> str:
    if value is None:
        return default
    return str(value).strip()[:limit]


def _pair(result: Any) -> dict[str, Any]:
    """把内核的 `(ok, detail)` 元组统一成 JSON 对象。

    元组过不了 JSON；而 `[true, "…"]` 这种位置数组又容易在两端各自漂移。
    统一成 `{"ok":…, "detail":…}` 让契约显式。
    """
    if isinstance(result, tuple) and len(result) == 2:
        return {"ok": bool(result[0]), "detail": str(result[1])}
    if isinstance(result, dict):
        return result
    return {"ok": bool(result), "detail": ""}


def _jsonable(value: Any) -> Any:
    """把结果压成 JSON 安全结构（探针结果里可能有 Decimal/Path/自定义枚举）。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


# --------------------------------------------------------------------------- #
# 分发表
# --------------------------------------------------------------------------- #
def build_handlers(kernel: Any) -> dict[str, Handler]:
    """由内核实例构建 method → handler 表。"""

    # ---- 只读 ---------------------------------------------------------- #
    async def h_health_report(p: dict) -> str:
        return str(kernel.health_report(_as_str(p.get("scope") or "all", "all",
                                                limit=32) or "all"))

    async def h_lifecycle_status(p: dict) -> str:
        return str(kernel.lifecycle_status_text())

    async def h_status_text(p: dict) -> str:
        return str(kernel.status_text())

    async def h_lifecycle_text(p: dict) -> str:
        return str(kernel.lifecycle_text())

    async def h_jobs_text(p: dict) -> str:
        return str(kernel.jobs_text())

    async def h_safety_warnings(p: dict) -> list[str]:
        return [str(x) for x in kernel.safety_warnings()]

    async def h_allowlist(p: dict) -> list[str]:
        return [str(x) for x in kernel.allowlist()]

    async def h_diagnose(p: dict) -> str:
        return str(kernel.diagnose(_as_str(p.get("target") or "all", "all") or "all"))

    async def h_list_incidents(p: dict) -> str:
        return str(kernel.incidents_text(
            _as_int(p.get("limit"), 10, low=1, high=50)))

    async def h_level_prompt(p: dict) -> str:
        """当前等级提示词文本（`on_llm_request` 注入源）。"""
        return str(kernel.level_prompt())

    async def h_injection(p: dict) -> dict:
        """适配层注入所需的全部素材（等级块 + 模式 + 状态上下文）。

        刻意做成**一次调用**：注入发生在每次 LLM 请求上，是最热路径；
        分成 4 次 IPC 往返会让每次对话都多付 3 次往返的延迟。
        """
        return _jsonable(kernel.llm_injection())

    async def h_status_context(p: dict) -> str:
        return str(kernel.status_context())

    async def h_runtime_flags(p: dict) -> dict:
        """适配层做 L-D 二次确认所需的权威开关（**必须来自内核**）。

        适配层不能拿自己那份配置副本判断 `dry_run`：两份配置一旦漂移，
        适配层会在"自己以为 dry-run"时传 `bypass_confirm=True`，
        而内核其实已经关掉 dry-run —— 那就是一次无确认的真睡眠。
        """
        return _jsonable(kernel.runtime_flags())

    async def h_sleep_armed(p: dict) -> bool:
        return bool(kernel.sleep_armed())

    # ---- 有副作用 ------------------------------------------------------ #
    async def h_run_check(p: dict) -> dict:
        return _jsonable(await kernel.run_check(
            _as_str(p.get("scope") or "all", "all", limit=32) or "all"))

    async def h_request_sleep(p: dict) -> dict:
        reason = _as_str(p.get("reason"), "Bot 自主请求休息", limit=200)
        duration = _as_int(p.get("duration_min"), 0, low=0, high=120)
        return _pair(await kernel.request_sleep(
            reason, duration,
            actor=_as_str(p.get("actor"), "llm", limit=32) or "llm",
            trigger=_as_str(p.get("trigger"), "llm", limit=32) or "llm",
            bypass_confirm=_as_bool(p.get("bypass_confirm"), False)))

    async def h_restart_container(p: dict) -> dict:
        name = _as_str(p.get("container"), "", limit=128)
        if not name:
            raise IpcError(E_BAD_REQUEST, "container 不能为空")
        return _pair(await kernel.request_restart(
            name,
            actor=_as_str(p.get("actor"), "llm", limit=32) or "llm",
            trigger=_as_str(p.get("trigger"), "llm", limit=32) or "llm",
            reason=_as_str(p.get("reason"), f"重启 {name}", limit=200)))

    async def h_request_qq_login(p: dict) -> dict:
        """B17 §8.2：QQ 登录恢复阶梯。"""
        return _pair(await kernel.request_qq_login(
            actor=_as_str(p.get("actor"), "command", limit=32) or "command",
            trigger=_as_str(p.get("trigger"), "command", limit=32) or "command",
            reason=_as_str(p.get("reason"), "", limit=200)))

    async def h_qq_login_status(p: dict) -> dict:
        """B17 §10.3：登录状态页读取的只读透传（不含任何凭据字段）。"""
        return _jsonable(await kernel.qq_login_status())

    async def h_start_container(p: dict) -> dict:
        name = _as_str(p.get("container"), "", limit=128)
        if not name:
            raise IpcError(E_BAD_REQUEST, "container 不能为空")
        return _pair(await kernel.request_start(
            name,
            actor=_as_str(p.get("actor"), "llm", limit=32) or "llm",
            trigger=_as_str(p.get("trigger"), "llm", limit=32) or "llm"))

    async def h_set_mode(p: dict) -> dict:
        return _pair(await kernel.set_mode(_as_str(p.get("mode"), "", limit=32)))

    async def h_set_profile(p: dict) -> dict:
        return _pair(await kernel.set_profile(
            _as_str(p.get("profile"), "", limit=32)))

    # ---- 适配层 → 内核的记账（审计/活跃度由内核单写者持有，I1） ---------- #
    async def h_note_event(p: dict) -> None:
        kernel.note_event()
        return

    async def h_audit_append(p: dict) -> None:
        kernel.audit_append(
            kind=_as_str(p.get("kind"), "adapter.event", limit=64),
            target=_as_str(p.get("target"), "", limit=128),
            detail=_as_str(p.get("detail"), "", limit=300),
            ok=_as_bool(p.get("ok"), True),
            actor=_as_str(p.get("actor"), "llm", limit=32) or "llm",
            trigger=_as_str(p.get("trigger"), "llm", limit=32) or "llm")
        return

    # ---- 探活 ---------------------------------------------------------- #
    async def h_ping(p: dict) -> dict:
        return {"version": str(getattr(kernel, "version", "") or ""),
                "ts": time.time(), "pid": _safe_pid()}

    return {
        "health_report": h_health_report,
        "lifecycle_status": h_lifecycle_status,
        "status_text": h_status_text,
        "lifecycle_text": h_lifecycle_text,
        "jobs_text": h_jobs_text,
        "safety_warnings": h_safety_warnings,
        "allowlist": h_allowlist,
        "diagnose": h_diagnose,
        "list_incidents": h_list_incidents,
        "level_prompt": h_level_prompt,
        "injection": h_injection,
        "status_context": h_status_context,
        "runtime_flags": h_runtime_flags,
        "sleep_armed": h_sleep_armed,
        "run_check": h_run_check,
        "request_sleep": h_request_sleep,
        "restart_container": h_restart_container,
        "request_qq_login": h_request_qq_login,
        "qq_login_status": h_qq_login_status,
        "start_container": h_start_container,
        "set_mode": h_set_mode,
        "set_profile": h_set_profile,
        "note_event": h_note_event,
        "audit_append": h_audit_append,
        "ping": h_ping,
    }


def _safe_pid() -> int:
    try:
        import os
        return os.getpid()
    except Exception:  # noqa: BLE001  # pragma: no cover
        return 0


#: 方法名全集（供服务端鉴权/契约测试断言"表里没有多余/缺失"）
METHOD_NAMES = frozenset({
    "health_report", "lifecycle_status", "status_text", "lifecycle_text",
    "jobs_text", "safety_warnings", "allowlist", "diagnose", "list_incidents",
    "level_prompt", "injection", "status_context", "runtime_flags",
    "sleep_armed", "run_check", "request_sleep", "restart_container",
    "request_qq_login", "qq_login_status",
    "start_container", "set_mode", "set_profile", "note_event",
    "audit_append", "ping",
})

#: 会产生真实副作用的 method（服务端超时后**不得重试**，避免重复执行）
MUTATING_METHODS = frozenset({
    "run_check", "request_sleep", "restart_container", "start_container",
    "request_qq_login",
    "set_mode", "set_profile", "note_event", "audit_append",
})


def missing_methods(handlers: dict[str, Handler]) -> list[str]:
    return sorted(METHOD_NAMES - set(handlers))


def extra_methods(handlers: dict[str, Handler]) -> list[str]:
    return sorted(set(handlers) - METHOD_NAMES)


__all__ = [
    "E_INTERNAL",
    "E_UNKNOWN_METHOD",
    "METHOD_NAMES",
    "MUTATING_METHODS",
    "Handler",
    "build_handlers",
    "extra_methods",
    "missing_methods",
]
