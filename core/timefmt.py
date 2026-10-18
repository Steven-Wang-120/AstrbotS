"""Guardian 守护系统 —— 本地时间与时段的语义化描述（纯函数，无 IO）。

**为什么需要这一层**：LLM 没有可靠的"现在几点"感知。把裸 Unix 时间戳丢给模型，
让它自己推断"这算不算深夜"，得到的是随机答案——而且每次对话都可能不同，于是
作息判定的行为不可复现。正确做法是**在插件里用纯函数算好**，只把结论
（"2026-10-18 02:13（周日 · 深夜）"）交给模型。

**为什么不用 `datetime.now()`**：这个模块必须能被单测。所有函数都以
`now: float`（Unix 时间戳）和 `tz_offset_h` 作为入参，自己不做取时动作。
唯一"取时"的地方是调用方（`kernel_runtime.llm_injection`），它拿的是
`time.time()` 与落盘的 `env.tz_offset_h`。

**为什么时段划分这么粗**：只到 7 段、且不在时段之间改变语气。粒度再细
（比如"早上八点该更有精神"）既无法验证，也会让模型把注意力放错地方。
时段只用于两件事：给模型一个"现在是什么时候"的锚点，以及配合作息窗口
判断"困不困"。

本模块**不得 `import astrbot`**（I6 域分离，见 `tests/test_kernel_decoupling.py`）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 时段划分：`(起, 止, 名称)`，左闭右开（`[起, 止)`）。
#
# 顺序按开始小时升序排列，"深夜"跨越零点所以它的区间写在最后单独处理。
# 之所以把 23:00–05:00 定义为"深夜"而不是"夜间"：口语里 23 点以后说的
# 都是"半夜/深夜"，05:00 天开始亮才叫"凌晨"。
PERIODS: list[tuple[int, int, str]] = [
    (5, 8, "凌晨"),    # 05:00-07:59 天将亮
    (8, 11, "上午"),   # 08:00-10:59
    (11, 13, "中午"),  # 11:00-12:59
    (13, 17, "下午"),  # 13:00-16:59
    (17, 19, "傍晚"),  # 17:00-18:59
    (19, 23, "晚上"),  # 19:00-22:59
]

# 23:00–05:00 单独定义，因为它跨零点
LATE_NIGHT_START = 23
LATE_NIGHT_END = 5
LATE_NIGHT_NAME = "深夜"

WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]

# 时区偏移的合理范围（UTC-12 到 UTC+14），超出视为数据错误
TZ_OFFSET_MIN_H = -12.0
TZ_OFFSET_MAX_H = 14.0


def clamp_tz_offset(tz_offset_h: float) -> float:
    """把时区偏移夹到真实存在的范围内；非数值/NaN 一律按 UTC 处理。

    宁可给出一个"保守但确定"的值，也不要让一个坏偏移把整条作息判定带偏。
    """
    try:
        value = float(tz_offset_h)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    if value == float("inf"):
        return TZ_OFFSET_MAX_H
    if value == float("-inf"):
        return TZ_OFFSET_MIN_H
    return max(TZ_OFFSET_MIN_H, min(TZ_OFFSET_MAX_H, value))


def local_now(now: float, tz_offset_h: float = 8.0) -> datetime:
    """把 Unix 时间戳转成"某个固定偏移下的本地时间"（带 tzinfo 的 aware datetime）。

    **不用 `ZoneInfo`**：作息判定只关心"墙上几点"，不关心 DST 过渡那一小时的
    历史正确性。用固定偏移可以避免"某个地区某天缺一小时"导致窗口边界漂移，
    也让结果完全可复现。真正的时区解析在 `probes/env_platform.py` 里做完了。
    """
    offset = clamp_tz_offset(tz_offset_h)
    tz = timezone(timedelta(hours=offset))
    try:
        return datetime.fromtimestamp(float(now), tz=tz)
    except (TypeError, ValueError, OSError, OverflowError):
        return datetime.fromtimestamp(0.0, tz=tz)


def describe_period(now: float, tz_offset_h: float = 8.0) -> str:
    """返回时段名：`深夜` / `凌晨` / `上午` / `中午` / `下午` / `傍晚` / `晚上`。"""
    hour = local_now(now, tz_offset_h).hour
    if hour >= LATE_NIGHT_START or hour < LATE_NIGHT_END:
        return LATE_NIGHT_NAME
    for start, end, name in PERIODS:
        if start <= hour < end:
            return name
    # 理论上到不了这里（PERIODS 覆盖 05:00-22:59，深夜覆盖 23:00-04:59）
    return LATE_NIGHT_NAME


def describe(now: float, tz_offset_h: float = 8.0) -> tuple[str, str]:
    """返回 `(形如 "2026-10-18 02:13（周日 · 深夜）", 时段名)`。

    第二个返回值是给上层做判断用的（M3 的困意表达），不展示给模型。
    固定 `YYYY-MM-DD HH:MM` 格式，不用 `strftime("%c")` —— 后者随 locale 变，
    单测会挂在不同机器上。
    """
    moment = local_now(now, tz_offset_h)
    period = describe_period(now, tz_offset_h)
    weekday = WEEKDAY_CN[moment.weekday()]
    text = f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d} " \
           f"{moment.hour:02d}:{moment.minute:02d}（周{weekday} · {period}）"
    return text, period


def describe_uptime(uptime_hours: float) -> str:
    """把连续运行时长写成"3.2 天"这种模型能直接理解的形式。

    不足 1 天时用小时，避免出现"0.1 天"这种反直觉表述。
    """
    try:
        hours = float(uptime_hours)
    except (TypeError, ValueError):
        return "未知"
    if hours != hours or hours < 0:  # NaN / 负数
        return "未知"
    if hours < 24:
        return f"{hours:.1f} 小时"
    return f"{hours / 24.0:.1f} 天"
