"""Guardian 守护系统 —— 生命周期等级与自主睡眠策略。

对应文档：B15

核心原则（P1，不可协商）：
- 绝不匹配对话关键词。等级推进只依赖容器运行时长。
- 每级注入不同提示词，引导 Bot 自主决定睡眠。
- `max_uptime_hours` 为硬上限，到达即强制睡眠（除非显式关闭）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class LevelSpec:
    id: str = "L0"
    name: str = "常规"
    after_hours: float = 0.0
    prompt: str = ""
    action: str = "none"  # none | notify | arm_sleep | force_sleep

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LevelSpec:
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
    #: 首次"想睡但被窗口挡住"的时刻。用于 `max_defer_hours` 顺延上限：
    #: 从这一刻起算，超过上限就无视窗口强制入睡。0 表示当前没有顺延。
    deferred_since: float = 0.0
    #: 困意注入被窗口抑制到此刻（回落到窗口开启时刻，或窗口结束）。
    #: **只影响提示词注入**，不影响 `level_index` / `sleep_armed`——等级与
    #: 武装状态永远由 uptime 决定，窗口无权改写它们。
    suppressed_until: float = 0.0
    #: 最近一次"已经说出口"的主动消息标识。
    #:
    #: 这里刻意**不做频率限制**：Bot 说完就去睡了，睡下之后不可能再发第二条，
    #: 冷却窗口在本场景里是多余的复杂度。真正要防的是"同一件事说两遍"：
    #: 插件重载、内核重启、轮询撞车、或者 L3 报备完之后管理员回了句话
    #: 导致 `recent_talk` 把睡眠推后、下一轮又想报备一次。
    #:
    #: 因此用"事件键"而不是"时间窗口"：键相同就说明是同一个事件，不再重复。
    #: 键形如 `sleep:L3:600.0`（等级 + 触发时的整点 uptime）、`deferred:L3`、
    #: `resume:1729260000`（上次入睡时刻）。
    announced_key: str = ""
    announced_at: float = 0.0

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
            "deferred_since": self.deferred_since,
            "suppressed_until": self.suppressed_until,
            "announced_key": self.announced_key,
            "announced_at": self.announced_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> LifecycleState:
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

        # 用户已批准的语义（B15）：被选中的等级若 action="force_sleep"，该等级即为权威，
        # 一旦 uptime 达到该等级阈值就立即强制睡眠——不必等到 max_uptime_hours。
        #
        # 注意这**不绕过任何安全门**：白名单、每小时配额、看门狗待确认归属、本体容器
        # 权限，全部仍由 SafetyGuard 在真正执行前判定。"权威"只表示策略层不再压着
        # 不发指令，而不是说策略层可以越过安全层。
        #
        # force_on_deadline 只管辖"硬上限"这一条兜底路径，与等级自身的 action 无关：
        # 用户把某等级配成 force_sleep 是显式意图，不应被 deadline 开关关掉。
        force_source = ""
        force_sleep = action == "force_sleep"
        if force_sleep:
            force_source = "level"
            reason += " | level action=force_sleep"
            if not st.forced_at:
                # 记录本次强制义务的起始时刻；on_sleep_done 会清零。
                st.forced_at = now

        # 硬上限兜底：即便最高等级没有配 force_sleep，到达 max_uptime_hours 也必须强制。
        if self.force_on_deadline and uptime_hours >= self.max_uptime_hours:
            force_sleep = True
            action = "force_sleep"
            force_source = "deadline"
            if not st.forced_at:
                st.forced_at = now
            reason += " | deadline reached"

        inject_prompt = None
        if spec.prompt:
            same_level = st.last_inject_level == spec.id
            cooled = (now - st.last_inject_at) >= self.inject_cooldown_sec
            if (not same_level) or cooled:
                inject_prompt = self.render_prompt(spec, uptime_hours, idx, st.last_sleep_at)
                st.last_inject_at = now
                st.last_inject_level = spec.id

        # 武装状态**每轮按当前等级重算**，不做单向闩锁。
        #
        # 曾经这里是 `if action == "arm_sleep": st.sleep_armed = True`，只置真不置假：
        # 一旦某轮命中 arm_sleep 等级，此后即使 uptime 回落（某个 uptime_targets
        # 容器被重启 / 时钟校正）让等级降回 L0，武装状态仍然为真。而适配层把
        # `sleep_armed` 直接当作"权威路径"，会同时跳过管理员校验与二次确认——
        # 于是一张"免确认通行证"会在等级早已回落后长期有效。改为每轮重算即可根除。
        st.sleep_armed = action == "arm_sleep"

        return {
            "level_index": idx,
            "level": spec,
            "inject_prompt": inject_prompt,
            "action": action,
            "force_sleep": force_sleep,
            # "" | "level" | "deadline"：强制睡眠的**来源**。日志与审计必须能区分
            # "某等级自己配了 force_sleep" 和 "到达 30 天硬上限"，否则运维无法
            # 从记录里判断到底是哪条策略在要求睡眠。
            "force_source": force_source,
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
        # 睡过一觉，顺延与抑制都结清了：下一次"想睡"重新从零开始计时。
        st.deferred_since = 0.0
        st.suppressed_until = 0.0
        # `announced_key` **不清**：醒来后的"我回来了"要靠它跟睡前那条
        # 区分成两个事件（键不同），而同一个轮次重复上报则靠它去重。
        # 清掉反而会让 sleep 那条在醒来瞬间被重发一次。

    def describe(self, st: LifecycleState) -> str:
        nxt = self.next_level(st.level_index)
        nxt_txt = (f"{nxt.name}（还有 {self.remaining_to_next(st.uptime_hours, st.level_index):.1f}h）"
                   if nxt else "已是最高等级")
        # 强制义务是"已发出但尚未由一次成功睡眠结清"的状态，必须能被运维看见，
        # 否则自定义 force_sleep 等级触发了什么、什么时候触发的都无从追溯。
        forced_txt = ""
        if st.forced_at:
            forced_txt = (" | 强制义务始于 "
                          + time.strftime("%Y-%m-%d %H:%M", time.localtime(st.forced_at)))
        return (
            f"运行时长 {st.uptime_hours:.1f}h（{st.uptime_hours / 24:.1f} 天） | "
            f"等级 {st.level_id} {st.level_name} | 下一级 {nxt_txt} | "
            f"距硬上限 {self.remaining_to_deadline(st.uptime_hours):.1f}h | "
            f"武装睡眠 {'是' if st.sleep_armed else '否'}{forced_txt}"
        )


def drowsy_visible(level_action: str, in_asleep_window: bool,
                   uptime_hours: float, l1_threshold: float) -> bool:
    """Bot 此刻**该不该表现出困意**（纯函数，无 IO）。

    这是"人生理节律"的全部对外可见部分，刻意做得极窄：用户明确要求
    「生物钟现阶段可以不表现出来，或者只有 asleep 窗口期间 bot 才会
    感觉有困意想去睡觉」，以及「每天内语气先不变化，粒度太小」。

    因此三条同时成立才给真：

    1. 等级本身已经进入"想睡"区（`action` 为 `arm_sleep` / `force_sleep`）——
       困意是 uptime 的函数，不是时钟的函数；
    2. 此刻确实落在作息窗口内——窗口是唯一让它"说出困"的许可；
    3. 已越过 L1 阈值——避免刚睡醒就喊困。

    第 1 条是关键：**窗口本身永远不能让 Bot 变困**。否则一到 02:00 就算
    刚重启完 3 分钟也会开始说困，那是定时任务，不是生物钟。
    """
    if level_action not in ("arm_sleep", "force_sleep"):
        return False
    if not in_asleep_window:
        return False
    return float(uptime_hours) >= float(l1_threshold)


def build_policy(cfg) -> LifecyclePolicy:
    """从 GuardianConfig 构造 LifecyclePolicy。"""
    levels = [LevelSpec.from_dict(x) for x in (cfg.lifecycle.levels or [])]
    return LifecyclePolicy(
        levels=levels,
        max_uptime_hours=cfg.lifecycle.max_uptime_hours,
        force_on_deadline=cfg.lifecycle.force_sleep_on_deadline,
        inject_cooldown_sec=cfg.lifecycle.inject_cooldown_sec,
    )
