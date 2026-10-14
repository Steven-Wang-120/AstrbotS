"""Guardian 守护系统 —— 生命周期等级与自主睡眠策略。

对应文档：B15

核心原则（P1，不可协商）：
- 绝不匹配对话关键词。等级推进只依赖容器运行时长。
- 每级注入不同提示词，引导 Bot 自主决定睡眠。
- `max_uptime_hours` 为硬上限，到达即强制睡眠（除非显式关闭）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LevelSpec:
    id: str = "L0"
    name: str = "常规"
    after_hours: float = 0.0
    prompt: str = ""
    action: str = "none"  # none | notify | arm_sleep | force_sleep

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LevelSpec":
        return cls(
            id=str(d.get("id") or "L0"),
            name=str(d.get("name") or "常规"),
            after_hours=float(d.get("after_hours", 0) or 0),
            prompt=str(d.get("prompt") or ""),
            action=str(d.get("action") or "none"),
        )


@dataclass
class LifecycleState:
    uptime_hours: float = 0.0
    level_id: str = "L0"
    level_name: str = "常规"
    level_index: int = 0
    last_sleep_at: float = 0.0
    last_inject_at: float = 0.0
    last_inject_level: str = ""
    sleep_armed: bool = False
    forced_at: float = 0.0
    clock_skew_warned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "uptime_hours": round(self.uptime_hours, 3),
            "level_id": self.level_id,
            "level_name": self.level_name,
            "level_index": self.level_index,
            "last_sleep_at": self.last_sleep_at,
            "last_inject_at": self.last_inject_at,
            "last_inject_level": self.last_inject_level,
            "sleep_armed": self.sleep_armed,
            "forced_at": self.forced_at,
            "clock_skew_warned": self.clock_skew_warned,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "LifecycleState":
        st = cls()
        for k, v in (d or {}).items():
            if hasattr(st, k) and v is not None:
                setattr(st, k, v)
        return st


class LifecyclePolicy:
    """把 uptime 映射为等级，并决定本轮动作。纯函数区可独立单测。"""

    def __init__(self, levels: list[LevelSpec], max_uptime_hours: float = 720.0,
                 force_on_deadline: bool = True, inject_cooldown_sec: int = 900) -> None:
        self.levels: list[LevelSpec] = sorted(levels, key=lambda x: x.after_hours) or [
            LevelSpec("L0", "常规", 0.0, "", "none")
        ]
        self.max_uptime_hours = float(max_uptime_hours)
        self.force_on_deadline = bool(force_on_deadline)
        self.inject_cooldown_sec = int(inject_cooldown_sec)

    # ---------------- 纯函数区 ---------------- #
    def resolve_level(self, uptime_hours: float) -> tuple[int, LevelSpec]:
        idx = 0
        for i, spec in enumerate(self.levels):
            if uptime_hours >= spec.after_hours:
                idx = i
        return idx, self.levels[idx]

    def next_level(self, idx: int) -> LevelSpec | None:
        return self.levels[idx + 1] if idx + 1 < len(self.levels) else None

    def remaining_to_next(self, uptime_hours: float, idx: int) -> float | None:
        nxt = self.next_level(idx)
        if nxt is None:
            return None
        return max(0.0, nxt.after_hours - uptime_hours)

    def remaining_to_deadline(self, uptime_hours: float) -> float:
        return max(0.0, self.max_uptime_hours - uptime_hours)

    def render_prompt(self, spec: LevelSpec, uptime_hours: float, idx: int,
                      last_sleep_at: float = 0.0) -> str:
        nxt = self.next_level(idx)
        mapping = {
            "{uptime_hours}": f"{uptime_hours:.1f}",
            "{uptime_days}": f"{uptime_hours / 24:.1f}",
            "{level_name}": spec.name,
            "{next_level_hours}": f"{nxt.after_hours:.0f}" if nxt else "-",
            "{remaining_hours}": f"{self.remaining_to_deadline(uptime_hours):.1f}",
            "{last_sleep_at}": (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(last_sleep_at))
                if last_sleep_at else "从未"
            ),
        }
        text = spec.prompt or ""
        for k, v in mapping.items():
            text = text.replace(k, v)
        return text

    # ---------------- 决策区 ---------------- #
    def evaluate(self, uptime_hours: float, st: LifecycleState,
                 now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        idx, spec = self.resolve_level(uptime_hours)

        st.uptime_hours = uptime_hours
        st.level_index = idx
        st.level_id = spec.id
        st.level_name = spec.name

        action = spec.action or "none"
        reason = f"uptime={uptime_hours:.1f}h -> {spec.id}({spec.name})"

        force_sleep = False
        if self.force_on_deadline and uptime_hours >= self.max_uptime_hours:
            force_sleep = True
            action = "force_sleep"
            reason += " | deadline reached"

        inject_prompt = None
        if spec.prompt:
            same_level = st.last_inject_level == spec.id
            cooled = (now - st.last_inject_at) >= self.inject_cooldown_sec
            if (not same_level) or cooled:
                inject_prompt = self.render_prompt(spec, uptime_hours, idx, st.last_sleep_at)
                st.last_inject_at = now
                st.last_inject_level = spec.id

        if action == "arm_sleep":
            st.sleep_armed = True

        return {
            "level_index": idx,
            "level": spec,
            "inject_prompt": inject_prompt,
            "action": action,
            "force_sleep": force_sleep,
            "reason": reason,
            "remaining_to_next": self.remaining_to_next(uptime_hours, idx),
            "remaining_to_deadline": self.remaining_to_deadline(uptime_hours),
        }

    def on_sleep_done(self, st: LifecycleState, now: float | None = None) -> None:
        st.last_sleep_at = time.time() if now is None else now
        st.sleep_armed = False
        st.forced_at = 0.0
        st.last_inject_at = 0.0
        st.last_inject_level = ""
        st.uptime_hours = 0.0
        st.level_index = 0
        st.level_id = self.levels[0].id
        st.level_name = self.levels[0].name

    def describe(self, st: LifecycleState) -> str:
        nxt = self.next_level(st.level_index)
        nxt_txt = (f"{nxt.name}（还有 {self.remaining_to_next(st.uptime_hours, st.level_index):.1f}h）"
                   if nxt else "已是最高等级")
        return (
            f"运行时长 {st.uptime_hours:.1f}h（{st.uptime_hours / 24:.1f} 天） | "
            f"等级 {st.level_id} {st.level_name} | 下一级 {nxt_txt} | "
            f"距硬上限 {self.remaining_to_deadline(st.uptime_hours):.1f}h | "
            f"武装睡眠 {'是' if st.sleep_armed else '否'}"
        )


def build_policy(cfg) -> LifecyclePolicy:
    """从 GuardianConfig 构造 LifecyclePolicy。"""
    levels = [LevelSpec.from_dict(x) for x in (cfg.lifecycle.levels or [])]
    return LifecyclePolicy(
        levels=levels,
        max_uptime_hours=cfg.lifecycle.max_uptime_hours,
        force_on_deadline=cfg.lifecycle.force_sleep_on_deadline,
        inject_cooldown_sec=cfg.lifecycle.inject_cooldown_sec,
    )
