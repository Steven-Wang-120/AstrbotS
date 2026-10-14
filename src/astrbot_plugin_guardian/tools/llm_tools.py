"""Guardian 守护系统 —— LLM 工具的业务逻辑。

对应文档：B09

本模块只提供「纯逻辑」函数（入参为 runtime / 事件信息），
`main.py` 中的 `@filter.llm_tool()` 方法负责参数解析与回执。
这样命令入口与工具入口复用同一套逻辑，避免两份实现。

重要：本模块**不做任何关键词匹配**。工具是否被调用完全由 LLM 自主决定。
"""

from __future__ import annotations

from typing import Any

from ..core.log import logger

VALID_SCOPES = {"all", "containers", "qq", "ros2"}
VALID_MODES = {"server", "ros2"}
VALID_PROFILES = {"normal", "night", "maintenance", "aggressive"}


def _admin_required(rt) -> bool:
    return bool(rt.config.llm_tools.require_admin)


def check_admin(rt, is_admin: bool) -> tuple[bool, str]:
    if not _admin_required(rt):
        return True, ""
    if is_admin:
        return True, ""
    return False, "该操作需要管理员权限，已拒绝。"


# --------------------------------------------------------------------------- #
# 只读工具
# --------------------------------------------------------------------------- #
def health_report(rt, scope: str = "all") -> str:
    scope = (scope or "all").strip().lower()
    if scope not in VALID_SCOPES:
        return f"参数 scope 非法：{scope}。可选 {sorted(VALID_SCOPES)}。"
    return rt.health_report(scope)


def lifecycle_status(rt) -> str:
    st = rt.engine.lifecycle_state
    lc = rt.config.lifecycle
    return (
        "生命周期状态：\n"
        f"- {rt.lifecycle_policy.describe(st)}\n"
        f"- 硬上限: {lc.max_uptime_hours:.0f}h（{lc.max_uptime_hours / 24:.0f} 天），"
        f"到达强制睡眠: {'是' if lc.force_sleep_on_deadline else '否'}\n"
        f"- 当前注入等级: {rt.state.get('lifecycle.inject_level') or '(无)'}\n"
        f"- 睡眠目标: {', '.join(lc.sleep_targets) or '(未配置)'}\n"
        f"- 最近睡眠: {rt.engine.lifecycle_text().splitlines()[-1]}"
    )


def list_incidents(rt, limit: int = 10) -> str:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    return rt.incidents_text(limit)


def diagnose(rt, target: str = "all") -> str:
    return rt.diagnose(target)


# --------------------------------------------------------------------------- #
# 有副作用工具
# --------------------------------------------------------------------------- #
async def run_check(rt, scope: str = "all") -> str:
    scope = (scope or "all").strip().lower()
    if scope not in VALID_SCOPES:
        return f"参数 scope 非法：{scope}。可选 {sorted(VALID_SCOPES)}。"
    result = await rt.run_check(scope)
    if result.get("skipped"):
        return "上一次巡检尚未结束，本次已跳过。"
    results = result.get("results", [])
    bad = [r for r in results if r["status"] not in ("ok",)]
    head = f"巡检完成（{result.get('elapsed_ms', 0)}ms，{len(results)} 项）。"
    if not bad:
        return head + " 全部正常。"
    lines = [head, f"发现 {len(bad)} 项异常："]
    for r in bad[:10]:
        lines.append(f"  - [{r['status']}] {r['target']}: {r['message']}")
    return "\n".join(lines)


async def request_sleep(rt, reason: str, duration_min: float = 0,
                        is_admin: bool = False, armed: bool = False) -> str:
    """请求一次真睡眠（重启目标容器）。由 Bot 自主决定调用。"""
    try:
        dur = int(float(duration_min or 0))
    except (TypeError, ValueError):
        dur = 0
    dur = max(0, min(120, dur))
    reason = (reason or "Bot 自主请求休息").strip()[:200]

    if not rt.config.lifecycle.enabled:
        return "生命周期与睡眠功能当前已关闭，无法执行睡眠。"

    if rt.config.dry_run:
        ok, detail = await rt.request_sleep(reason, dur, actor="llm",
                                            trigger="llm", bypass_confirm=True)
        return f"（dry-run 模式）睡眠未真正执行：{detail}"

    if not armed:
        # 未到 arm_sleep 等级时，需管理员确认；此处由工具层直接执行但记录来源
        allowed, msg = check_admin(rt, is_admin)
        if not allowed:
            return (msg + " 若确实需要，可等待生命周期等级推进到"
                    "「准备睡眠」或由管理员执行 /guardian sleep。")

    ok, detail = await rt.request_sleep(reason, dur, actor="llm",
                                        trigger="llm", bypass_confirm=armed)
    if ok:
        return (f"已开始睡眠：{detail}。我会休息一下，"
                "期间服务会短暂中断，醒来后继续。")
    return f"睡眠未执行：{detail}"


async def restart_container(rt, container: str, reason: str = "",
                            is_admin: bool = False) -> str:
    allowed, msg = check_admin(rt, is_admin)
    if not allowed:
        return msg
    name = (container or "").strip()
    allow = rt.allowlist()
    if name not in allow:
        return (f"容器 {name or '(空)'} 不在动作白名单内，已拒绝。"
                f"允许的目标：{', '.join(allow) or '(白名单为空)'}")
    ok, detail = await rt.request_restart(
        name, actor="llm", trigger="llm", reason=reason or "Bot 请求重启")
    if ok:
        return f"已重启容器 {name}：{detail}"
    return f"重启 {name} 未执行：{detail}"


async def set_mode(rt, mode: str, is_admin: bool = False) -> str:
    allowed, msg = check_admin(rt, is_admin)
    if not allowed:
        return msg
    m = (mode or "").strip().lower()
    if m not in VALID_MODES:
        return f"参数 mode 非法：{mode}。可选 {sorted(VALID_MODES)}。"
    ok, detail = await rt.set_mode(m)
    return detail if ok else f"切换失败：{detail}"


async def set_profile(rt, profile: str, is_admin: bool = False) -> str:
    allowed, msg = check_admin(rt, is_admin)
    if not allowed:
        return msg
    p = (profile or "").strip().lower()
    if p not in VALID_PROFILES:
        return f"参数 profile 非法：{profile}。可选 {sorted(VALID_PROFILES)}。"
    ok, detail = await rt.set_profile(p)
    return detail if ok else f"切换失败：{detail}"


# --------------------------------------------------------------------------- #
# 供 on_llm_request 注入的动态上下文
# --------------------------------------------------------------------------- #
def build_status_context(rt) -> str:
    st = rt.engine.lifecycle_state
    return (
        "<guardian_status>\n"
        f"守护模式: {rt.state.get('mode')}\n"
        f"运行档位: {rt.state.get('profile')}\n"
        f"系统已连续运行: {st.uptime_hours:.1f} 小时\n"
        f"生命周期等级: {st.level_id} {st.level_name}\n"
        "（这是系统内部状态，供你判断是否需要安排休息，不必逐字复述。）\n"
        "</guardian_status>"
    )


def log_tool_call(name: str, args: dict[str, Any] | None) -> None:
    logger.info(f"[guardian] LLM 调用工具 {name} args={args}")
