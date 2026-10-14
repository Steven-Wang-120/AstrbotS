# C05 · QQ 掉线重登测试（Guardian 守护系统）

> 对应开发文档：`B05_服务器模式_QQ掉线与NapCat重登.md`；关联 `B15_生命周期等级与自主睡眠机制.md` §5（QQ 独立巡检）、`B10_调度器与定时任务.md`。
> 对应 Gate：`G-SERVER`（QQ 掉线冷却后重登成功）、`G-SAFETY`（token 脱敏）。
> 上位规格：`B00_项目总览与需求规格.md`（唯一权威，v0.2.0）§7 配置（`qq.*`）、§10 安全红线。
> 版本：v0.2.0　|　日期：2026-10-13

---

## 1. 目标

| ID | 目标 | 判据 |
| --- | --- | --- |
| C05-G1 | 三条证据链加权判定，单条抖动不误判 | score 加权正确；`ONLINE/SUSPECT/OFFLINE` 分级 |
| C05-G2 | 抖动抑制有效 | 连续 `offline_confirm_rounds` 轮失败才判掉线 |
| C05-G3 | 重登三方式正确 | `restart_container` / `webui_quick_login` / `disabled` |
| C05-G4 | 冷却与每小时熔断生效 | 冷却期内拒绝；超上限升级 FATAL |
| C05-G5 | 重登后复核闭环 | 复核 `user_id>0` 关闭事件；失败升级 |
| C05-G6 | 凭证安全 | `access_token` 不出现在日志/审计/`raw`/事件 |
| C05-G7 | QQ 独立巡检按 20 分钟节拍 | `qq.check_interval=1200`，与总巡检解耦 |

---

## 2. 测试环境 / 前置

| 项 | 要求 |
| --- | --- |
| 环境 | E1（真双容器）+ E7（假 OneBot，L2） |
| NapCat | OneBot11 HTTP `:3000`，WebUI `:6099`，`qq.onebot_http_base=http://napcat:3000` |
| 默认配置 | `qq.relogin_method=restart_container`、`relogin_cooldown=300`、`max_relogin_per_hour=4`、`offline_confirm_rounds=2`、`check_interval=1200` |
| 依赖注入 | `FakeDockerClient`（restart）、`aioresponses` 或 `stub_onebot`（HTTP）、`fake_state`、`timewarp` |

**前置命令**：

```bash
curl -s http://127.0.0.1:3000/get_status        # 期望 retcode=0
curl -s http://127.0.0.1:3000/get_login_info    # 期望 data.user_id>0
```

---

## 3. 测试数据与夹具

### 3.1 假 OneBot 响应样例

`get_status` 在线：

```json
{"status": "ok", "retcode": 0, "data": {"online": true, "good": true}}
```

`get_status` 掉线：

```json
{"status": "ok", "retcode": 0, "data": {"online": false, "good": false}}
```

`get_login_info` 已登录：

```json
{"status": "ok", "retcode": 0, "data": {"user_id": 10001, "nickname": "guardian-bot"}}
```

`get_login_info` 未登录：

```json
{"status": "ok", "retcode": 0, "data": {"user_id": 0, "nickname": ""}}
```

`retcode != 0`（业务错误）：

```json
{"status": "failed", "retcode": 1404, "message": "not logged in"}
```

`HTTP 401`（鉴权失败）：响应体为空，状态码 401。

### 3.2 证据链与权重（B05 §2）

| 证据 | 来源 | 命中权重 |
| --- | --- | --- |
| E1 `online=true` | `get_status` | +0.50 |
| E2 `user_id>0` | `get_login_info` | +0.30 |
| E3 `connected=true`（或 `last_event_age<300s`） | 适配器/自记 | +0.20 |

判定：`score>=0.5`→ONLINE；`0.2<=score<0.5`→SUSPECT；`score<0.2` 或 E1&E2 双否→OFFLINE 候选。

### 3.3 夹具

| 夹具 | 用途 |
| --- | --- |
| `stub_onebot` | 可控返回 online/user_id/retcode/HTTP 码 |
| `FakeDockerClient` | 断言 `restart` 调用与次数 |
| `fake_state` | 持久化 `fail_streak` / `relogin_marks` / `relogin_attempts` |
| `timewarp` | 加速 20 分钟节拍与冷却/熔断窗口 |

---

## 4. 用例表

| 用例ID | 前置条件 | 操作步骤 | 预期结果 | 判定依据 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| C05-01 | E1+E2+E3 全在线 | 触发 `qq_login` 探针 | `OK/INFO`，`score=1.0`，`fail_streak=0` | status + score | P0 |
| C05-02 | E1 超时、E2/E3 正常 | 触发探针 | 仍 `OK`（score=0.5，容忍 E1 抖动） | status + score | P0 |
| C05-03 | E1/E2 均失败、E3 失败 | 连续触发 | `score=0.0`，`fail_streak` 递增 1→2→3 | score + streak | P0 |
| C05-04 | E1 `online=false` 且 E2 `user_id=0` | 首轮触发 | 硬证据直通：`streak=3`，立即 `CRIT` | streak + status | P0 |
| C05-05 | `get_status` 超时、`get_login_info` 正常 | 触发探针 | 判定 `ONLINE`（score=0.5） | status | P0 |
| C05-06 | 单轮网络抖动（全失败 1 次） | 触发 1 次 | 不触发动作，仅 `fail_streak=1`，`WARN` | streak + 无 action | P0 |
| C05-07 | 连续 `offline_confirm_rounds=2` 轮失败 | 触发 2 次 | 第 2 轮 `WARN`（SUSPECT），无动作 | status + 无 action | P1 |
| C05-08 | 连续 3 轮全失败 | 触发 3 次 | 第 3 轮 `CRIT/CRIT`，`suggested=qq_relogin` | status + suggested | P0 |
| C05-09 | `dry_run=true`，判定掉线 | 执行 `QqReloginAction` | `detail` 以 `[dry-run]` 开头；容器/登录态零变化 | detail 前缀 + 零副作用 | P0 |
| C05-10 | `relogin_method=restart_container`，`dry_run=false` | 执行动作 | 调用 `restart napcat` 1 次，20s 后复核 `user_id>0`，`ok=true` | 调用记录 + `ok` | P0 |
| C05-11 | `relogin_method=webui_quick_login`，WebUI 正常 | 执行动作 | 调 `SetQuickLogin` 成功，复核通过 | `ok=true` | P1 |
| C05-12 | `webui_quick_login` 返回 `code!=0` | 执行动作 | `ok=false`，`detail` 含失败码，尝试计数 +1 | `ok==false` | P1 |
| C05-13 | `webui_quick_login` WebUI 不可达 | 执行动作 | `ok=false`，`detail` 含"WebUI 不可达" | detail 匹配 | P1 |
| C05-14 | `relogin_method=disabled` | 判定掉线后执行动作 | `ok=false`，`detail` 含"仅告警"；无容器/WebUI 调用 | 调用记录为空 | P0 |
| C05-15 | 冷却期内再次触发重登 | 执行动作 | `ok=false`，`detail` 含剩余秒数 | detail 匹配 | P0 |
| C05-16 | 1 小时内第 5 次重登 | 执行动作 | 熔断：`ok=false`，提示人工；FATAL 事件 | detail + 事件级别 | P0 |
| C05-17 | 重登成功 | 执行动作后 | `fail_streak=0`、`state=ONLINE`，incident 关闭，推送"重登成功" | KV + 通知 | P0 |
| C05-18 | 连续 3 次重登失败 | 执行动作 3 次 | `attempts>=3`，`profile=maintenance`，FATAL 通知含"人工扫码" | profile + 通知 | P0 |
| C05-19 | `access_token="secret-token"` | 触发探针 + 动作 + 审计 | token 不出现在日志/`raw`/`Incident.message`/`audit.jsonl` | `grep` 全为 0 | P0 |
| C05-20 | `access_token` 已配置 | 触发 401 响应 | 归为 `NapCatUnavailable`，计入 `fail_streak`，不误判 ONLINE | 异常归类 + streak | P0 |
| C05-21 | `check_interval=1200`，`timewarp` 加速 | 推进 60 分钟虚拟时间 | `guardian:qq` 任务恰好触发 3 次（20 分钟节拍） | 回调计数 == 3 | P0 |
| C05-22 | 同上 | 观察总巡检 `guardian:patrol`（60s）与 `guardian:qq` | 两者节拍独立；QQ 不随总巡检频率变化 | 两任务计数各自正确 | P0 |
| C05-23 | 总巡检与 QQ 巡检同时含 `qq_login` | 触发一轮总巡检 | 总巡检顺带跑 `qq_login` 但**不重复触发重登**（冷却统一收口） | restart 调用 ≤ 1 | P1 |
| C05-24 | E3 适配器 API 不可用（`connected=None`） | 触发探针，`last_event_age<300s` | E3 记 +0.20（兜底），判定不受影响 | score 正确 | P1 |

---

## 5. 边界与异常用例

| 用例ID | 前置条件 | 操作步骤 | 预期结果 | 判定依据 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| C05-25 | `get_login_info` 返回 `user_id=0` | 触发探针 | E2 记 `valid=false`，不误判在线 | score/streak | P0 |
| C05-26 | `retcode!=0`（业务错误） | 触发探针 | 抛 `NapCatError`，被 `_e1/_e2` 捕获为失败证据 | 证据 `ok=false` | P1 |
| C05-27 | `last_event_age` 很大（静默期） | 触发探针 | 静默期不等于掉线；若 E1/E2 正常仍判在线 | status | P1 |
| C05-28 | 三证据并发采集 | 单次 `check()` | 总耗时 ≤ 各接口超时（并发，非串行求和） | 耗时断言 | P1 |
| C05-29 | 重登成功后 `fail_streak` 复位 | 复核成功 | `probe.qq_login` 的 `fail_streak=0` | KV 值 | P1 |
| C05-30 | `max_relogin_per_hour=1` | 1 小时内第 2 次 | 熔断触发 | detail + 事件 | P1 |
| C05-31 | 冷却剩余时间为 0（刚好过期） | 执行动作 | 冷却通过，允许重登 | 调用发生 | P2 |
| C05-32 | 每小时窗口跨重启 | 重启进程后检查 `relogin_marks` | 窗口标记落盘，重启后仍计数（防绕过） | KV 持久 | P1 |
| C05-33 | 重登中 `restart` 抛异常 | 执行动作 | `ok=false`，`attempts+1`，`detail` 含异常类型 | 字段匹配 | P1 |
| C05-34 | `webui_base` 从 OneBot 基址推导 | 检查 `webui_base` | 主机名取自 `onebot_http_base`，端口 6099 | 属性值 | P2 |
| C05-35 | `qq.umo_list` 为空 | 掉线告警 | 通知降级为日志，不视为错误 | 无异常 | P2 |
| C05-36 | 时间加速下 20 分钟节拍首跳 | 从 0 推进 | 首跳在 `interval` 后（非立即），除非 `run_now=true` | 首跳时刻 | P1 |

---

## 5.1 状态机（B05 §11）

```
ONLINE ──(score<0.5, streak=1)──► SUSPECT ──(streak=2)──► SUSPECT
   ▲                                   │                     │
   │                                   │ (score>=0.5)        │ (streak>=3 或 hard)
   └───────────────────────────────────┘                     ▼
                                                          OFFLINE
                                                             │
                        SafetyGuard.authorize(qq_relogin)
                              ├─ deny ─► SUPPRESSED（审计 + 通知）
                              └─ allow ─► RELOGIN_IN_PROGRESS
                                               │
                        ┌──────────────────────┴──────────────────────┐
                        ▼                                             ▼
                复核成功(user_id>0)                          复核失败 / attempts>=3
                        │                                             │
                    ONLINE                                    OFFLINE_HARD
               （streak=0, 事件闭环）                    （maintenance + FATAL 通知人工扫码）
```

## 5.2 时间线（重登成功路径）

```
t0        QqLoginProbe 连续第 3 次失败 -> CRIT + suggested=qq_relogin
t0        SafetyGuard 授权（冷却/熔断/dry_run 通过）
t0        QqReloginAction: method=restart_container -> POST /containers/napcat/restart?t=10
t0+20s    sleep(VERIFY_WAIT_SEC)
t0+25s    get_login_info -> user_id=10001 -> ok=true
t0+25s    复位 streak=0、state=ONLINE；关闭 incident；推送"重登成功"
```

## 5.3 抖动抑制分层（B05 §7）

| 层 | 机制 | 参数 | 用例 |
| --- | --- | --- | --- |
| L1 证据加权 | 三证据独立打分（非布尔与） | 0.5/0.3/0.2 | C05-01/02/05 |
| L2 连续计数 | `fail_streak` 持久化 | `SUSPECT_CONSEC=2`、`OFFLINE_CONSEC=3` | C05-06/07/08 |
| L3 硬证据直通 | E1&E2 双否 → `streak=OFFLINE_CONSEC` | — | C05-04 |
| L4 动作冷却 | `qq.relogin_cooldown=300` | 300s | C05-15 |
| L5 熔断 | `max_relogin_per_hour=4` | 4 次/小时 | C05-16 |
| L6 成功复位 | 复核成功 `fail_streak=0` | — | C05-17 |

> 与总巡检关系（B15 §5）：总巡检仍会顺带跑 `qq_login`，但**不重复触发重登**（由 `SafetyGuard` 冷却统一收口，C05-23）。

## 5.4 脱敏核查清单（C05-19）

| 检查位置 | 期望 |
| --- | --- |
| 日志 | 无 `access_token` 明文；`mask_token()` 输出 `ab***(N)` |
| `ProbeResult.raw` | 仅含 `base`/`e1`/`e2`/`e3`，无 token |
| `Incident.message` | 无 token |
| `audit.jsonl` | `redact()` 后 token 为 `***` |
| 请求头 | 仅 `Authorization: Bearer <token>`，不入 query string |

---

## 6. 通过标准

- 第 4 节全部 P0（C05-01/02/03/04/05/06/08/09/10/14/15/16/17/18/19/20/21/22）通过。
- 第 5 节 P0（C05-25）通过。
- 抖动抑制正确：单轮抖动不动作（C05-06），连续 N 轮才判定（C05-08）。
- 重登闭环：成功复位（C05-17）；失败升级 `maintenance` + FATAL（C05-18）。
- token 全链路脱敏（C05-19）。
- QQ 独立巡检 20 分钟节拍正确且与总巡检解耦（C05-21/22）。

## 7. 证据留存

| 证据 | 命令 | 存放 |
| --- | --- | --- |
| 探针结果 | `pytest tests/test_qq.py -v` / `run_once` 输出 | `artifacts/c05/qq.log` |
| 假 OneBot 交互 | `stub_onebot` 请求日志 | `artifacts/c05/stub.log` |
| 重登调用 | `FakeDockerClient.calls` / 真环境 `docker events` | `artifacts/c05/calls.txt` |
| 脱敏校验 | `grep -r "secret-token" data/ logs/`（期望 0） | `artifacts/c05/redact.txt` |
| 节拍验证 | 时间加速下 `guardian:qq` 回调计数 | `artifacts/c05/beat.txt` |

## 8. 回归建议

- 改动 `probes/qq_login.py` / `utils/napcat.py` / `actions/qq_actions.py` 后**必跑** C05 全量。
- 改动 `core/scheduler.py` 或 QQ 任务注册后，联动回归 C05-21/22/23 与 C10。
- 改动脱敏逻辑后必跑 C05-19。
- 每次发版前跑 C05-18（失败升级路径）。

## 9. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-10-13 | v0.2.0 | 初版：三条证据链、抖动抑制、三方式重登、冷却/熔断、复核闭环、失败升级、token 脱敏、20 分钟独立节拍 |
