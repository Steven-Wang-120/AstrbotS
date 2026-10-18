"""Guardian 守护系统 —— 作息窗口判定（纯函数，无 IO）。

**窗口只做闸门，不做触发器。** 这一点是整个设计的核心，值得写清楚：

```
困意（core/lifecycle.py）  uptime → L0..L4 → 该不该睡
窗口（本模块）              现在几点 + 最近有无对话 → 能不能睡
                           ↑ 两者都满足才真正执行
```

如果合并成一个"到点就重启"的机制，`lifecycle.levels` 那张按连续运行时长
递增的表就失去意义了——白天有人、夜里也有人，最终退化成定时重启。

**绝不能靠解析对话文本判断"该不该睡"**（B15 §6 / B09 v0.2.0）。窗口判定
基于系统时钟与 `state["last_event_ts"]`，不匹配任何关键词。

**跨零点是唯一容易写错的地方**：`23:00–07:00` 这种区间的两个端点大小关系
是反的。本模块统一把本地时刻折算成"当日第 N 分钟"，再判断是否落在
`[start, 1440) ∪ [0, end)`。

本模块**不得 `import astrbot`**（I6 域分离）。
"""

from __future__ import annotations

import re

MINUTES_PER_DAY = 24 * 60

# `HH:MM`，允许 `H:MM`；不接受 `24:00`（那不是合法的墙上时间）
_HHMM = re.compile(r"^\s*(\d{1,2})\s*:\s*(\d{1,2})\s*$")


def parse_hhmm(text: str) -> int | None:
    """解析 `HH:MM`，返回"当日第几分钟"（0..1439）；非法返回 None。

    与 `core/config.py:_parse_hhmm` 的区别：那边返回 `(hour, minute)` 元组供
    配置校验用，这里返回便于比较的单一整数。两边的合法范围必须一致：
    `00:00` 合法、`23:59` 合法、`24:00` 非法、`-1:00` 非法。

    **不接受 `24:00`**：它常被当成"一天结束"，但当作窗口端点时语义会变得
    含混（是 24:00 还是次日 00:00？）。想要"到午夜"就写 `00:00`。
    """
    if not isinstance(text, str):
        return None
    match = _HHMM.match(text)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def format_hhmm(minutes: int) -> str:
    """把"当日第几分钟"格式化回 `HH:MM`（`1440` 归零成 `00:00`）。"""
    value = int(minutes) % MINUTES_PER_DAY
    return f"{value // 60:02d}:{value % 60:02d}"


def _local_minutes(now: float, tz_offset_h: float) -> int:
    """当前本地时刻折算成"当日第几分钟"。"""
    from .timefmt import local_now

    moment = local_now(now, tz_offset_h)
    return moment.hour * 60 + moment.minute


def window_minutes(start: str, end: str) -> tuple[int, int] | None:
    """把两端字符串解析成分钟数；任一端非法返回 None。

    `start == end` **视为零长度**（也就是永不命中），由调用方决定要不要据此
    禁用整个特性。这里不替它做决定，只如实返回。
    """
    start_min = parse_hhmm(start)
    end_min = parse_hhmm(end)
    if start_min is None or end_min is None:
        return None
    return start_min, end_min


def window_length_minutes(start: str, end: str) -> int | None:
    """窗口时长（分钟）。`start == end` 返回 0；非法配置返回 None。"""
    bounds = window_minutes(start, end)
    if bounds is None:
        return None
    start_min, end_min = bounds
    if start_min == end_min:
        return 0
    if start_min < end_min:
        return end_min - start_min
    # 跨零点：23:00 -> 07:00 是 8 小时
    return (MINUTES_PER_DAY - start_min) + end_min


def in_window(now: float, start: str, end: str, tz_offset_h: float = 8.0) -> bool:
    """当前是否落在 `[start, end)` 内（左闭右开）。

    三条语义必须记住：

    1. **左闭右开**：`[02:00, 07:00)` 里 `07:00` 已经**不在**窗口内。否则
       两个相邻窗口会在端点处重复命中。
    2. **支持跨零点**：`23:00–07:00` 覆盖 23:00-23:59 与 00:00-06:59。
    3. **`start == end` 视为零长度、永不命中**（D4 决策）。想要"全天"就
       不要启用这个特性——把它写成 00:00-00:00 然后期望全天，是自找麻烦。

    非法配置一律返回 False：宁可"不睡"，也不要因为一个坏配置在错误的
    时刻把容器停掉。
    """
    bounds = window_minutes(start, end)
    if bounds is None:
        return False
    start_min, end_min = bounds
    if start_min == end_min:
        return False
    current = _local_minutes(now, tz_offset_h)
    if start_min < end_min:
        return start_min <= current < end_min
    # 跨零点
    return current >= start_min or current < end_min


def minutes_until_window(now: float, start: str, end: str,
                         tz_offset_h: float = 8.0) -> int:
    """距离"下一次窗口开始"还有多少分钟。

    - 在窗口内：返回 0（已经开始了）
    - 在窗口前：返回距 `start` 的分钟数
    - 在窗口后：返回距"明天/次日的 `start`"的分钟数

    非法配置返回 -1（调用方据此显示"未配置"而不是一个假数字）。
    """
    bounds = window_minutes(start, end)
    if bounds is None:
        return -1
    start_min, end_min = bounds
    if start_min == end_min:
        return -1
    current = _local_minutes(now, tz_offset_h)
    inside = (start_min <= current < end_min) if start_min < end_min else (
        current >= start_min or current < end_min)
    if inside:
        return 0
    return (start_min - current) % MINUTES_PER_DAY


def window_bounds(now: float, start: str, end: str,
                  tz_offset_h: float = 8.0) -> tuple[float, float]:
    """返回本次（或下一次）窗口的 `(起, 止)` Unix 时间戳。

    用途是给日志与状态文本一个"这一觉大概睡到几点"的具体值。跨零点时
    `止 > 起` 仍然成立（止落在次日），调用方不需要再判断一次。

    非法配置、以及 `start == end` 返回 `(0.0, 0.0)`。
    """
    bounds = window_minutes(start, end)
    if bounds is None:
        return 0.0, 0.0
    start_min, end_min = bounds
    if start_min == end_min:
        return 0.0, 0.0
    length = window_length_minutes(start, end) or 0
    lead = minutes_until_window(now, start, end, tz_offset_h)
    if lead < 0:
        return 0.0, 0.0
    # 窗口内 `lead == 0`，此时起点就在今天（或昨天，跨零点时）；窗口前
    # `lead` 是"距起点还有几分钟"，两种都统一成 `now + lead * 60`。
    if lead == 0:
        current = _local_minutes(now, tz_offset_h)
        # 已过 start 且在同一天：往回退到 start
        # 跨零点且 now 在次日凌晨：同样是往回退
        back = (current - start_min) % MINUTES_PER_DAY
        begin = now - back * 60.0
    else:
        begin = now + lead * 60.0
    return begin, begin + length * 60.0
