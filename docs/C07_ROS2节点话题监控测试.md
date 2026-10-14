# C07 · ROS2 节点话题监控测试（Guardian 守护系统）

> 对应开发文档：`B07_ROS2模式_节点与话题健康监控.md`；关联 `B02`（模式切换）、`B12`（命令白名单）、`B03`（探针框架）。
> 对应 Gate：`G-ROS2`（期望节点缺失可告警；配恢复命令可执行并复核）。
> 上位规格：`B00_项目总览与需求规格.md`（唯一权威，v0.2.0）§4 模式、§7 配置（`ros2.*`）、§10 红线 1/8。
> 版本：v0.2.0　|　日期：2026-10-13

---

## 1. 目标

| ID | 目标 | 判据 |
| --- | --- | --- |
| C07-G1 | 期望节点缺失可发现 | `node list` 比对 `expected_nodes`，缺失 → `CRIT` |
| C07-G2 | 期望话题存在且存活 | 存在性 + `topic hz` 频率 + publisher count |
| C07-G3 | 域级问题可诊断 | `ros2 doctor` 摘要与异常行提取 |
| C07-G4 | 恢复命令白名单化执行并复核 | 仅执行配置映射命令，执行后复核 |
| C07-G5 | 双后端等价 | `local` 与 `docker_exec` 解析结果一致 |
| C07-G6 | 超时保护不阻塞事件循环 | 子进程/exec 超时返回，事件循环无阻塞 |
| C07-G7 | 与容器守护共存 | `mode=ros2` 时 `docker_daemon` 可选共存 |

---

## 2. 测试环境 / 前置

| 项 | 要求 |
| --- | --- |
| 环境 | E7（假 ros2 输出，L1）+ E1（真 ROS2 容器，L3） |
| 配置 | `mode=ros2`、`ros2.exec_backend=local|docker_exec`、`expected_nodes`、`expected_topics`、`topic_hz_min`、`node_restart_commands` |
| DDS | `ROS_DOMAIN_ID=0`、`ROS_LOCALHOST_ONLY=0`、`RMW_IMPLEMENTATION=rmw_fastrtps_cpp` 与业务侧一致 |
| 依赖注入 | `Ros2Runner`（假 `_local`/`_docker_exec`）、`FakeDockerClient`、`fake_state` |

**前置命令**：

```bash
docker exec astrbot bash -lc 'ros2 --version'                 # 期望输出发行版
docker exec astrbot bash -lc 'env | grep -E "ROS_DOMAIN_ID|ROS_LOCALHOST_ONLY|RMW_IMPLEMENTATION"'
```

---

## 3. 测试数据与夹具

### 3.1 假 ros2 输出样例

`ros2 node list`：

```
/camera_node
/lidar_node
/nav_node
```

`ros2 topic list`：

```
/camera_node/image_raw [sensor_msgs/msg/Image]
/scan [sensor_msgs/msg/LaserScan]
/parameter_events [rcl_interfaces/msg/ParameterEvent]
```

`ros2 topic hz --window 5 /scan`：

```
average rate: 9.998
	min: 0.090s max: 0.110s std dev: 0.00400s window: 5
```

`ros2 topic info /scan`：

```
Type: sensor_msgs/msg/LaserScan
Publisher count: 1
Subscription count: 2
```

`ros2 topic info /scan`（无发布者）：

```
Type: sensor_msgs/msg/LaserScan
Publisher count: 0
Subscription count: 1
```

`ros2 doctor --report`（含 warning）：

```
NETWORK CONFIGURATION
  ROS_LOCALHOST_ONLY is set, which will prevent discovery.
PACKAGE VERSIONS
  WARNING: package version mismatch detected.
```

### 3.2 夹具

| 夹具 | 用途 |
| --- | --- |
| `fake_runner(stdout_map)` | 按 verb 返回预置 stdout/stderr/rc |
| `fake_docker`（exec_capture） | `docker_exec` 后端 |
| `node_restart_commands` | 白名单映射样本（见 §3.3） |
| `timewarp` | 复核延迟与超时 |

### 3.3 恢复命令白名单样本

```json
{"ros2": {"node_restart_commands": {
  "/camera_node": "systemctl --user restart ros2-camera.service",
  "/lidar_node":  "docker restart ros2-lidar",
  "/nav_node":    "ros2 lifecycle set /nav_node deactivate"}}}
```

---

## 4. 用例表

| 用例ID | 前置条件 | 操作步骤 | 预期结果 | 判定依据 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| C07-01 | `expected_nodes` 全部存在 | 触发 `ros2_nodes` | 每节点 `OK/INFO`，汇总 `missing=[]` | status + metrics | P0 |
| C07-02 | 杀掉一个期望节点 | 触发探针 | 该节点 `CRIT/CRIT`，汇总含节点名 | status + missing | P0 |
| C07-03 | `expected_topics` 全部存在 | 触发 `ros2_topics` | 每话题存在性 `OK` | status | P0 |
| C07-04 | 移除一个期望话题 | 触发探针 | `CRIT/CRIT`，`metrics.exists=false` | status + metrics | P0 |
| C07-05 | `hz=9.998`、`topic_hz_min=5` | 触发探针 | `OK/INFO`（`hz>=min`） | status | P0 |
| C07-06 | `hz=2.1`、`topic_hz_min=5` | 触发探针 | `WARN/WARN`（`0<hz<min`） | status | P0 |
| C07-07 | `hz` 无输出（静默） | 触发探针 | `CRIT/CRIT`（`--window 5` 内无消息） | status | P0 |
| C07-08 | `Publisher count: 0` | 触发探针 | `CRIT/CRIT`（业务链路已断） | status + metrics | P0 |
| C07-09 | `topic_hz_min=0`（未配置频率） | 触发探针 | 仅判存在性/publisher，不判频率 | 无 hz Finding | P1 |
| C07-10 | `doctor` 含 warning | 触发 `ros2_doctor` | `WARN/WARN`，`metrics.warning_count>0` | status + metrics | P0 |
| C07-11 | `doctor` 含 error | 触发探针 | `Status=CRIT` 但 `Severity=WARN`（域级噪音） | status/severity | P0 |
| C07-12 | `doctor` 无异常 | 触发探针 | `OK/INFO` | status | P1 |
| C07-13 | 同命令 `local` 后端 | 运行 `node_list`/`topic_list`/`topic_hz` | 解析结果正确 | 解析输出 | P0 |
| C07-14 | 同命令 `docker_exec` 后端 | 运行同上 | 解析结果与 `local` 一致（允许顺序差异） | 集合相等 | P0 |
| C07-15 | `docker_exec` 但 `docker_container=""` | 运行命令 | `rc=127`，`detail` 含配置错误，不重试 | rc + detail | P0 |
| C07-16 | 白名单内命令（`/camera_node`） | 执行 `ros2_restart` | 命令执行 + 复核（8s 后 `node_list` 出现） | `ok=true` | P0 |
| C07-17 | 非白名单命令（构造 `ActionSpec`） | 执行动作 | 抛 `Ros2CommandError` 或 `ok=false`，审计含拒绝原因 | 异常/字段 | P0 |
| C07-18 | target 不在 `expected_nodes` | 执行动作 | `ok=false`，`detail` 含"不在 ros2.expected_nodes" | detail | P0 |
| C07-19 | `dry_run=true` | 执行动作 | `detail` 含 `dry-run`，节点未重启 | detail + 零副作用 | P0 |
| C07-20 | 恢复命令成功但节点未出现 | 执行动作 | 复核 3 次未出现 → `ok=false`，升级 FATAL + 冷却 | `ok==false` | P0 |
| C07-21 | `mode=ros2` + `docker_daemon` 启用 | 触发一轮 | 两者结果出现在同一快照，互不影响 | 结果含双方 | P0 |
| C07-22 | `docker_daemon` socket 不可用 | 触发一轮 | ROS2 探针仍独立工作，不因 Docker 异常整体跳过 | ROS2 结果存在 | P0 |
| C07-23 | `ROS_DOMAIN_ID` 不一致 | 触发 `ros2_nodes` | `node list` 为空 → `UNKNOWN/WARN` + `hint=check_ros_domain`，**不**产生 `ros2_restart` | status + 无 action | P0 |
| C07-24 | `ROS_LOCALHOST_ONLY=1` | 触发探针 | 记录告警（阻断跨容器发现），不误判为节点全死 | 告警 + status | P0 |
| C07-25 | `ros2` rc=127（未 source） | 触发探针 | `UNKNOWN/WARN`，绝不判 `OK` | status | P0 |
| C07-26 | 命令注入（`argv` 含 `;`/`$()`） | `assert_allowed_restart` | 抛 `Ros2CommandError` | 异常类型 | P0 |

---

## 5. 边界与异常用例

| 用例ID | 前置条件 | 操作步骤 | 预期结果 | 判定依据 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| C07-27 | 白名单内注入 sleep 命令 | 执行动作，`timeout=1` | `timed_out=True`、`rc=124`，事件循环未阻塞 | 字段 + 循环存活 | P0 |
| C07-28 | 名称非法（`topic="/a b"`） | `_name` 校验 | 抛 `Ros2CommandError` | 异常类型 | P1 |
| C07-29 | `topic info` 解析不出 count | 触发探针 | `WARN/WARN`（无法解析 publisher） | status | P1 |
| C07-30 | 空 `node list` 且无期望节点 | 触发探针 | 不判缺失（无期望即无缺失） | 无 CRIT | P1 |
| C07-31 | `topic_list` 输出含 ANSI 颜色 | 触发解析 | 过滤非 `/` 行，解析正确 | 集合正确 | P2 |
| C07-32 | 节点所在容器 `exited` | 触发探针 | 缺失告警降级 `WARN` 并标注 `reason=container_down` | status + reason | P1 |
| C07-33 | `ros2_restart` 冷却期内再触发 | 执行动作 | `Incident.suppressed=True`，`suppress_reason=cooldown` | 字段匹配 | P0 |
| C07-34 | 1h 内累计超 `max_actions_per_hour` | 执行动作 | `circuit_open` + FATAL，动作全部被拒 | code + 级别 | P0 |
| C07-35 | 节点抖动（相邻周期 OK/CRIT 交替） | 触发多轮 | 冷却抑制重复动作；夜间档位静默噪音 | 动作 ≤ 1 | P1 |
| C07-36 | `doctor` 报告含大写节标题 | 提取异常行 | 跳过节标题（`_SECTION_RE`），不误报 | 异常行集合 | P2 |
| C07-37 | `exec_capture` 超时 | 触发 `docker_exec` | 返回 `timed_out=True`，不阻塞 | 字段 | P1 |
| C07-38 | `use_cli_fallback=true` 且 exec 失败 | 触发 | 降级 `docker exec` CLI 或失败告警 | 降级记录 | P2 |
| C07-39 | `expected_nodes` 为空 | 触发探针 | 不产生节点 Finding（无期望） | 无 CRIT | P2 |
| C07-40 | `doctor` 执行失败（rc!=0） | 触发探针 | `UNKNOWN/WARN`，记录 stderr 摘要 | status | P1 |

---

## 5.1 域发现前置条件（B07 §11）

| # | 前置条件 | 检查方法 | 不满足表现 | 用例 |
| --- | --- | --- | --- | --- |
| P1 | `ROS_DOMAIN_ID` 与业务侧一致 | 两侧 `env \| grep ROS_DOMAIN_ID` | 完全互不可见，`node list` 为空 | C07-23 |
| P2 | `ROS_LOCALHOST_ONLY=0` | 同上 | 仅回环发现，跨容器不可见 | C07-24 |
| P3 | `RMW_IMPLEMENTATION` 一致 | 同上 | 域号相同也互不可见 | C07-23 |
| P4 | 容器网络可达（推荐 host） | `docker inspect` | 组播/单播被隔离，发现失败 | C07-21 |
| P5 | `/dev/shm` ≥ 64MB | `df -h /dev/shm` | 大消息传输失败、`hz` 抖动 | C07-06 |
| P6 | 容器内 ROS 环境已 source | `docker exec <c> bash -lc 'ros2 --version'` | rc=127，探针降级 `UNKNOWN` | C07-25 |
| P7 | `ros2` CLI 与节点版本一致 | 两侧 `ros2 --version` | 子命令参数不兼容 | C07-05 |
| P8 | `docker_exec` 目标容器在运行 | `docker ps` | exec 报 404/409，动作失败 | C07-15 |

## 5.2 恢复动作安全链（B07 §9 / B12 §5）

```
Finding(CRIT, target=/camera_node)
  └─ SafetyGuard.authorize(kind="ros2_restart", target="/camera_node")
       ├─ 白名单：target ∈ node_restart_commands         → 否则 deny(not_whitelisted)
       ├─ 命令：argv 逐字 ∈ 配置映射（assert_allowed_restart）
       ├─ 冷却窗口内已有同目标动作                        → deny(suppress_reason="cooldown")
       ├─ 窗口内动作数 > max_actions_per_hour            → deny("circuit_open") + FATAL
       ├─ dry_run=true                                   → allow(dry_run=True)
       └─ allow
  └─ Ros2RestartNodeAction.execute()
       ├─ 执行成功 → verify(): sleep 8s → node_list ×3（间隔 4s）
       │    ├─ 出现   → ActionResult(ok=True)  → Incident(recovered) → 通知"已恢复"
       │    └─ 未出现 → ActionResult(ok=False) → Incident(CRIT) → 升级 FATAL + 冷却
       └─ 执行失败 → 连续失败计数 +1；连续 3 次 → 只告警 + 切 maintenance
```

## 5.3 常见故障与判定信号（B07 §12）

| 现象 | 判定信号 | 对应用例 |
| --- | --- | --- |
| 全部期望节点同时缺失 | `node_list` 为空 + `doctor` 网络异常 → `UNKNOWN` | C07-23 |
| `ros2` rc=127 | `stderr` 含 `command not found` | C07-25 |
| `hz` 一直无输出 | `parse_topic_hz` 返回 None → `CRIT` | C07-07 |
| `hz` 抖动误报 | 已固定 `--window 5` | C07-06 |
| publisher=0 但节点在线 | `Publisher count: 0` → `CRIT` | C07-08 |
| 命令成功但复核失败 | `verify()` 三次仍未出现 → `ok=false` | C07-20 |
| docker exec 权限不足 | `Permission denied` | C07-15 |

---

## 6. 通过标准

- 第 4 节全部 P0（C07-01/02/03/04/05/06/07/08/10/11/13/14/15/16/17/18/19/20/21/22/23/24/25/26）通过。
- 第 5 节 P0（C07-27/33/34）通过。
- 期望节点缺失可告警（C07-02）；空图降级 `UNKNOWN` 不误动作（C07-23）。
- 话题存在性/频率/发布者判定正确（C07-04/06/08）。
- 白名单强制（C07-17/18/26）；恢复后复核（C07-16/20）。
- 双后端等价（C07-14）；超时保护不阻塞（C07-27）。
- `G-ROS2` 必过项 A1–A3、A7–A10 对应用例全绿。

## 7. 证据留存

| 证据 | 命令 | 存放 |
| --- | --- | --- |
| 探针结果 | `pytest tests/test_ros2.py -v` / `run_once` 输出 | `artifacts/c07/ros2.log` |
| 假 ros2 输出 | 第 3.1 节样例文件 | `artifacts/c07/outputs/` |
| 恢复命令执行 | `RunOutput`（argv/rc/stdout） | `artifacts/c07/actions/` |
| DDS 环境 | 两侧 `env | grep ROS_` | `artifacts/c07/dds.txt` |
| 审计 | `audit.jsonl` 中 `ros2_restart` 行 | 随包归档 |

## 8. 回归建议

- 改动 `utils/ros2_runner.py` / `probes/ros2_*.py` / `actions/ros2_actions.py` 后**必跑** C07 全量。
- 改动 `modes/ros2.py` 探针集后联动回归 C02-26/27 与 C07-21。
- 改动 `core/safety.py` 白名单/熔断后联动回归 C07-17/26/33/34 与 C12。
- 每次部署 DDS 环境变更后必跑 C07-23/24。

## 9. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-10-13 | v0.2.0 | 初版：节点/话题存在性、频率与发布者、doctor 诊断、双后端等价、超时保护、白名单恢复与复核、DDS 域错误、容器共存 |
