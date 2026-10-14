# C04 · Docker 容器健康与重启测试（Guardian 守护系统）

> 对应开发文档：`B04_服务器模式_Docker容器健康与重启.md`；关联 `B12_安全权限与危险操作保护.md`、`B13_部署集成_compose_socket与看门狗自重启.md`。
> 对应 Gate：`G-SERVER`（容器停止 2×间隔内拉起）、`G-SAFETY`（非白名单被拒、dry-run 零副作用）。
> 上位规格：`B00_项目总览与需求规格.md`（唯一权威，v0.2.0）§4 模式、§7 配置、§10 安全红线。
> 版本：v0.2.0　|　日期：2026-10-13

---

## 1. 目标

| ID | 目标 | 判据 |
| --- | --- | --- |
| C04-G1 | 经 socket HTTP API 正确观测容器状态 | 无需容器内 docker CLI |
| C04-G2 | 状态判定分级正确 | 404→CRIT、非 running→CRIT、restarting→WARN、OOM→CRIT |
| C04-G3 | daemon 不可达只告警不动作 | `UNKNOWN` + 零副作用 |
| C04-G4 | 重启动作幂等、可复核 | 2×间隔内恢复 `running`，事件闭环 |
| C04-G5 | 受保护容器与自重启三策略正确 | 默认拒绝；watchdog/docker_api/disabled 分支清晰 |
| C04-G6 | 降级链完整 | API 协商失败 → CLI（若开） → `UNKNOWN` + 告警 |

---

## 2. 测试环境 / 前置

| 项 | 要求 |
| --- | --- |
| 环境 | E1（真双容器，L3/L4）+ E7（假 docker，L2） |
| socket | `/var/run/docker.sock` 已挂载且可写 |
| 容器 | `astrbot`（受保护本体）、`napcat`（默认在 `managed` 时可动作） |
| 默认配置 | `dry_run=true`、`safety.protected_containers=["astrbot"]`、`allow_self_restart=false` |
| 白名单 | `containers.managed=["napcat"]`（或 `safety.allowed_containers`） |

**前置命令**：

```bash
docker compose -p guardian-test -f deploy/docker-compose.yml up -d
docker exec astrbot python -c "import os; print(os.access('/var/run/docker.sock', os.W_OK))"  # True
```

---

## 3. 测试数据与夹具

### 3.1 mock Docker API 响应样例

`GET /containers/{id}/json`（running，正常）：

```json
{"State": {"Status": "running", "Running": true, "RestartCount": 0, "OOMKilled": false,
  "Health": {"Status": "healthy"}, "StartedAt": "2026-10-01T00:00:00.000000000Z", "ExitCode": 0},
 "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}}
```

`exited`（非 running）：

```json
{"State": {"Status": "exited", "Running": false, "RestartCount": 2, "OOMKilled": false,
  "StartedAt": "2026-10-01T00:00:00.000000000Z", "ExitCode": 1}}
```

`restarting`（窗口抑制）：

```json
{"State": {"Status": "restarting", "Running": false, "RestartCount": 3, "OOMKilled": false}}
```

`OOMKilled`：

```json
{"State": {"Status": "exited", "Running": false, "OOMKilled": true, "ExitCode": 137,
  "FinishedAt": "2026-10-13T04:12:03.000000000Z"}}
```

`unhealthy`：

```json
{"State": {"Status": "running", "Running": true, "Health": {"Status": "unhealthy"}}}
```

`GET /_ping` → `OK`；`GET /version` → `{"ApiVersion": "1.44", "Version": "24.0.7"}`；容器不存在 → HTTP 404。

### 3.2 夹具

| 夹具 | 用途 |
| --- | --- |
| `FakeDockerClient(script)` | 按 `inspect:{name}` / `ping` / `stats:{name}` 回放响应 |
| `fake_state` | 记录 `action.last_restart.<name>` 与 KV |
| `fake_notifier` | 断言告警条数 |
| `timewarp` | 控制 `RESTARTING_WINDOW_SEC` 与复核轮询 |

---

## 4. 用例表

| 用例ID | 前置条件 | 操作步骤 | 预期结果 | 判定依据 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| C04-01 | daemon 正常 | 触发 `docker_daemon` 探针 | `OK/INFO`，`metrics.api_version` 非空 | `status==ok` + 字段非空 | P0 |
| C04-02 | `ping=false`（daemon 无响应） | 触发探针 | `UNKNOWN/WARN`，**不动作**，单条告警 | `status==unknown` + 无 action | P0 |
| C04-03 | socket 不存在 | 触发探针 | `UNKNOWN/WARN`，文案提示挂载/权限 | 文案匹配 + 零动作 | P0 |
| C04-04 | 容器不存在（inspect 404） | `container_health` 探针 | `CRIT/CRIT`，`metrics.exists=false`，`suggested=start_container` | status + suggested | P0 |
| C04-05 | `State.Status="exited"` | 触发探针 | `CRIT/CRIT`，`suggested=start_container` | status + suggested | P0 |
| C04-06 | `State.Status="restarting"` | 触发探针 | `WARN/WARN`，`suggested=None`（窗口抑制） | status + action 为空 | P0 |
| C04-07 | `State.Status="paused"` | 触发探针 | `WARN/WARN`，不动作 | status + action 为空 | P1 |
| C04-08 | `RestartCount` 由 0 → 1 | 连续两次采样 | 第二次 `WARN/WARN`，`metrics.restart_delta>=1` | status + delta | P1 |
| C04-09 | 窗口内新增重启 ≥ 3 | 连续采样 | `CRIT/CRIT`，`suggested=None`（CrashLoop 熔断） | status + action 为空 | P0 |
| C04-10 | `OOMKilled=true` | 触发探针 | `CRIT/CRIT`，`forensics=oom`，转 B06 取证 | status + 字段 | P0 |
| C04-11 | `Health.Status="unhealthy"` | 触发探针 | `CRIT/CRIT`，`suggested=restart_container` | status + suggested | P0 |
| C04-12 | `Health.Status="starting"` | 触发探针 | `WARN/WARN`，不动作 | status + action 为空 | P1 |
| C04-13 | `napcat` 非 running，`dry_run=true` | 授权并执行 `restart_container` | `detail` 以 `[dry-run]` 开头；容器 `StartedAt`/`RestartCount` 不变 | detail 前缀 + 状态零变化 | P0 |
| C04-14 | `napcat` 非 running，`dry_run=false` | 执行 `RestartContainerAction` | 调用 `POST /containers/napcat/restart` 1 次，复核 `running` | 调用记录 + `ok=true` | P0 |
| C04-15 | 重启 API 返回 500 | 执行动作 | `ok=false`，`detail` 含 HTTP 500，计入失败即停 | `ok==false` + 计数 | P0 |
| C04-16 | 重启成功 | 复核轮询 `inspect` | ≤ `VERIFY_TIMEOUT_SEC=60s` 内 `running`，事件闭环，`output` 含末次 State | `ok=true` + output 非空 | P0 |
| C04-17 | 重启后仍不 running | 复核超时 | `ok=false`，末次状态写入 `output`，升级告警 | `ok==false` + output | P1 |
| C04-18 | 容器处于 `restarting` | 触发动作 | `_precheck` 返回 None，跳过重复动作（幂等） | `detail` 含"正在重启中" | P0 |
| C04-19 | 同容器并发两次动作 | 并发调用 `execute` | 第二次因 `_inflight` 跳过，仅一次真实重启 | 调用记录 1 次 | P0 |
| C04-20 | `astrbot`（受保护），`allow_self_restart=false` | 授权 `restart_container` | `Decision.allowed=false`，`code=protected_requires_self_restart` | code 匹配 + 零动作 | P0 |
| C04-21 | `astrbot`，`allow_self_restart=true`，`strategy=watchdog` | 授权并执行 | 写 `restart_request.json`（含 nonce/ts/target），**不**直接调 Docker API | 文件存在 + 无 restart 调用 | P0 |
| C04-22 | `astrbot`，`allow_self_restart=true`，`strategy=docker_api` | 执行 | 先回执后 `POST /containers/astrbot/restart`；连接中断视为成功 | 调用记录 + `ok=true` | P1 |
| C04-23 | `astrbot`，`allow_self_restart=true`，`strategy=disabled` | 授权 | `code=self_restart_strategy_disabled`，拒绝 | code 匹配 + 零动作 | P0 |
| C04-24 | API 版本协商失败（`/version` 异常） | 触发客户端 | 回退 `docker.api_version`（默认 v1.44），不崩溃 | 使用回退版本 | P1 |
| C04-25 | socket 不可用且 `use_cli_fallback=true` | 触发探针/动作 | 降级到 docker CLI（verb 白名单），或失败告警 | 降级链记录 | P1 |
| C04-26 | socket 权限不足（EACCES） | 触发探针 | `UNKNOWN/WARN`，文案提示 `group_add` GID | 文案 + 零动作 | P0 |
| C04-27 | `dry_run=false`，`napcat` 已在运行 | 执行 `StartContainerAction` | 幂等跳过（`detail` 含"已在运行"），不重复 start | 调用记录 + detail | P1 |
| C04-28 | 连续 3 次动作失败 | 执行失败动作 3 次 | `profile=maintenance`，仅告警，FATAL 1 条 | profile + 通知条数 | P0 |

---

## 5. 边界与异常用例

| 用例ID | 前置条件 | 操作步骤 | 预期结果 | 判定依据 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| C04-29 | `DockerUnavailable` | 探针内 `inspect` 抛该异常 | `UNKNOWN/WARN`，零动作 | status + action 为空 | P0 |
| C04-30 | 非白名单容器（`postgres`） | 授权 `restart_container` | `code=not_whitelisted`，拒绝并审计 | code + 审计行 | P0 |
| C04-31 | 目标名含注入字符（`napcat;rm`） | 授权 | `code=invalid_target`，拒绝 | code 匹配 | P0 |
| C04-32 | `created` 且 `uptime<60s` | 触发探针 | `WARN/WARN`（状态转换中），不动作 | status + action 为空 | P1 |
| C04-33 | 数据盘 ≥ 95%（`self_health`） | 触发自检 | `CRIT/CRIT`，仅告警（无自动动作） | status + action 为空 | P1 |
| C04-34 | 数据盘 ≥ 85% | 触发自检 | `WARN/WARN` | status 匹配 | P2 |
| C04-35 | 本体 CPU ≥ 90% | 触发自检 | `WARN/WARN`，不自动重启 | status + action 为空 | P2 |
| C04-36 | 重启后 5 分钟内再次触发同容器 | 触发动作 | `_precheck` 命中 `RESTARTING_WINDOW_SEC` 窗口，跳过 | detail 含"重启窗口" | P1 |
| C04-37 | `_ping` 超时（3s） | 触发探针 | `UNKNOWN/WARN`，不动作 | status + 零动作 | P0 |
| C04-38 | 容器 304（已在目标态） | 执行动作 | 视为幂等成功，不计失败 | `ok=true` | P2 |
| C04-39 | `restart` 期间连接被重置 | `tolerate_disconnect=true` | 视为成功（自重启场景） | `ok=true` | P1 |
| C04-40 | 探针 `raw` 含大响应 | 触发探针 | `raw` 截断保存（≤ 4096），不影响判定 | 长度上限 | P2 |

---

## 5.1 判定短路顺序（B04 §5）

`container_health` 探针按以下顺序短路，先到先得：

| 顺序 | 条件 | status/severity | suggested | 用例 |
| --- | --- | --- | --- | --- |
| 1 | `Status=restarting` | WARN/WARN | None | C04-06 |
| 2 | 非 running（`exited/dead`） | CRIT/CRIT | start_container | C04-05 |
| 3 | `paused` | WARN/WARN | None | C04-07 |
| 4 | `created/removing` 且 uptime<60s | WARN/WARN | None | C04-32 |
| 5 | `OOMKilled=true` | CRIT/CRIT | None（转 B06） | C04-10 |
| 6 | `Health.Status=unhealthy` | CRIT/CRIT | restart_container | C04-11 |
| 7 | `restart_delta>=3` | CRIT/CRIT | None（CrashLoop） | C04-09 |
| 8 | `restart_delta>=1` | WARN/WARN | None | C04-08 |
| 9 | `Health.Status=starting` | WARN/WARN | None | C04-12 |
| 10 | 其余 | OK/INFO | None | C04-01 |

> 探针只产出指标与 `suggested`（日志辅助）；**授权依据由 `PolicyEngine` 按规则表推导**，`metrics["suggested"]` 不作为授权依据（B04 §11）。

## 5.2 降级链

```
socket HTTP API ──(可用)──► 正常判定
      │
      ├─(socket 不存在/权限不足/daemon 无响应)─► UNKNOWN + 告警（不动作）
      │        │
      │        └─(use_cli_fallback=true)─► docker CLI（verb 白名单）
      │                    │
      │                    └─(CLI 亦失败)─► UNKNOWN + 告警
      └─(容器 404)─► CRIT + start_container（若在白名单）
```

**铁律**：**绝不在不可观测时执行动作**（C04-02/03/26/29/37）。

## 5.3 自重启策略验证要点（B04 §9 / B13）

| 策略 | 断言 | 用例 |
| --- | --- | --- |
| `watchdog` | 写 `restart_request.json`（nonce/ts/target/requested_by），插件**不**调 Docker API | C04-21 |
| `docker_api` | 先回执后 `POST /containers/{id}/restart`；连接中断视为成功 | C04-22 |
| `disabled` | 直接拒绝（`code=self_restart_strategy_disabled`） | C04-23 |

宿主机侧补充（C13/C12 覆盖）：`guardian-watchdog.sh` 再校验 `target ∈ GUARDIAN_ALLOWED_TARGETS`，非白名单写 `outcome=denied`。

---

## 6. 通过标准

- 第 4 节全部 P0（C04-01/02/03/04/05/06/09/10/11/13/14/15/16/18/19/20/21/23/26/28）通过。
- 第 5 节 P0（C04-29/30/31/37）通过。
- daemon 不可达时**零动作**（C04-02/03/26/29/37）。
- `dry_run=true` 下容器状态零变化（C04-13）。
- 重启动作在 ≤ 60s 内复核 `running`（C04-16）；`G-SERVER` 在真环境 2×间隔内闭环。
- 受保护容器默认拒绝（C04-20）；三策略分支正确（C04-21/22/23）。

## 7. 证据留存

| 证据 | 命令 | 存放 |
| --- | --- | --- |
| 探针结果 | `run_once` 输出 / 单测日志 | `artifacts/c04/probe.log` |
| 容器状态前后 | `docker inspect napcat > artifacts/c04/napcat-before.json`（及 after） | `artifacts/c04/` |
| 动作调用记录 | `FakeDockerClient.calls` / 真环境 `docker events` | `artifacts/c04/calls.txt` |
| 看门狗请求 | `restart_request.json` + `journalctl -t guardian-watchdog` | `artifacts/c04/watchdog/` |
| 审计 | `audit.jsonl` 中 `restart_container` 行 | 随包归档 |

## 8. 回归建议

- 改动 `utils/dockerclient.py` / `probes/docker_daemon.py` / `probes/container_health.py` / `probes/self_health.py` / `actions/container_actions.py` 后**必跑** C04 全量。
- 改动 `core/safety.py`（白名单/冷却/熔断）后，联动回归 C04-20/23/28/30/31 与 C12。
- 每次部署变更 socket/GID 后必跑 C04-03/26（R2）。
- 每次变更自重启策略后必跑 C04-21/22/23 与 C01-31。

## 9. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-10-13 | v0.2.0 | 初版：daemon 可达性、容器状态判定、重启幂等与复核、受保护容器三策略、降级链、socket 权限 |
