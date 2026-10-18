"""Guardian 守护系统 —— LLM 工具的业务逻辑。

对应文档：B09

本模块只提供「纯逻辑」函数（入参为 runtime / 事件信息），
`main.py` 中的 `@filter.llm_tool()` 方法负责参数解析与回执。
这样命令入口与工具入口复用同一套逻辑，避免两份实现。

**为什么这里全是 `async`**：`kernel.mode=ipc` 时，所有读取（状态、白名单、
生命周期开关）都必须走 IPC 才能拿到**权威值**。适配层若拿自己那份配置副本
判断，两份配置一旦漂移，就会出现"适配层以为要确认、内核其实已放行"的无确认
破坏性动作。所以本模块一律 `await rt.<方法>()`。

重要：本模块**不做任何关键词匹配**。工具是否被调用完全由 LLM 自主决定。
"""

from __future__ import annotations

import math
from typing import Any

from ..core.log import logger

VALID_SCOPES = {"all", "containers", "qq", "ros2"}
VALID_MODES = {"server", "ros2"}
VALID_PROFILES = {"normal", "night", "maintenance", "aggressive"}


def safe_int(value: Any, default: int = 0, *, low: int | None = None,
             high: int | None = None) -> int:
    """把 LLM/命令传来的数字安全地转成 int。

    LLM 工具的参数类型是 `number`，而 JSON 允许 `Infinity` / `NaN` / `1e400`。
    `int(float("inf"))` 抛的是 **OverflowError**（不是 ValueError），
    `int(float("nan"))` 抛 ValueError。只捕 ValueError 会让 inf 直接击穿工具层
    抛到 AstrBot，用户看到的是一个未处理的异常而不是可读回执。
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    result = int(parsed)
    if low is not None and result < low:
        result = low
    if high is not None and result > high:
        result = high
    return result


async def _runtime_flags(rt) -> dict[str, Any]:
    """取内核的**权威**运行开关（ipc 模式下只能靠 IPC）。"""
    try:
        return dict(await rt.runtime_flags() or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[guardian] 读取内核运行开关失败: {exc}")
        return {}


async def check_admin(rt, is_admin: bool) -> tuple[bool, str]:
    flags = await _runtime_flags(rt)
    if not flags.get("require_admin", True):
        return True, ""
    if is_admin:
        return True, ""
    return False, "该操作需要管理员权限，已拒绝。"


# --------------------------------------------------------------------------- #
# 只读工具
# --------------------------------------------------------------------------- #
async def health_report(rt, scope: str = "all") -> str:
    scope = (scope or "all").strip().lower()
    if scope not in VALID_SCOPES:
        return f"参数 scope 非法：{scope}。可选 {sorted(VALID_SCOPES)}。"
    return await rt.health_report(scope)


async def lifecycle_status(rt) -> str:
    return await rt.lifecycle_status_text()


async def drowsy_status(rt) -> str:
    """"我困不困"——给**正在对话的模型**喂事实，不喂成品句子。

    为什么这里不像 `health_report` 那样直接返回一段排好版的中文：
    这条路径的问题来自"用户在跟我聊天时顺口问了一句你困吗"。
    如果工具自己生成一句"我困了先去睡啦"，模型拿到后只能复述或尴尬地
    转述，等于一段对话里塞了两张嘴。所以这里只交出事实
    （困到什么程度、离下一级还差多久、现在能不能睡），
    由正在说话的模型带着人格自己组织语言。

    同时也**不调 LLM**：内核不碰 provider 是硬约束（I6），
    且这一轮本来就已经在模型里了，再生成一次纯属浪费。
    """
    info = await rt.drowsy_report()
    if not info:
        return ("读取不到自己的在线时长（uptime 探针不可信，通常是容器刚启动"
                "或时钟回拨），所以也说不上困不困。可以如实告诉对方没查到。")

    parts: list[str] = []
    parts.append(f"当前状态：{info.get('level_name') or info.get('level_id')}"
                 f"（{info.get('level_id')}），已连续在线 "
                 f"{info.get('uptime_hours')} 小时")
    parts.append(f"困倦程度：{info.get('mood')}")

    nxt = str(info.get("next_level_name") or "")
    gap = str(info.get("next_level_in_text") or "")
    if nxt and gap:
        parts.append(f"距离「{nxt}」还有 {gap}")

    if info.get("drowsy"):
        if info.get("can_sleep_now"):
            dur = int(info.get("sleep_duration_min") or 0)
            parts.append(f"现在可以睡（打算睡 {dur} 分钟）" if dur else "现在可以睡")
        else:
            why = []
            if info.get("recent_talk"):
                why.append("刚刚还有人在说话")
            if not info.get("in_window"):
                win = str(info.get("window") or "")
                why.append(f"还没到作息窗口（{win}）" if win else "还没到作息窗口")
            parts.append("虽然困，但现在睡不了" + (f"（{'；'.join(why)}）" if why else ""))
    else:
        parts.append("现在完全可以继续聊，不困")

    parts.append(f"距硬性上限还剩：{info.get('remaining_text')}")
    parts.append(f"现在时间：{info.get('now_text')}")

    joined = "\n".join(f"- {p}" for p in parts)
    return ("（以下是它自己的真实状态数据，仅供参考。要不要说、说多少、"
            "用什么语气，由你根据人格和当前对话自己决定；"
            "对方只是随口一问的话，不必把数字全报出来。）\n" + joined)


async def list_incidents(rt, limit: int = 10) -> str:
    return await rt.incidents_text(safe_int(limit, 10, low=1, high=50))


async def diagnose(rt, target: str = "all") -> str:
    return await rt.diagnose(target)


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
                        is_admin: bool = False, armed: bool = False,
                        confirm: bool = False) -> str:
    """请求一次真睡眠（重启目标容器）。由 Bot 自主决定调用。

    破坏性二次确认（B12 §L-D / B09 §6.2）：真睡眠会中断服务，因此除"已武装"的
    权威路径外，首次调用（`confirm=false`）只回确认提示、**不执行**；必须再次调用
    并置 `confirm=true` 才进入安全链。
    """
    dur = safe_int(duration_min, 0, low=0, high=120)
    reason = (reason or "Bot 自主请求休息").strip()[:200]

    flags = await _runtime_flags(rt)
    if flags and not flags.get("lifecycle_enabled", True):
        return "生命周期与睡眠功能当前已关闭，无法执行睡眠。"

    if not armed:
        # 未到 arm_sleep 等级时需管理员权限（L-T）；armed 时**有意**跳过该校验。
        #
        # ⚠️ 这是已确认的设计（B15 §9 A3），不是遗漏：armed 表示"生命周期已推进到
        # 该休息的等级"，此时 Bot 的睡眠请求被视作已授权，L-T 与 L-D 一并放行，
        # 只保留 SafetyGuard（L-S）的白名单/冷却/熔断。代价是：任意用户的
        # 会话在武装窗口内都可能引导 LLM 触发一次真睡眠。面向不可信用户群部署时，
        # 应同时收紧 require_admin 与等级配置，而不是改这里。
        allowed, msg = await check_admin(rt, is_admin)
        if not allowed:
            return (msg + " 若确实需要，可等待生命周期等级推进到"
                    "「准备睡眠」或由管理员执行 /guardian sleep。")

    # 二次确认：只有"已武装"（生命周期权威路径）或显式 confirm=true 才真正执行。
    if not (armed or confirm):
        return ("确认执行？将执行一次真睡眠（重启睡眠目标容器，服务会短暂中断后恢复）。\n"
                "请再次调用并置 confirm=true。")

    ok, detail = await rt.request_sleep(reason, dur, actor="llm",
                                        trigger="llm", bypass_confirm=True)
    if ok:
        return (f"已开始睡眠：{detail}。我会休息一下，"
                "期间服务会短暂中断，醒来后继续。")
    return f"睡眠未执行：{detail}"


async def restart_container(rt, container: str, reason: str = "",
                            is_admin: bool = False, confirm: bool = False) -> str:
    """重启受管容器。破坏性操作，需管理员 + 白名单 + 二次确认（B09 §6.2 / B12 §L-D）。

    首次调用（`confirm=false`）只返回确认提示、不执行；`confirm=true` 才进入
    `SafetyGuard` 白名单/冷却/熔断安全链。
    """
    allowed, msg = await check_admin(rt, is_admin)
    if not allowed:
        return msg
    name = (container or "").strip()
    allow = await rt.allowlist()
    if name not in allow:
        return (f"容器 {name or '(空)'} 不在动作白名单内，已拒绝。"
                f"允许的目标：{', '.join(allow) or '(白名单为空)'}")
    if not confirm:
        return (f"确认执行？将重启容器 {name}。\n"
                "请再次调用并置 confirm=true。")
    ok, detail = await rt.request_restart(
        name, actor="llm", trigger="llm", reason=reason or "Bot 请求重启")
    if ok:
        return f"已重启容器 {name}：{detail}"
    return f"重启 {name} 未执行：{detail}"


async def set_mode(rt, mode: str, is_admin: bool = False) -> str:
    allowed, msg = await check_admin(rt, is_admin)
    if not allowed:
        return msg
    m = (mode or "").strip().lower()
    if m not in VALID_MODES:
        return f"参数 mode 非法：{mode}。可选 {sorted(VALID_MODES)}。"
    ok, detail = await rt.set_mode(m)
    return detail if ok else f"切换失败：{detail}"


async def set_profile(rt, profile: str, is_admin: bool = False) -> str:
    allowed, msg = await check_admin(rt, is_admin)
    if not allowed:
        return msg
    p = (profile or "").strip().lower()
    if p not in VALID_PROFILES:
        return f"参数 profile 非法：{profile}。可选 {sorted(VALID_PROFILES)}。"
    ok, detail = await rt.set_profile(p)
    return detail if ok else f"切换失败：{detail}"


def log_tool_call(name: str, args: dict[str, Any] | None) -> None:
    logger.info(f"[guardian] LLM 调用工具 {name} args={args}")
