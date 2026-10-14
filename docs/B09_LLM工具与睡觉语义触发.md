# B09 · LLM 工具与「睡觉」语义触发

> 上位规格：`B00_项目总览与需求规格.md`（唯一权威）。实现 B00 §5（档位与「睡觉」闭环）、§8（LLM 工具契约）、§9（命令契约）、§10（安全红线）、§13（Gate `G-LLM`/`G-SAFETY`）。关联 B03（引擎）、B08（通知）、B10（调度器）、B11（状态与审计）、B12（安全）、C09（测试）。产出：`tools/llm_tools.py`、`main.py` 的 `@filter.llm_tool()` 方法组与 `@filter.on_llm_request()` 注入。版本 v0.1.0，日期 2026-10-13。

> **v0.2.0 增量（必读，覆盖本文的「睡觉」语义实现）**：B00 v0.2 明确三条约束——
> ① **禁止关键词触发**：插件层绝不匹配"睡觉/晚安"等词；触发源仅调度器巡检 / 生命周期等级推进 / Bot 主动调用工具；
> ② 「睡觉」由**生命周期等级**推进：等级阈值前端可配（`lifecycle.levels`），每级注入**不同提示词**引导 Bot 自主决定；
> ③ 工具集调整：删除 `guardian_enter_night_watch`，新增 `guardian_request_sleep(reason, duration_min)` 与 `guardian_lifecycle_status()`；30 天硬上限由插件强制睡眠兜底。
> 提示词注入改用 `@filter.on_llm_request()` 的 `extra_user_content_parts`（不要写 system_prompt）。完整设计见 `B15`。

## 1. 目标

| ID | 目标 | 判据 |
| --- | --- | --- |
| T-G1 | 8 个工具与 B00 §8 契约**逐字一致** | 工具名/参数/类型/权限比对通过（C09-1） |
| T-G2 | 「我要睡觉了」等自然语言稳定触发夜间档位 | 5 组触发词均命中 `guardian_enter_night_watch`（C09-6） |
| T-G3 | 破坏性工具在非管理员下被拒且可读 | `event.is_admin()` 为假时返回拒绝文案（C09-9） |
| T-G4 | LLM 幻觉出的非法参数被运行时白名单拦下 | 非法容器名/枚举值被拒并回执（C09-10） |
| T-G5 | 工具与命令复用同一 core 逻辑 | 不存在第二份 `restart_container` 实现（C09-12） |
| T-G6 | 工具调用可观测、可审计 | 每次调用写 `llm.tool_call`/`llm.tool_respond` 审计（C09-13） |
| T-G7 | 工具异常不把栈抛给 LLM | 任何异常均转可读回执（C09-14） |

非目标：不做多轮工具编排（由 LLM 自行决定）；不实现任意 shell 工具（B00 §10-1 红线）。

## 2. LLM 工具清单（与 B00 §8 完全一致）

| 工具名 | 参数 | 参数细节 | 副作用 | 权限 |
| --- | --- | --- | --- | --- |
| `guardian_health_report` | `scope`(all\|containers\|qq\|ros2) | 缺省 `all` | 只读 `engine.snapshot`，**绕过 Notifier**（B08 §10），不动跃迁基线 | 所有用户 |
| `guardian_run_check` | `scope` | 缺省 `all` | `run_once(trigger="llm")`；动作受 `SafetyGuard` 约束；在途巡检回执「稍后再试」 | 所有用户（动作受约束） |
| `guardian_diagnose` | `target`(枚举) | 白名单容器 ∪ `qq` ∪ `docker` ∪ ROS2 期望节点 | 只读诊断，不改状态 | 所有用户 |
| `guardian_list_incidents` | `limit`(1-50) | 缺省 10 | 只读环形缓冲，含被静默/抑制事件 | 所有用户 |
| `guardian_restart_container` | `container`(枚举=白名单) | `managed ∪ allowed_containers`；`confirm` 二次确认 | **重启容器**（dry-run 仅记录） | 管理员 + 确认 |
| `guardian_set_mode` | `mode`(server\|ros2) | — | `switch_mode`：停表→等在途→重建探针→重置冷却→落盘→通知 | 管理员 |
| `guardian_set_profile` | `profile`(枚举) | normal/night/maintenance/aggressive | `set_profile`：重排 interval + 静默级别 | 管理员 |
| `guardian_enter_night_watch` | `reason`(string), `hours`(number) | `reason` 截断 120 字；`hours` 1-24 缺省 8 | 切 `night` + 写 `night_until` + 预约维护重启 + 静默低级别告警 | 所有用户（低风险自愈） |

> **关键约束**：`container` 与 `target` 在 docstring 中**枚举化**，运行时**再次校验白名单**；非白名单一律拒绝并回执说明（B00 §8 末段）。

## 3. `@filter.llm_tool()` 写法详解

AstrBot 在**加载期**解析被装饰方法的 docstring，把 `Args:` 段逐行 `name(type): desc` 转换为 JSON schema。

| 规则 | 说明 | 示例 |
| --- | --- | --- |
| 参数行格式 | `name(type): desc` | `scope(string): 查询范围` |
| 支持类型 | `string`/`number`/`object`/`boolean`/`array`/`array[string]` | `hours(number)`、`confirm(boolean)` |
| 缺省值/必填 | 由函数签名默认值决定（**不是** docstring）；无默认值即 `required` | `hours: float = 8.0` → `default=8.0`；`target: str = ""` 可选 |
| 描述即约束 | 枚举/范围只能写进描述文本（软约束） | `取值 all/containers/qq/ros2` |
| 返回值 | async generator，`yield event.plain_result(text)` | 文本进入 LLM 上下文 |
| `name` 缺省 | 不带 `name` 时取**函数名** | `@filter.llm_tool()` → 工具名=函数名 |

**枚举收口的两层设计**：docstring 是静态字符串，无法在加载期拼接动态白名单，故 ① 描述里显式列出常见枚举值降低幻觉；② 方法体内用常量元组 `SCOPE_VALUES`/`MODE_VALUES`/`PROFILE_VALUES` 与 `engine.allowed_containers()` 复核，非法即拒。需**强类型枚举 schema** 时改用高级工具 `FunctionTool`（§6.3）。

**返回值约定**：成功 → 人类可读文本；参数非法 → `[Guardian] 参数 X 非法，允许值：...`；权限不足 → `[Guardian] 操作被拒绝：...`；内部异常 → `logger.exception` + `[Guardian] ...失败：<类型>: <消息>`。工具**不得**直接 `await self.context.send_message(...)`（回执走 event，主动推送走 Notifier）。

## 4. `tools/llm_tools.py` 完整代码

```python
# tools/llm_tools.py
"""Guardian LLM 工具层：8 个 @filter.llm_tool() 方法 + 注册入口。

约定（B00 §8/§10）：工具为 async generator，成功与失败都经 yield event.plain_result(text)
回执；docstring 的 Args: 段仅作软约束，硬约束在方法体内；破坏性工具先 _require_admin() 鉴权，
再 engine.request_action() 过 SafetyGuard；全部复用 core 层，命令与工具共用同一实现。
"""
from __future__ import annotations
from typing import Any
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from ..core.models import ActionSpec

SCOPE_VALUES = ("all", "containers", "qq", "ros2")
MODE_VALUES = ("server", "ros2")
PROFILE_VALUES = ("normal", "night", "maintenance", "aggressive")
NIGHT_HOURS_MIN, NIGHT_HOURS_MAX, NIGHT_HOURS_DEFAULT = 1.0, 24.0, 8.0
PROFILE_ZH = {"normal": "常规", "night": "夜间", "maintenance": "维护", "aggressive": "激进"}


class GuardianLLMTools:
    """混入类：由 GuardianPlugin(Star) 继承。依赖 self.engine/state/audit/scheduler/config。"""
    @staticmethod
    def _dig(cfg: Any, path: str, default: Any = None) -> Any:
        """按点号路径读配置，兼容 dict 与属性访问，缺失即回落默认值。"""
        cur = cfg
        for part in path.split("."):
            if cur is None:
                return default
            cur = cur.get(part) if hasattr(cur, "get") else getattr(cur, part, None)
        return default if cur is None else cur
    @staticmethod
    def _actor(event: AstrMessageEvent) -> str:
        """审计主体：sender_id 脱敏为 u:***XXXX（B11 §9）。"""
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sid = ""
        return f"u:***{sid[-4:]}" if sid else "u:unknown"
    @staticmethod
    def _bad_arg(event: AstrMessageEvent, name: str, value: Any, allowed: tuple):
        return event.plain_result(
            f"[Guardian] 参数 {name}={value!r} 非法，允许值：{' / '.join(allowed)}")

    async def _deny(self, event: AstrMessageEvent, tool: str, reason: str):
        await self.audit.append(kind="llm.tool_denied", actor=self._actor(event),
                                trigger="llm", target=tool, detail=reason, ok=False)
        return event.plain_result(f"[Guardian] 操作被拒绝：{reason}")

    async def _require_admin(self, event: AstrMessageEvent, tool: str):
        """破坏性工具鉴权；通过返回 None，否则返回一条拒绝回执。"""
        if not self._dig(self.config, "llm_tools.require_admin", True):
            return None
        if event.is_admin():
            return None
        return await self._deny(event, tool, "该工具仅管理员可用（llm_tools.require_admin=true）")
    @filter.llm_tool(name="guardian_health_report")
    async def guardian_health_report(self, event: AstrMessageEvent, scope: str = "all"):
        """查询当前健康快照（只读，不触发巡检、不推送通知）。
        Args:
            scope(string): 查询范围，取值 all/containers/qq/ros2，默认 all
        """
        try:
            if scope not in SCOPE_VALUES:
                yield self._bad_arg(event, "scope", scope, SCOPE_VALUES); return
            snap = await self.engine.snapshot(scope)          # 只读，绕过 Notifier（B08 §10）
            yield event.plain_result(self.render_health(snap))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[guardian] guardian_health_report 异常")
            yield event.plain_result(f"[Guardian] 查询失败：{type(exc).__name__}: {exc}")
    @filter.llm_tool(name="guardian_run_check")
    async def guardian_run_check(self, event: AstrMessageEvent, scope: str = "all"):
        """立即执行一轮健康巡检；可能触发自动修复动作（仍受 SafetyGuard 约束）。
        Args:
            scope(string): 巡检范围，取值 all/containers/qq/ros2，默认 all
        """
        try:
            if scope not in SCOPE_VALUES:
                yield self._bad_arg(event, "scope", scope, SCOPE_VALUES); return
            if getattr(self.engine, "busy", False):
                yield event.plain_result("[Guardian] 上一轮巡检尚未结束，请稍后再试。"); return
            incidents = await self.engine.run_once(trigger="llm", scope=scope)
            yield event.plain_result(self.render_scan(incidents))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[guardian] guardian_run_check 异常")
            yield event.plain_result(f"[Guardian] 巡检失败：{type(exc).__name__}: {exc}")
    @filter.llm_tool(name="guardian_diagnose")
    async def guardian_diagnose(self, event: AstrMessageEvent, target: str = ""):
        """对单个目标做深度诊断（只读），如容器 astrbot/napcat、qq、docker 或 ROS2 节点名。
        Args:
            target(string): 诊断目标，取值 = 白名单容器名 / qq / docker / ROS2 节点名
        """
        try:
            targets = self.engine.diagnosable_targets()       # 白名单并集（含 qq/docker）
            if target not in targets:
                yield self._bad_arg(event, "target", target, tuple(sorted(targets))); return
            yield event.plain_result(await self.engine.diagnose(target, actor=self._actor(event)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[guardian] guardian_diagnose 异常")
            yield event.plain_result(f"[Guardian] 诊断失败：{type(exc).__name__}: {exc}")
    @filter.llm_tool(name="guardian_list_incidents")
    async def guardian_list_incidents(self, event: AstrMessageEvent, limit: int = 10):
        """查询最近的守护事件历史（只读，含被静默/抑制的事件）。
        Args:
            limit(number): 返回条数，取值 1-50，默认 10
        """
        try:
            try:
                n = int(limit)
            except (TypeError, ValueError):
                yield self._bad_arg(event, "limit", limit, ("1-50",)); return
            if not 1 <= n <= 50:
                yield self._bad_arg(event, "limit", limit, ("1-50",)); return
            yield event.plain_result(self.render_incidents(self.state.recent_incidents(n)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[guardian] guardian_list_incidents 异常")
            yield event.plain_result(f"[Guardian] 查询失败：{type(exc).__name__}: {exc}")
    @filter.llm_tool(name="guardian_restart_container")
    async def guardian_restart_container(self, event: AstrMessageEvent,
                                         container: str = "", confirm: bool = False):
        """重启一个受守护的 Docker 容器（破坏性操作，仅管理员）。

        当用户明确要求重启/拉起/恢复某个容器时调用。容器名必须属于当前白名单，
        非白名单目标一律拒绝。首次调用（confirm=false）只返回确认提示，不执行动作。
        Args:
            container(string): 目标容器名，必须属于白名单（containers.managed ∪ safety.allowed_containers），常见值：astrbot/napcat
            confirm(boolean): 二次确认标志，true 才真正执行，默认 false
        """
        try:
            denied = await self._require_admin(event, "guardian_restart_container")
            if denied is not None:
                yield denied; return
            allowed = self.engine.allowed_containers()        # 运行时白名单复核（权威）
            if container not in allowed:
                yield await self._deny(
                    event, "guardian_restart_container",
                    f"容器 {container!r} 不在白名单内，允许：{'/'.join(sorted(allowed)) or '（空）'}")
                return
            if not confirm:
                yield event.plain_result(
                    f"[Guardian] 确认执行？将重启容器 {container}。\n"
                    f"请再次调用并置 confirm=true；当前 dry_run="
                    f"{self._dig(self.config, 'dry_run', True)}。")
                return
            spec = ActionSpec(kind="restart_container", target=container,
                              params={"reason": "llm_tool", "timeout": 10})
            res = await self.engine.request_action(spec, trigger="llm", actor=self._actor(event))
            yield event.plain_result(
                f"[Guardian] 重启 {container} -> {'成功' if res.ok else '失败'}"
                f"（{res.elapsed_ms}ms）\n{res.detail}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[guardian] guardian_restart_container 异常")
            yield event.plain_result(f"[Guardian] 重启失败：{type(exc).__name__}: {exc}")
    @filter.llm_tool(name="guardian_set_mode")
    async def guardian_set_mode(self, event: AstrMessageEvent, mode: str = ""):
        """切换 Guardian 守护模式（server=容器/QQ，ros2=节点/话题）。仅管理员。
        Args:
            mode(string): 守护模式，取值 server/ros2
        """
        try:
            denied = await self._require_admin(event, "guardian_set_mode")
            if denied is not None:
                yield denied; return
            if mode not in MODE_VALUES:
                yield self._bad_arg(event, "mode", mode, MODE_VALUES); return
            await self.engine.switch_mode(mode)               # 与 /guardian mode 同一实现
            yield event.plain_result(f"[Guardian] 守护模式已切换为 {mode}。")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[guardian] guardian_set_mode 异常")
            yield event.plain_result(f"[Guardian] 切换模式失败：{type(exc).__name__}: {exc}")
    @filter.llm_tool(name="guardian_set_profile")
    async def guardian_set_profile(self, event: AstrMessageEvent, profile: str = ""):
        """切换 Guardian 守护档位（normal/night/maintenance/aggressive）。仅管理员。
        Args:
            profile(string): 守护档位，取值 normal/night/maintenance/aggressive
        """
        try:
            denied = await self._require_admin(event, "guardian_set_profile")
            if denied is not None:
                yield denied; return
            if profile not in PROFILE_VALUES:
                yield self._bad_arg(event, "profile", profile, PROFILE_VALUES); return
            await self.engine.set_profile(profile, actor=self._actor(event))
            yield event.plain_result(
                f"[Guardian] 守护档位已切换为 {profile}（{PROFILE_ZH.get(profile, profile)}）。")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[guardian] guardian_set_profile 异常")
            yield event.plain_result(f"[Guardian] 切换档位失败：{type(exc).__name__}: {exc}")
    @filter.llm_tool(name="guardian_enter_night_watch")
    async def guardian_enter_night_watch(self, event: AstrMessageEvent,
                                         reason: str = "用户休息",
                                         hours: float = NIGHT_HOURS_DEFAULT):
        """进入夜间守护档位：切 night + 静默低级别告警 + 预约凌晨维护重启。

        当用户表达休息、睡觉、离开、夜间、晚安等意图时调用，例如「我要睡觉了」「晚安」
        「我休息了」「去睡了」「先睡了」「下线了」。无需管理员权限。
        Args:
            reason(string): 进入夜间的原因，写入回执与审计，默认「用户休息」
            hours(number): 夜间持续小时数，取值 1-24，默认 8
        """
        try:
            try:
                h = float(hours)
            except (TypeError, ValueError):
                yield self._bad_arg(event, "hours", hours, ("1-24",)); return
            if not NIGHT_HOURS_MIN <= h <= NIGHT_HOURS_MAX:
                yield self._bad_arg(event, "hours", hours, ("1-24",)); return
            text = await self.engine.enter_night_watch(
                reason=str(reason)[:120], hours=h, actor=self._actor(event))
            yield event.plain_result(text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[guardian] guardian_enter_night_watch 异常")
            yield event.plain_result(f"[Guardian] 进入夜间守护失败：{type(exc).__name__}: {exc}")


def register_llm_tools(context, plugin) -> None:
    """加载期注册：混入方法由 @filter.llm_tool() 自动挂载，这里处理开关与高级工具。"""
    if not plugin._dig(plugin.config, "llm_tools.enabled", True):
        logger.info("[guardian] llm_tools.enabled=false，跳过工具注册"); return
    logger.info("[guardian] LLM 工具已注册：8 个（含 guardian_enter_night_watch）")
```

> `render_health`/`render_scan`/`render_incidents` 为插件侧渲染助手（复用 B08 §5「首行结论 + 明细 + 动作/抑制」模板），置于 `utils/textutil.py`，命令与工具共用。

## 5. 「睡觉」语义的完整闭环设计

| 触发词 | 语义 | 期望调用 |
| --- | --- | --- |
| 我要睡觉了 | 明确休息 | `guardian_enter_night_watch(reason="我要睡觉了", hours=8)` |
| 晚安 | 夜间告别 | 同上，`hours=8` |
| 我休息了 | 休息 | 同上 |
| 去睡了 / 先睡了 | 即将离开 | 同上，`hours` 可为 6 |
| 下线了 | 离开 | 同上，`hours` 默认 |

**工具 description 必须显式写明**（§4 已落实）——这是 LLM 选择工具的唯一依据，不写就没有触发率：

```
当用户表达休息、睡觉、离开、夜间、晚安等意图时调用，例如「我要睡觉了」「晚安」
「我休息了」「去睡了」「先睡了」「下线了」。无需管理员权限。
```

参数：`reason`(string，截断 120 字，缺省「用户休息」，写入回执与审计)；`hours`(number，1-24，缺省 8，用于计算 `night_until` 与 once 任务触发时刻)。执行内容（四件事，一次完成）：

```
用户「我要睡觉了」
  └─ LLM 调用 guardian_enter_night_watch(reason, hours)
       └─ engine.enter_night_watch(reason, hours, actor)      # 复用 /guardian night 同一实现
            1) scheduler.apply_profile("night")  ├─ 取消 patrol(60s) → 新建 patrol(180s)
                                                 └─ add_cron_job("night_restart", maintenance_restart_cron)
            2) state.update(night_until=now+hours)            # 落盘，跨重启保持
            3) add_once_job("night_expire", at=night_until)   # 到期回落 normal（B10 §6）
            4) bus.publish("profile.changed", "night")        # Notifier 推送回执
       └─ 静默：Notifier 在 night 档位静默低于 mute_below_severity（默认 warn）的告警
```

**回执文案**：

```
[Guardian] 已进入夜间守护（8.0h）
- 档位: normal -> night（巡检间隔 180s）  - 静默: 低于 warn 的告警不再推送
- 维护重启: 已预约 0 4 * * *  - 到期回落: 2026-10-14 07:30（night -> normal）  - 晚安，我会看着它。
```

`night_until` 到期由 `_night_expire` 处理：**仅当当前档位仍是 `night`** 才回落 `normal`（用户期间手动切 `aggressive` 则不动），随后推送「夜间守护已结束，回落 normal 档位」（B10 §6）。

## 6. 可选增强

### 6.1 `@filter.on_llm_request()` 注入动态上下文

**原则：每轮变化的内容不要放进 `system_prompt`**（破坏前缀缓存、易被污染），改用 `extra_user_content_parts`。

```python
# main.py —— 每次 LLM 请求前注入一行「守护状态」上下文
@filter.on_llm_request()
async def _inject_guardian_context(self, event: AstrMessageEvent, req) -> None:
    try:
        snap = await self.engine.snapshot("all")
        line = (f"[Guardian 实时状态] mode={snap.get('mode')} profile={snap.get('profile')} "
                f"dry_run={snap.get('dry_run')} night_until={snap.get('night_until') or '-'}")
        req.extra_user_content_parts.append(Plain(line))   # 追加用户片段，保持 system_prompt 稳定
    except Exception:  # noqa: BLE001 上下文注入失败绝不影响对话
        logger.debug("[guardian] 注入守护上下文失败", exc_info=True)
```

### 6.2 破坏性工具二次确认流程

```
require_admin（event.is_admin()） ├─ 否 → 拒绝回执 + 审计 llm.tool_denied
  └─ 是 → 白名单复核 ├─ 否 → 拒绝回执（含允许清单）
                    └─ 是 → confirm? ├─ false → 「请再次调用并置 confirm=true；当前 dry_run=X」
                                     └─ true  → engine.request_action() → SafetyGuard.authorize()
                                                  ├─ deny → 回执拒绝原因（冷却/熔断/dry-run/受保护）
                                                  └─ allow → 执行 → 回执 ok/elapsed_ms/detail
```

**dry-run 提示话术**：`dry_run=true` 时动作仅记录不执行，回执须显式说明，避免用户误以为已生效。

### 6.3 工具与命令复用同一 core 逻辑

命令与工具**必须**共用同一实现：健康快照 → `engine.snapshot` + `textutil.render_health`；立即巡检 → `engine.run_once`；重启容器 → `engine.request_action`；切模式/档位 → `engine.switch_mode`/`set_profile`；夜间守护 → `engine.enter_night_watch`。**禁止**在 `llm_tools.py` 复制 `engine`/`safety` 逻辑，工具层只做「参数解析 → 鉴权 → 调 core → 渲染回执」。

需**强类型枚举 schema** 时用高级工具（加载期用 `engine.allowed_containers()` 动态填充 `enum`）：

```python
from dataclasses import dataclass, field
from astrbot.api.event import AstrAgentContext
from astrbot.api.tool import FunctionTool
from astrbot.api.star import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult


@dataclass
class GuardianRestartTool(FunctionTool[AstrAgentContext]):
    """enum 由加载期用 engine.allowed_containers() 填充。"""
    plugin: object = None
    name: str = "guardian_restart_container"
    description: str = "重启受守护的 Docker 容器（仅管理员，需 confirm=true）"
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {
        "container": {"type": "string", "enum": [], "description": "白名单容器名"},
        "confirm": {"type": "boolean", "default": False}}, "required": ["container"]})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        ok, text = await self.plugin.core_restart(kwargs.get("container", ""),
                                                  bool(kwargs.get("confirm")))
        return ToolExecResult(ok=ok, message=text)
```

注册：`context.add_llm_tools(GuardianRestartTool(plugin=plugin))`（加载期完成）。

### 6.4 LLM 幻觉防护

| 防线 | 机制 | 失败表现 |
| --- | --- | --- |
| 枚举参数 | docstring 描述列出取值域 | LLM 少写非法值 |
| 运行时白名单复核 | `engine.allowed_containers()` / 常量元组 | 非法值被拒 |
| 权限 | `event.is_admin()` | 非管理员全拒 |
| 安全链 | `SafetyGuard.authorize` 冷却/熔断/dry-run | 高频/危险动作被抑制 |
| 无自由文本 | 参数无 `command`/`shell` 字段 | 无法注入 |

非法参数回执模板：`[Guardian] 参数 {name}={value!r} 非法，允许值：{v1} / {v2} / ...`；`[Guardian] 操作被拒绝：容器 'redis' 不在白名单内，允许：napcat / postgres`。

### 6.5 工具调用可观测（审计）

```python
# main.py —— 工具调用前/后各写一条审计（B11 core/audit.py）
@filter.on_using_llm_tool()
async def _on_tool_use(self, event: AstrMessageEvent, tool_name: str, tool_args: dict) -> None:
    if str(tool_name).startswith("guardian_"):
        await self.audit.append(kind="llm.tool_call", actor=self._actor(event), trigger="llm",
                                target=tool_name, params=tool_args, ok=True)

@filter.on_llm_tool_respond()
async def _on_tool_resp(self, event: AstrMessageEvent, tool_name: str, response) -> None:
    if str(tool_name).startswith("guardian_"):
        await self.audit.append(kind="llm.tool_respond", actor=self._actor(event), trigger="llm",
                                target=tool_name, result=str(response)[:500], ok=True)
```

> 审计字段与 `core/audit.py` 一致（`ts/actor/trigger/probe/target/action/params/result/ok/elapsed_ms/suppressed/reason`）；`params` 中若含 token 由审计层统一脱敏（B11 §9）。钩子形参名以实际框架版本为准。

### 6.6 典型对话示例

```
① 睡觉场景   用户：我要睡觉了
  Bot ：已进入夜间守护（8.0h）normal->night（180s）| 静默 warn 以下 | 维护重启预约 0 4 * * *
        | 到期回落 2026-10-14 07:30 | 晚安，我会看着它。
② 查询健康   用户：现在服务器还好吗
  Bot ：健康快照 astrbot 运行中 mem=312MB uptime=3d04h | napcat 运行中 mem=488MB | qq 在线 42ms
        | docker 可达 v1.44 | 档位 normal / dry_run=true
③ 重启被拒   用户：帮我重启一下 napcat → Bot：操作被拒绝：该工具仅管理员可用（require_admin=true）
④ 非法容器名 用户：重启 redis → Bot：操作被拒绝：容器 'redis' 不在白名单内，允许：napcat / postgres
⑤ 夜间到期   Bot：夜间守护已结束，回落 normal 档位（巡检间隔恢复 60s）
⑥ 立即巡检   用户：立刻检查一遍 → Bot：巡检摘要（2 项）· [CRIT] container_health/napcat 未运行
        （exit_code=1）· [WARN] container_memory/astrbot 内存 745MB 超阈值 · 动作 restart_container=ok
```

## 7. 验收标准（对应 C09）

| 编号 | 判据 | 方法 | 关联 |
| --- | --- | --- | --- |
| C09-1 | 8 个工具名/参数/类型与 B00 §8 一致 | 反射工具 schema 比对 | T-G1 |
| C09-2 | `guardian_health_report` 只读，不改 `_last_status`、无 `notify.sent` | 连续调用后查状态与审计 | B08 §10 |
| C09-3 | `scope` 非法值被拒并回执允许值 | 传 `scope=bogus` | T-G4 |
| C09-4 | `guardian_run_check` 在途巡检时回执「稍后再试」 | 并发调用 | B03 §8 |
| C09-5 | `guardian_list_incidents` `limit` 越界（0/51）被拒 | 传 0、51 | T-G4 |
| C09-6 | 5 组触发词（含「我要睡觉了」「晚安」）均触发夜间档位 | LLM 端到端 + 关键词回退断言 | T-G2 / `G-LLM` |
| C09-7 | 进入夜间后 `profile=night`、`night_until` 已写、cron `night_restart` 已建 | 查 `state` 与 `scheduler.status()` | B10 §6 |
| C09-8 | `hours` 越界（0/25）与 `hours=8` 边界值处理正确 | 边界表驱动 | T-G4 |
| C09-9 | 非管理员调用破坏性工具被拒并写 `llm.tool_denied` | `is_admin()=False` | T-G3 / `G-SAFETY` |
| C09-10 | 非法容器名被拒，回执含允许清单 | 传 `redis` | T-G4 |
| C09-11 | 首次 `confirm=false` 不执行；`confirm=true` 走安全链 | 两次调用 + 审计比对 | §6.2 |
| C09-12 | 命令与工具走同一 core（重启路径仅一处实现） | 代码检索 + 行为一致性 | T-G5 |
| C09-13 | 每次工具调用写 `llm.tool_call`/`llm.tool_respond` 审计 | 调用后查 `audit.jsonl` | T-G6 |
| C09-14 | 工具内部异常被兜底为可读回执，不向 LLM 抛栈 | 注入异常依赖 | T-G7 |
| C09-15 | `on_llm_request` 注入走 `extra_user_content_parts`，`system_prompt` 不变 | 比对请求对象 | §6.1 |

**Gate 复述**：`G-LLM` 要求「我要睡觉了」触发夜间档位（C09-6/7）、破坏性工具在非管理员下被拒（C09-9）；`G-SAFETY` 要求非白名单重启被拒（C09-10）、dry-run 零副作用（C09-11）。通过 C09 即达成里程碑 M5（智能）。
