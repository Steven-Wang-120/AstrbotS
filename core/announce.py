"""Guardian 守护系统 —— 主动消息的"事实层"。

对应需求：Bot 困了会自己去跟管理员说一声要睡了。

## 这个模块为什么只装事实、不含句子

用户明确要求：「到底说什么让 LLM 自己决定」。所以这里**不允许**出现任何
写死的成品话术（"我困了""我去睡了""晚安"之类）。内核只负责回答两个问题：

1. 现在**该不该**开口（由 uptime / 作息窗口 / 幂等去重共同决定）；
2. 如果要开口，**有哪些事实**必须告诉模型（运行多久、几级了、离窗口还有
   几小时、这次睡多久、刚才为什么推迟了……）。

"怎么把这些事实说成一句人话"完全交给适配层的 `compose_announce()`，
由 `context.llm_generate()` 结合人格与最近对话历史生成。内核进程不碰
provider 凭据，也不 `import astrbot`（不变式 I6）。

## 为什么这里不能写死句子（不只是"用户偏好"）

写死的话术有三个实际坏处，都是本项目已经踩过或明确要避开的：

- **人格不同就串味**。同一个模板套在一个毒舌人格和一个温柔人格上，
  前者会显得机械、后者会显得冷淡。只有让模型拿着人格自己组织才协调。
- **事实变化要改代码**。等级从 L1 到 L4、有没有被窗口挡住、顺延了多久、
  是不是被强制——组合起来十几种，写死就是十几句，且随配置改动立刻失效。
- **无法随时间自适应**。模型知道"现在几点"，可以把"还有 5 小时天就亮了"
  这种只有它才能算出来的话自然带进去。

## 占位符

`render_template()` 只做一件事：把 `{{name}}` 替换成事实的文本形式。
**故意不认识的占位符原样保留**——写错 `{{currenttime}}` 时，模型会看到
一串花括号而不是被静默替换成空，人能一眼看出模板配错了。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: 本模块能替换的全部占位符。新增占位符必须同时登记在这里，
#: `tests/test_announce.py` 会用这个元组反查"DEFAULTS 模板里有没有写错的"。
PLACEHOLDERS: tuple[str, ...] = (
    "{{current_time}}",     # 现在几点，如 "10月18日 02:14（周六）"
    "{{uptime}}",           # 已运行时长，如 "24.5 小时（1.0 天）"
    "{{level_id}}",         # "L3"
    "{{level_name}}",       # "III 级 · 准备睡眠"
    "{{remaining}}",        # 距硬上限还剩多久
    "{{duration}}",         # 这次打算睡多久
    "{{targets}}",          # 会睡哪些容器
    "{{period}}",           # 作息窗口本身，如 "02:00 - 07:00"
    "{{to_window}}",        # 距窗口开启还有多久（已在窗口内则是"已在窗口内"）
    "{{defer_reason}}",     # 上次为什么没睡成
    "{{wait_text}}",        # 顺延已持续多久
    "{{slept}}",            # 上次睡了多久
    "{{forced}}",           # "是" / "否"，是否属于强制睡眠
    "{{reason}}",           # 内核给出的判定原因（给模型当背景，不必原文照抄）
)

KIND_LABELS: dict[str, str] = {
    "sleep": "准备睡了",
    "deferred": "想睡但还没睡成",
    "resume": "睡醒回来了",
}


def _fmt_hours(hours: float | None) -> str:
    """小时数 → 人能读的"X 天 Y 小时" / "X 小时 Y 分"。

    为什么要分层：这个值会出现在"已经连续在线"和"距硬上限还剩"两处，
    量级差了两个数量级。601 小时说成"601 小时"没人能换算，119 小时
    说成"4 天 23 小时"才看得出"还早"。同一套刻度用在两处，读的人才
    不用心算。

    分档：
    - < 1 小时  → "MM 分钟"
    - < 1 天    → "H 小时 M 分"
    - >= 1 天   → "D 天 H 小时"（整小时不补 0 分）
    """
    if hours is None:
        return "未知"
    try:
        value = float(hours)
    except (TypeError, ValueError):
        return "未知"
    if value != value or value < 0:  # NaN 或负数
        return "未知"
    total_min = int(round(value * 60))
    if total_min < 60:
        return f"{total_min} 分钟"
    hh, mm = divmod(total_min, 60)
    if hh < 24:
        return f"{hh} 小时" if mm == 0 else f"{hh} 小时 {mm} 分"
    days, rest_h = divmod(hh, 24)
    return f"{days} 天" if rest_h == 0 else f"{days} 天 {rest_h} 小时"


def now_text(now: float | None = None, tz_offset_h: float = 8.0) -> str:
    """把时间戳写成"10月18日 02:14（周六）"。

    `tz_offset_h` 必须来自 `env_platform` 探针落库的值，不能用 `time.timezone`
    ——容器里那个经常是 0，会让"凌晨两点"显示成"下午六点"。
    """
    ts = time.time() if now is None else float(now)
    # 用固定偏移而非 localtime：容器 TZ 不可信，偏移由调用方从探针给出。
    utc = time.gmtime(ts)
    shifted = time.gmtime(ts + float(tz_offset_h) * 3600.0)
    weekday = "一二三四五六日"[shifted.tm_wday]
    # `tm_yday` 由 gmtime 给出的是 UTC 的那一天；跨时区后日期可能差一天，
    # 这里用移位后的时间戳重算，保证"日期"和"时分"来自同一个时区。
    _ = utc
    return (f"{shifted.tm_mon} 月 {shifted.tm_mday} 日 "
            f"{shifted.tm_hour:02d}:{shifted.tm_min:02d}（周{weekday}）")


@dataclass
class SleepyFacts:
    """一次"要开口"的全部事实。字段只描述世界，不描述措辞。"""

    #: sleep | deferred | resume
    kind: str = "sleep"
    level_id: str = "L3"
    level_name: str = "准备睡眠"
    #: 已运行时长（小时）
    uptime_hours: float = 0.0
    #: 现在几点（已格式化的本地时间）
    now_text: str = ""
    #: 作息窗口本身，如 "02:00 - 07:00"；未启用时为空串
    period: str = ""
    #: 距窗口开启还有几小时；已在窗口内为 0.0
    hours_to_window: float = 0.0
    #: 此刻是否落在窗口内
    in_window: bool = True
    #: 这次打算睡多久（分钟）；0 表示"醒来再说"
    duration_min: int = 0
    #: 会参与睡眠的容器名
    targets: tuple[str, ...] = ()
    #: 距硬上限还剩几小时；None 表示未知
    remaining_hours: float | None = None
    #: 是否属于强制睡眠（L4 / 硬上限）
    forced: bool = False
    #: 上次没睡成的原因（deferred 用）
    defer_reason: str = ""
    #: 顺延已持续多久（小时）
    wait_hours: float = 0.0
    #: 最近是否有人在说话（决定了语气该不该"打扰一下"）
    recent_talk: bool = False
    #: 上一次睡了多少分钟（resume 用）
    slept_minutes: int = 0
    #: 内核判定原因，原样给模型当背景
    reason: str = ""
    #: 预留：适配层补充的额外事实（例如"现在有人在群里聊天"）
    extras: dict[str, Any] = field(default_factory=dict)

    # ---------------- 派生文本（供模板替换） ---------------- #
    @property
    def uptime_text(self) -> str:
        return _fmt_hours(self.uptime_hours)

    @property
    def remaining_text(self) -> str:
        if self.remaining_hours is None:
            return "未设置硬上限"
        return _fmt_hours(self.remaining_hours)

    @property
    def duration_text(self) -> str:
        if not self.duration_min:
            return "睡到自然醒"
        return f"{int(self.duration_min)} 分钟"

    @property
    def targets_text(self) -> str:
        return "、".join(self.targets) if self.targets else "（暂无可用目标）"

    @property
    def to_window_text(self) -> str:
        if not self.period:
            return "未设置作息窗口"
        if self.in_window:
            return "已经在窗口内了"
        return f"还有 {_fmt_hours(self.hours_to_window)}"

    @property
    def wait_text(self) -> str:
        if self.wait_hours <= 0:
            return "刚开始"
        return f"已经等了 {_fmt_hours(self.wait_hours)}"

    @property
    def slept_text(self) -> str:
        if self.slept_minutes <= 0:
            return "不知道睡了多久"
        return _fmt_hours(self.slept_minutes / 60.0)

    @property
    def forced_text(self) -> str:
        return "是" if self.forced else "否"

    @property
    def kind_text(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def as_mapping(self) -> dict[str, str]:
        """占位符 → 文本。`render_template` 唯一使用的数据源。"""
        return {
            "{{current_time}}": self.now_text or "未知",
            "{{uptime}}": self.uptime_text,
            "{{level_id}}": self.level_id,
            "{{level_name}}": self.level_name,
            "{{remaining}}": self.remaining_text,
            "{{duration}}": self.duration_text,
            "{{targets}}": self.targets_text,
            "{{period}}": self.period or "未设置",
            "{{to_window}}": self.to_window_text,
            "{{defer_reason}}": self.defer_reason or "（没有推迟过）",
            "{{wait_text}}": self.wait_text,
            "{{slept}}": self.slept_text,
            "{{forced}}": self.forced_text,
            "{{reason}}": self.reason or "（无）",
        }

    def to_dict(self) -> dict[str, Any]:
        """给 IPC 用的纯 JSON 结构。模板也从这里带出去。"""
        data = {
            "kind": self.kind,
            "kind_text": self.kind_text,
            "level_id": self.level_id,
            "level_name": self.level_name,
            "uptime_hours": round(float(self.uptime_hours), 3),
            "now_text": self.now_text,
            "period": self.period,
            "hours_to_window": round(float(self.hours_to_window), 3),
            "in_window": bool(self.in_window),
            "duration_min": int(self.duration_min),
            "targets": list(self.targets),
            "remaining_hours": (None if self.remaining_hours is None
                                else round(float(self.remaining_hours), 3)),
            "forced": bool(self.forced),
            "defer_reason": self.defer_reason,
            "wait_hours": round(float(self.wait_hours), 3),
            "recent_talk": bool(self.recent_talk),
            "slept_minutes": int(self.slept_minutes),
            "reason": self.reason,
            "extras": dict(self.extras or {}),
        }
        # 顺便把派生文本一起送出去：适配层渲染模板时不必再重复实现一遍
        # 格式规则，减少"两边算法不一致"的风险。
        data["text"] = self.as_mapping()
        return data


def render_template(template: str, facts: Any, extra: dict[str, str] | None = None) -> str:
    """把 `{{占位符}}` 换成事实文本。

    三条约定：

    1. **按长度降序替换**。否则 `{{uptime}}` 会先吃掉 `{{uptime_hours}}`
       的前半段，留下 `3.5_hours` 这种残渣。
    2. **不认识的占位符原样保留**。静默替换成空串会让人以为模板生效了，
       实际上关键事实被吞掉；留着花括号一眼就能看出配错了。
    3. **不做任何文案加工**。不加前缀、不加表情、不改标点——那是模型的活。

    `facts` 可以是 `SleepyFacts` 也可以是其 `to_dict()` 的结果；两种形态
    都支持，因为内核侧与适配层侧拿到的东西不一样。
    """
    if not template:
        return ""
    if isinstance(facts, SleepyFacts):
        mapping = facts.as_mapping()
    elif isinstance(facts, dict):
        text_map = facts.get("text")
        mapping = dict(text_map) if isinstance(text_map, dict) else _dict_to_mapping(facts)
    else:
        mapping = {}
    if extra:
        mapping.update({str(k): str(v) for k, v in extra.items()})

    out = str(template)
    for key in sorted(mapping, key=len, reverse=True):
        out = out.replace(key, str(mapping[key]))
    return out


def _dict_to_mapping(data: dict[str, Any]) -> dict[str, str]:
    """给"直接传状态字典"的场景兜一层：只能替换字典里真有的键。"""
    out: dict[str, str] = {}
    for name in PLACEHOLDERS:
        bare = name.strip("{}")
        if bare in data and data[bare] is not None:
            out[name] = str(data[bare])
    return out


def missing_placeholders(template: str) -> list[str]:
    """模板里出现但本模块不认识的占位符。配置校验与测试共用。"""
    import re

    found = re.findall(r"\{\{[a-zA-Z_][a-zA-Z0-9_]*\}\}", template or "")
    known = set(PLACEHOLDERS)
    return sorted({f for f in found if f not in known})


def place_text(umo: str) -> str:
    """把 UMO 描述成"群里 / 私聊"这种不泄露号码的说法。

    UMO 形如 `aiocqhttp:GroupMessage:123456` / `aiocqhttp:FriendMessage:789`。
    消息里说容器名和群号既无必要也不合适，只给"在群里"这个粒度。
    """
    text = str(umo or "")
    lowered = text.lower()
    if "group" in lowered:
        return "群里"
    if "friend" in lowered or "private" in lowered or "direct" in lowered:
        return "私聊"
    return ""
