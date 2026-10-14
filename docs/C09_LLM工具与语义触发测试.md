# C09 · LLM 工具与语义触发测试（Guardian 守护系统）

> 对应开发文档：`B09_LLM工具与睡觉语义触发.md`；关联 `B15`（生命周期等级与自主睡眠）、`B12`（权限与安全）、`B11`（审计）。
> 对应 Gate：`G-LLM`（"我要睡觉了"触发夜间/睡眠语义；非管理员破坏性工具被拒）、`G-SAFETY`（非白名单重启被拒、dry-run 零副作用）。
> 上位规格：`B00_项目总览与需求规格.md`（唯一权威，v0.2.0）§5.1 生命周期等级、§8 工具契约、§9 命令、§10 安全红线、P1 非关键词触发铁律。
> 版本：v0.2.0　|　日期：2026-10-13

---

## 1. 目标

| ID | 目标 | 判据 |
| --- | --- | --- |
| C09-G1 | 工具集合与 B00 §8 契约逐字一致 | 工具名/参数/类型/必填比对通过 |
| C09-G2 | docstring `Args:` 正确转 JSON schema | 参数名/类型/required 与签名一致 |
| C09-G3 | 破坏性工具非管理员被拒 | `event.is_admin()=False` 返回拒绝文案 + 审计 |
| C09-G4 | LLM 幻觉出的非法参数被运行时白名单拦下 | 非法容器名/枚举被拒并回执 |
| C09-G5 | 工具与命令复用同一 core | 无第二份 `restart_container` 实现 |
| C09-G6 | 工具调用可观测可审计 | 每次调用写 `llm.tool_call`/`llm.tool_respond` |
| C09-G7 | 工具异常不把栈抛给 LLM | 任何异常转可读回执 |
| C09-G8 | **非关键词触发**：插件层不解析对话文本 | 发"我要睡觉了"但 LLM 未调用工具 → 零动作 |

---

## 2. 测试环境与前置

| 项 | 要求 |
| --- | --- |
| 环境 | E7（反射 schema / 假 event，L1/L2）+ E1（真 LLM 端到端，L3） |
| 依赖注入 | 假 `AstrMessageEvent`（`is_admin()` 可控、`plain_result`、`get_sender_id`）、`FakeNotifier`、`fake_state`、`fake_audit`、假 `engine` |
| 配置基线 | `llm_tools.enabled=true`、`llm_tools.require_admin=true`、`dry_run=true`、`lifecycle.enabled=true` |
| 白名单 | `containers.managed=["napcat"]`、`safety.allowed_containers=["fake-svc-a"]` |
| 常量 | `SCOPE_VALUES=(all,containers,qq,ros2)`、`MODE_VALUES=(server,ros2)`、`PROFILE_VALUES=(normal,night,maintenance,aggressive)` |

**前置命令**：

```bash
docker exec astrbot python -c "from astrbot_plugin_guardian.tools.llm_tools import GuardianLLMTools as T; print([n for n in dir(T) if n.startswith('guardian_')])"
pytest tests/test_llm_tools.py -v --tb=short
```

---

## 3. 测试数据与夹具

### 3.1 工具契约基线（B00 §8）

| 工具名 | 参数 | 必填 | 副作用 | 权限 |
| --- | --- | --- | --- | --- |
| `guardian_health_report` | `scope`(string) | 否（默认 all） | 只读 | 所有用户 |
| `guardian_lifecycle_status` | 无 | — | 只读 | 所有用户 |
| `guardian_run_check` | `scope`(string) | 否 | 可能触发动作 | 所有用户（受约束） |
| `guardian_diagnose` | `target`(string) | 否（默认 ""） | 只读 | 所有用户 |
| `guardian_list_incidents` | `limit`(number) | 否（默认 10） | 只读 | 所有用户 |
| `guardian_request_sleep` | `reason`(string), `duration_min`(number) | 否 | 真睡眠（重启） | 所有用户（受约束） |
| `guardian_restart_container` | `container`(string), `confirm`(boolean) | `container` 必填 | 重启容器 | 管理员 + 确认 |
| `guardian_set_mode` | `mode`(string) | 否 | 切模式 | 管理员 |
| `guardian_set_profile` | `profile`(string) | 否 | 切档位 | 管理员 |

> v0.2 变更：删除 `guardian_enter_night_watch`，新增 `guardian_request_sleep` 与 `guardian_lifecycle_status`。基线 8 项 − 1 删除 + 2 新增 = **9 项**（C09-01 断言集合相等）。

### 3.2 假 event 与夹具

```python
class FakeEvent:
    def __init__(self, admin=False, sender="10001"):
        self._admin, self._sender, self.out = admin, sender, []
    def is_admin(self): return self._admin
    def get_sender_id(self): return self._sender
    def plain_result(self, text): self.out.append(text); return text
```

| 夹具 | 用途 |
| --- | --- |
| `fake_event(admin=...)` | 权限与回执断言 |
| `fake_engine` | 记录 `snapshot/run_once/request_action/switch_mode/set_profile/request_sleep/allowed_containers` 调用 |
| `fake_audit` | 记录 `llm.tool_call` / `llm.tool_respond` / `llm.tool_denied` |
| `capture_llm_request()` | 捕获 `on_llm_request` 后的 `req` 对象（system_prompt / extra_user_content_parts） |

---

## 4. 用例表

| 用例ID | 前置条件 | 操作步骤 | 预期结果 | 判定依据 | 优先级(P0/P1/P2) |
| --- | --- | --- | --- | --- | --- |
| C09-01 | `llm_tools.enabled=true` | 反射收集全部 `guardian_*` 工具名 | 集合 == §3.1 的 9 项（含 `guardian_request_sleep`、`guardian_lifecycle_status`，无 `guardian_enter_night_watch`） | 集合相等 | P0 |
| C09-02 | `llm_tools.enabled=false` | 加载插件 | 不注册任何工具，日志 `跳过工具注册` | 工具集合为空 | P1 |
| C09-03 | `guardian_health_report` | 解析其 docstring `Args:` 生成 schema | `scope` 为 string，非必填，默认 `all` | schema.required 不含 scope | P0 |
| C09-04 | `guardian_restart_container` | 解析 schema | `container` string 必填，`confirm` boolean 非必填 | `required==["container"]` | P0 |
| C09-05 | `guardian_request_sleep` | 解析 schema | `reason` string、`duration_min` number，均非必填 | 参数名/类型匹配 | P0 |
| C09-06 | `guardian_lifecycle_status` | 解析 schema | 无参数（properties 为空） | 参数数 == 0 | P1 |
| C09-07 | `guardian_list_incidents` | 解析 schema | `limit` number，描述含 `1-50` | 描述文本匹配 | P1 |
| C09-08 | 普通用户（`is_admin=False`） | 调 `guardian_restart_container` | 回执 `操作被拒绝：...仅管理员可用`，写 `llm.tool_denied` | 回执文本 + 审计 kind | P0 |
| C09-09 | 普通用户 | 调 `guardian_set_mode` / `guardian_set_profile` | 均被拒 | 两条拒绝回执 | P0 |
| C09-10 | 管理员 | 调 `guardian_restart_container(container="redis")` | 回执 `容器 'redis' 不在白名单内，允许：...`，写 `llm.tool_denied` | 回执含允许清单 + 零动作 | P0 |
| C09-11 | 管理员 | 调 `guardian_diagnose(target="not-a-target")` | 回执 `参数 target 非法，允许值：...` | 回执文本匹配 | P0 |
| C09-12 | 任意用户 | 调 `guardian_health_report(scope="bogus")` | 回执 `参数 scope 非法，允许值：all / containers / qq / ros2` | 回执文本匹配 | P0 |
| C09-13 | 任意用户 | 调 `guardian_list_incidents(limit=0)` 与 `limit=51` | 两次均被拒（越界） | 回执匹配 | P0 |
| C09-14 | `dry_run=true`，管理员 | `guardian_restart_container(container="napcat", confirm=true)` | 回执含 `dry-run` 语义；容器 `StartedAt`/`RestartCount` 不变 | 状态零变化 | P0 |
| C09-15 | 管理员 | `confirm=false` 首次调用 | 回执 `确认执行？...请再次调用并置 confirm=true`，不执行 | 零动作 | P0 |
| C09-16 | **关键词反证**：用户发 `我要睡觉了`，LLM 不调用任何工具 | 触发消息处理 | 插件层零动作：无 `guardian_request_sleep`/`enter_night`/`set_profile` 调用，无状态变更 | 引擎调用记录为空 + 审计无动作 | P0 |
| C09-17 | **关键词反证**：用户发 `晚安`/`我休息了`/`去睡了`/`下线了` | 同上，LLM 不调用工具 | 均零动作（插件不匹配任何关键词） | 5 组词全部零动作 | P0 |
| C09-18 | `lifecycle` 处于 `arm_sleep`（`sleep_armed=true`） | 调 `guardian_request_sleep(reason="休息", duration_min=0)` | 免二次确认，直接进入 `SleepAction` 授权链 | 无"确认"回执 + 动作被授权 | P0 |
| C09-19 | 非 `arm_sleep` 状态 | 调 `guardian_request_sleep` | 按常规流程（仍受 dry-run 与 SafetyGuard 约束） | 回执与状态一致 | P1 |
| C09-20 | 任意用户 | 调 `guardian_request_sleep(duration_min=200)` | 回执参数越界（允许 0-120） | 回执匹配 | P1 |
| C09-21 | `lifecycle.enabled=true`，`uptime` 已注入 | 调 `guardian_lifecycle_status()` | 返回运行时长、当前等级、距下一等级/硬上限剩余时间 | 回执含 uptime/level/remaining | P0 |
| C09-22 | `lifecycle.enabled=false` | 调 `guardian_lifecycle_status()` | 返回"生命周期未启用"类可读文案，不抛异常 | 回执可读 | P1 |
| C09-23 | 任意用户 | 调 `guardian_health_report` 数次 | 只读：不改 `_last_status`，无 `notify.sent` 审计 | 状态不变 + 审计无 notify | P0 |
| C09-24 | 巡检在途（`engine.busy=True`） | 调 `guardian_run_check` | 回执 `上一轮巡检尚未结束，请稍后再试` | 回执文本匹配 | P1 |
| C09-25 | `on_llm_request` 已注册，存在注入提示词 | 构造 `req` 并触发钩子 | 提示词追加到 `req.extra_user_content_parts` | 片段含 `<lifecycle_guard ...>` | P0 |
| C09-26 | 同上 | 触发钩子 | `req.system_prompt` 内容不变（未被污染） | 前后 system_prompt 相等 | P0 |
| C09-27 | 注入内容含状态提示 | 检查注入文本 | 不含 token/UMO 完整值/user_id 完整值 | 正则扫描无敏感串 | P0 |
| C09-28 | `inject_mode=system_prompt`（可选配置） | 触发钩子 | 追加到 `req.system_prompt`（该模式下允许） | 分支正确 | P2 |
| C09-29 | 无注入提示词（`state` 为空） | 触发钩子 | 不追加任何片段，无异常 | `extra_user_content_parts` 长度不变 | P1 |
| C09-30 | 工具调用前 | 触发 `on_using_llm_tool(tool_name="guardian_restart_container", args={...})` | 写 `llm.tool_call` 审计（actor/trigger/target/params） | 审计行字段完整 | P0 |
| C09-31 | 工具调用后 | 触发 `on_llm_tool_respond(tool_name=..., response=...)` | 写 `llm.tool_respond` 审计（result 截断 500） | 审计行存在 | P0 |
| C09-32 | 非 `guardian_` 前缀工具 | 触发两个钩子 | 不写 Guardian 审计 | 审计无新增 | P1 |

---

## 5. 边界与异常用例

| 用例ID | 前置条件 | 操作步骤 | 预期结果 | 判定依据 | 优先级(P0/P1/P2) |
| --- | --- | --- | --- | --- | --- |
| C09-33 | 注入依赖使 `engine.snapshot` 抛异常 | 调 `guardian_health_report` | 回执 `查询失败：<类型>: <消息>`，`logger.exception` 记录，不向 LLM 抛栈 | 回执可读 + 无栈上抛 | P0 |
| C09-34 | `engine.run_once` 抛异常 | 调 `guardian_run_check` | 回执 `巡检失败：...`，引擎不崩 | 回执可读 | P0 |
| C09-35 | `engine.request_action` 抛异常 | 调 `guardian_restart_container(confirm=true)` | 回执 `重启失败：...` | 回执可读 | P0 |
| C09-36 | `engine.switch_mode` 抛异常 | 调 `guardian_set_mode` | 回执 `切换模式失败：...` | 回执可读 | P1 |
| C09-37 | `engine.set_profile` 抛异常 | 调 `guardian_set_profile` | 回执 `切换档位失败：...` | 回执可读 | P1 |
| C09-38 | `event.is_admin()` 抛异常 | 调破坏性工具 | 回退 `event.role in {admin,owner}`；仍异常则按拒绝处理 | 不崩溃 | P1 |
| C09-39 | `event.get_sender_id()` 抛异常 | 调任意工具 | `_actor` 回退 `u:unknown`，审计可写 | 审计 actor 存在 | P2 |
| C09-40 | `mode` 非法值 `bogus` | 调 `guardian_set_mode` | 回执参数非法（管理员已通过权限） | 回执匹配 | P1 |
| C09-41 | `profile` 非法值 | 调 `guardian_set_profile` | 回执参数非法 | 回执匹配 | P1 |
| C09-42 | `guardian_restart_container(container="")` | 管理员调用 | 回执不在白名单（空串被拒） | 回执匹配 | P1 |
| C09-43 | 白名单含 `fake-svc-a`（来自 `allowed_containers`） | 管理员 `confirm=true` 重启它 | 白名单并集生效，动作被授权 | `allowed_containers()` 含该项 | P1 |
| C09-44 | 工具 docstring 描述与常量元组不一致（构造） | 运行时传 docstring 未列出的合法枚举 | 以方法体常量元组为准复核（运行时权威） | 行为以常量为准 | P1 |
| C09-45 | `duration_min` 为非数字 `"abc"` | 调 `guardian_request_sleep` | 回执参数非法，不抛 `ValueError` 到 LLM | 回执可读 | P1 |
| C09-46 | `lifecycle` 硬上限触发中（`force_sleep`） | 调 `guardian_lifecycle_status` | 回执显示剩余 0 与"硬上限已到"语义 | 回执含 deadline 语义 | P2 |
| C09-47 | `arm_sleep` 且 `dry_run=true` | 调 `guardian_request_sleep` | 免确认但 dry-run 只记录（`detail` 含 `[dry-run]`） | detail 前缀 + 零副作用 | P0 |
| C09-48 | 连续两次 `on_llm_request`（同等级，冷却内） | 触发钩子两次 | 第二次不重复注入（受 `inject_cooldown_sec` 约束） | 片段计数 == 1 | P1 |
| C09-49 | 工具返回值为 async generator | 迭代工具方法 | 逐条 `yield event.plain_result(text)`，文本进入 LLM 上下文 | 迭代产出 | P1 |
| C09-50 | 工具 `name` 缺省 | 反射工具名 | 取函数名作为工具名 | 工具名 == 方法名 | P1 |
| C09-51 | `scope` 缺省 | 调 `guardian_health_report()` | 使用默认 `all` | 引擎调用参数 | P1 |
| C09-52 | `limit` 缺省 | 调 `guardian_list_incidents()` | 使用默认 10 | 引擎调用参数 | P2 |
| C09-53 | 工具直接 `send_message`（禁止） | 静态检索 | 工具层不直接 `await context.send_message`（回执走 event） | 代码检索无命中 | P1 |
| C09-54 | 命令与工具同一 core | 检索 `restart_container` 实现 | 仅一处实现（命令/工具共用 `engine.request_action`） | 代码检索 == 1 | P0 |
| C09-55 | 工具名非 `guardian_` 前缀 | 触发审计钩子 | 不写 Guardian 审计 | 审计无新增 | P2 |
| C09-56 | 注入文本格式 | 检查 `extra_user_content_parts` 内容 | 形如 `<lifecycle_guard level="L3">...</lifecycle_guard>` | 标签匹配 | P1 |

---

## 5.1 工具参数收口两层设计（B09 §3）

| 层 | 机制 | 失败表现 | 用例 |
| --- | --- | --- | --- |
| 静态（docstring） | `Args:` 描述列出取值域 | LLM 少写非法值（软约束） | C09-03/04/05 |
| 运行时（方法体） | 常量元组 `SCOPE_VALUES`/`MODE_VALUES`/`PROFILE_VALUES` + `engine.allowed_containers()` | 非法值被拒并回执 | C09-10/11/12/13/40/41 |

> docstring 是静态字符串，无法拼接动态白名单，故**运行时复核为权威**（C09-44）。

## 5.2 破坏性工具二次确认流程（B09 §6.2）

```
_require_admin(event)  ├─ 否 -> 拒绝回执 + 审计 llm.tool_denied
  └─ 是 -> allowed_containers() 复核 ├─ 否 -> 拒绝回执（含允许清单）
        └─ 是 -> confirm? ├─ false -> "请再次调用并置 confirm=true；当前 dry_run=X"
                         └─ true  -> engine.request_action() -> SafetyGuard.authorize()
                              ├─ deny -> 回执拒绝原因（冷却/熔断/dry-run/受保护）
                              └─ allow -> 执行 -> 回执 ok/elapsed_ms/detail
```

## 5.3 LLM 幻觉防护五道防线（B09 §6.4）

| 防线 | 机制 | 失败表现 | 用例 |
| --- | --- | --- | --- |
| 枚举参数 | docstring 描述列出取值域 | 少写非法值 | C09-03..07 |
| 运行时白名单复核 | `allowed_containers()` / 常量元组 | 非法值被拒 | C09-10/12 |
| 权限 | `event.is_admin()` | 非管理员全拒 | C09-08/09 |
| 安全链 | `SafetyGuard.authorize` 冷却/熔断/dry-run | 高频/危险动作被抑制 | C09-14/15 |
| 无自由文本 | 参数无 `command`/`shell` 字段 | 无法注入 | C09-10 |

---

## 6. 通过标准

- 第 4 节全部 P0（C09-01/03/04/05/08/09/10/11/12/13/14/15/16/17/18/21/23/25/26/27/30/31）通过。
- 第 5 节 P0（C09-33/34/35/47）通过。
- 工具集合与 B00 §8 逐字一致（C09-01/03/04/05）；schema 的 required/类型正确（C09-03/04/05）。
- 非管理员被拒（C09-08/09）；非法容器名被拒且有话术（C09-10）。
- `dry_run=true` 零副作用（C09-14/47）。
- **关键词反证**成立：发"我要睡觉了"等 5 组词但 LLM 不调用工具 → 零动作（C09-16/17）。
- `arm_sleep` 下 `guardian_request_sleep` 免二次确认（C09-18）。
- `on_llm_request` 走 `extra_user_content_parts` 且 `system_prompt` 不变（C09-25/26）；注入不含敏感信息（C09-27）。
- 工具调用审计完整（C09-30/31）；异常全部兜底为可读回执（C09-33..37）。
- `G-LLM` 必过项：破坏性工具非管理员被拒、语义引导（由 C15 端到端补足）。

## 7. 证据留存

| 证据 | 采集方式 | 存放 |
| --- | --- | --- |
| 工具 schema | 反射导出 `{name: parameters}` JSON | `artifacts/c09/tools_schema.json` |
| 单测日志 | `pytest tests/test_llm_tools.py -v > artifacts/c09/llm.log` | `artifacts/c09/` |
| 关键词反证 | 5 组消息 + 引擎调用记录（空） | `artifacts/c09/keyword_negation.txt` |
| 注入对象对比 | `req.system_prompt` 前后快照 + `extra_user_content_parts` | `artifacts/c09/inject.json` |
| 审计 | `grep -E "llm\." audit.jsonl` | 随包归档 |

## 8. 回归建议

- 改动 `tools/llm_tools.py` / `main.py` 的 `@filter.llm_tool` 或 `on_llm_request` 后**必跑** C09 全量。
- 改动工具集合（增删工具）后联动回归 C09-01 与 C15 专项。
- 改动 `llm_tools.require_admin` 或权限助手后联动回归 C09-08/09 与 C12-17。
- 改动 `engine.request_action` 授权路径后联动回归 C09-14/15/18 与 C12。
- 每次发布前在 E1 做一次真实 LLM 端到端（C09-16 反证 + C15 自主睡眠）。

## 9. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-10-13 | v0.2.0 | 初版：工具注册与 schema、权限与话术、dry-run 零副作用、关键词反证、arm_sleep 免确认、on_llm_request 注入、审计、异常兜底 |
