# B13 · 部署集成（Compose / Docker Socket / 看门狗自重启）

> 上游权威规格：`B00_项目总览与需求规格.md`；配置键名一律沿用 B00 第 7 节，冲突以 B00 为准。
> 版本：v0.2.0　|　日期：2026-10-14　|　**本版已对齐实现**（宿主看门狗为 `guardian-watchdog.py`，协议为 v2 群组睡眠）

> ⚠️ **v0.1.0 → v0.2.0 实现修正摘要**（详见 §16 变更记录）：
> 1. 宿主执行器已从 bash 脚本改为 **`deploy/guardian-watchdog.py`**（纯 stdlib，Linux/Windows 双平台），`guardian-watchdog.sh` 仅作兼容包装；
> 2. 请求/结果协议升级为 **schema v2**：`operation="sleep"` + `targets[]` + `duration_min`，结果字段为 **`status`**（`sleeping|completed|failed|recovery_pending|rejected`）+ `ok`，取代 v1 的单 `target` + `outcome`；
> 3. systemd 单元实测值为 `ExecStart=/usr/local/bin/guardian-watchdog.sh`、`TimeoutStartSec=40min`、`Environment=GUARDIAN_COMPOSE_DIR=/opt/astrbot-stack`；定时器用 **`OnUnitActiveSec=30s`**（非 `OnUnitInactiveSec`）；
> 4. 插件侧**不存在** `actions/self_restart.py`；写请求在 `utils/watchdog.py::submit_sleep()`，动作在 `actions/sleep.py`；
> 5. 宿主侧**没有**指数退避状态文件；失败改由 ledger 的 `recovery_pending` 阶段 + 下一 tick 自动重试承载。

## 1. 目标

| # | 目标 | 落地物 | 验收 |
| --- | --- | --- | --- |
| P1 | 一键起双容器（`napcat` + `astrbot`） | `deploy/docker-compose.yml` | C01 |
| P2 | 插件经 `docker.sock` 读状态、重启白名单容器 | `group_add` + `docker.socket_path` | C04 |
| P3 | 本体容器可被安全重启（不做容器内自杀） | 宿主机看门狗 systemd 单元 | C04 / G-STABILITY |
| P4 | 重启/睡眠请求有来源、新鲜度、白名单三重校验 | `deploy/guardian-watchdog.py` | C12 |
| P5 | 可退避、可观测、可升级、可回滚、可卸载 | journald + `/var/log/guardian-watchdog.log` + `.env` + 本文 | G-INSTALL |

## 2. 部署拓扑

```
┌──────────────────────────── Arch Linux 宿主机 ─────────────────────────────┐
│ systemd                                                                    │
│  ├── docker.service ──► dockerd ──► /var/run/docker.sock (0660 root:docker)│
│  ├── guardian-watchdog.timer (OnBootSec=60s, OnUnitActiveSec=30s)          │
│  │     └── guardian-watchdog.service (Type=oneshot, root, TimeoutStartSec=40min)
│  │            └── /usr/local/bin/guardian-watchdog.sh → exec .py           │
│  │                 ① 取 flock（非阻塞，抢不到即退出）                        │
│  │                 ② request → .processing（原子改名，claim）              │
│  │                 ③ 校验 version/nonce/ts(≤120s)/targets⊆白名单           │
│  │                 ④ stop → sleeping(wake_at) → start → running 健康复检    │
│  │                 ⑤ 写 result.json；失败落 recovery_pending，下一 tick 重试 │
│  └── docker compose -p astrbot-stack -f /opt/astrbot-stack/docker-compose.yml
│         ├── container: napcat  (OneBot11 :3000, WebUI :6099)               │
│         │      volume: ./napcat/config, ./napcat/qq                        │
│         └── container: astrbot (WebUI :6185, 额外端口 :6199)               │
│                volume: ./data:/AstrBot/data, docker.sock                   │
│                group_add: [${DOCKER_GID:-999}] ← 免 root 访问 socket       │
│                ⇅ 共享卷 ./data/guardian/{restart_request,restart_result}.json│
└────────────────────────────────────────────────────────────────────────────┘
```

三条边界：**插件只读容器状态**（走 socket）、**插件只写请求文件**（不执行重启）、**宿主机只读请求文件并执行重启**（不信任容器内输入）。请求/结果文件通过 `./data` 共享目录交换——容器内路径 `/AstrBot/data/guardian/`，宿主机路径 `<compose_dir>/data/guardian/`（`GUARDIAN_REQUEST_FILE` / `GUARDIAN_RESULT_FILE` 可覆盖）。

## 3. `deploy/docker-compose.yml`（完整）

```yaml
# deploy/docker-compose.yml —— Guardian 双容器编排（Arch Linux）
name: astrbot-stack

services:
  napcat:
    image: mlikiowa/napcat-docker:latest
    container_name: napcat
    restart: unless-stopped
    environment:
      - TZ=${TZ:-Asia/Shanghai}
      - NAPCAT_UID=${NAPCAT_UID:-0}
      - NAPCAT_GID=${NAPCAT_GID:-0}
    ports:
      - "3000:3000"   # OneBot 11 HTTP / 反向 WS
      - "6099:6099"   # NapCat WebUI（暴露风险见 §12）
    volumes:
      - ./napcat/config:/app/napcat/config
      - ./napcat/qq:/app/.config/QQ
    networks: [astrbot-net]

  astrbot:
    image: soulter/astrbot:latest
    container_name: astrbot
    restart: unless-stopped
    depends_on: [napcat]
    environment:
      - TZ=${TZ:-Asia/Shanghai}
    ports:
      - "6185:6185"   # AstrBot WebUI
      - "6199:6199"   # AstrBot 额外端口
    volumes:
      - ./data:/AstrBot/data                    # 含插件源码与 guardian 请求/结果文件
      - /var/run/docker.sock:/var/run/docker.sock
    group_add:
      - "${DOCKER_GID:-999}"                    # 加入宿主 docker 组，免 root 访问 socket
    networks: [astrbot-net]

networks:
  astrbot-net:
    driver: bridge
```

要点：`container_name` 必须等于 B00 `containers.astrbot` / `containers.napcat` 默认值；`TZ` 规避 R7（cron 按容器本地时区解释）；`restart: unless-stopped` 只覆盖「进程退出」，**不覆盖**「容器活着但 QQ 掉线」「内存泄漏」，后者由探针负责；`depends_on` 只保证启动顺序，不保证 NapCat 已登录（登录由 B05 探针负责）。

> **请求/结果文件怎么落到宿主**：compose 只把 `./data` 双向挂给 `astrbot`（`/AstrBot/data`）。插件把请求写到 `/AstrBot/data/guardian/restart_request.json`，宿主机侧即 `<compose_dir>/data/guardian/restart_request.json`。宿主看门狗默认从 `GUARDIAN_COMPOSE_DIR` 推导该目录，**不需要**额外声明独立卷，也**不需要** `GUARDIAN_RESTART_DIR` 环境变量（v0.1.0 的写法已废弃）。`./data` 只挂给 `astrbot` 一个容器（不挂给 napcat），保证写权限面最小。

## 4. Docker GID：容器内访问 `docker.sock` 的前提

socket 的访问权由**文件属组 GID** 决定，与容器是不是 root 无关。

```bash
getent group docker                 # docker:x:998:
stat -c '%g' /var/run/docker.sock   # 998      ← 最可靠
```

把结果写进 `deploy/.env`：`DOCKER_GID=998`。不要用 `privileged: true` 绕过——那会打开整个内核能力面并掩盖配置错误；正确做法是只把宿主机的 docker 组 GID 用 `group_add` 加进容器。GID 变化时（重装 docker、迁移机器）：改 `.env` → `docker compose up -d astrbot` 重建；`docker_daemon` 探针会在下次巡检发现 socket 不可用并降级告警（R2，见 B04 / B14 Playbook P2）。

## 5. 为什么不做容器内自杀式重启

| 现象 | 机制 | 后果 |
| --- | --- | --- |
| 在途请求中断 | SIGTERM 到进程销毁之间，正在跑的 `/guardian status`、LLM 工具调用、通知推送全被切断 | 用户看到无响应，回执丢失 |
| 日志丢失 | stdout/stderr 缓冲随进程销毁，崩溃前最后几条日志常来不及刷盘 | 事后无法定位根因 |
| 重启风暴 | 重启后插件重载 → 立即巡检 → 故障条件仍在 → 再次请求重启 | 无限循环，服务雪崩（**R1**） |

看门狗破局：① 请求与执行**物理分离**（容器被杀时宿主机进程仍在跑，能写完结果）；② **nonce 去重 + 新鲜度**抗旧请求重放；③ 宿主机侧 **ledger + `recovery_pending`**：一次未完成的任务被持久化，下一 tick 自动 `start` + `running()` 健康复检，直到终态落地；④ 插件侧还有 `action_cooldown_sec`、`max_actions_per_hour`、失败即停三重约束（B00 §10 第 4、6 条）。

> 说明：v0.1.0 文档描述的"宿主机指数退避状态文件"（`watchdog.fails` / `watchdog.backoff_until`）**在 Python 执行器中已不存在**。当前实现不做退避等待，而是"每个 tick 只推进一步、状态持久化、失败不丢弃义务"——因为 systemd timer 本身就是天然的节流器（30s 一次），且睡眠任务可能持续数十分钟，退避反而会拖长恢复时间。

## 6. 自重启三策略对比与选择

配置键 `safety.self_restart_strategy`（B00 §7）。

| 维度 | `watchdog`（默认） | `docker_api` | `disabled` |
| --- | --- | --- | --- |
| 执行者 | 宿主机 systemd oneshot | 容器内 aiohttp | 无人 |
| 延迟 | ≤ 一个 timer 周期（默认 30s） | 立即（需先发回执） | — |
| 依赖 | systemd + 共享卷 + compose 文件 | socket 可 POST | — |
| 副作用 | 不中断在途请求；日志双份完整 | 中断在途请求；重启前日志易丢 | — |
| 风暴风险 | 低（宿主机退避） | 中（依赖插件冷却） | 无 |
| 推荐场景 | **生产默认** | 无 systemd（WSL / 托管平台） | 调试、演示、多副本 |

选择建议：单机 Arch + systemd → `watchdog`；拿不到宿主机 systemd → `docker_api`（冷却 ≥ 600s）；只观测不动手 → `disabled`（配 `dry_run: true`）。无论选哪个，`safety.allow_self_restart` 必须显式为 `true` 才允许重启本体（B00 §10 第 3 条）。

> **实现落点（与上表的关系）**：`self_restart_strategy` 只管辖**涉及本体容器**（`config.is_self(t)`）的睡眠/重启。`actions/sleep.py` 的实际分支是：
> - 目标含本体 → 走 `submit_sleep()`（看门狗路径），与 `strategy` 值无关；`strategy` 目前只用于 dry-run 文案与"是否禁用"的判定；
> - 目标**不含**本体（纯 napcat 等）→ 直接走 Docker Engine API（`stop → sleep → start`，`duration>0`）或 `restart`（`duration==0`），**不经过宿主机看门狗**。
>
> 也就是说：**只有"需要自己重启自己"的场景才必须依赖宿主机看门狗**——这正是 §5 说的"容器内自杀会死"。napcat 不是本插件宿主进程，插件可以在容器内直接把它停掉再拉起。

## 7. 看门狗实现

### 7.1 请求 / 结果文件协议（schema v2）

插件侧写入函数：`src/astrbot_plugin_guardian/utils/watchdog.py::submit_sleep()`。

`restart_request.json`（容器 → 宿主机）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `version` | int | 固定 `2`。宿主执行器同时兼容 v1（无 `version` 字段、单 `target`、`operation=restart`），但插件只发 v2 |
| `operation` | string | 固定 `"sleep"`（群组睡眠：先 stop，等到 `wake_at` 再 start） |
| `nonce` | string | `uuid4().hex`，去重与幂等键（宿主用 `[a-zA-Z0-9_-]{1,128}` 校验） |
| `ts` | float | 生成时刻 epoch 秒；宿主判新鲜度，容忍未来 60s、过去 `GUARDIAN_MAX_AGE_SEC`（默认 120s，上限 120s） |
| `targets` | list[str] | 目标容器名，1–10 个、去重、每个须 ∈ 宿主白名单且匹配 `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$` |
| `duration_min` | int | 睡眠时长（0–120 分钟）；`0` 表示"重启型"（stop 后立即 start） |
| `reason` | string | 人类可读原因，写入审计 |
| `requested_by` | string | 固定 `"astrbot_plugin_guardian"` |

**发布机制（关键，勿改）**：插件先用 `os.link(tmp, request_path)` 做**原子 no-clobber 发布**（绝不覆盖已排队的请求），发布前校验 `watchdog.pending` 已落盘；若发现 `restart_request.json.processing` 存在（宿主正在消费上一个请求），直接放弃本次发布。

`restart_result.json`（宿主机 → 容器）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `nonce` | string\|null | 回填配对；非法请求可能为 `null` |
| `ts` | float | 宿主写出时刻 |
| `ok` | bool | `status == "completed"` 时为 `true`，否则 `false` |
| `status` | string | `sleeping` / `completed` / `failed` / `recovery_pending` / `rejected` |
| `targets` | list[str] | 本次作用目标 |
| `target` | string\|null | `targets[0]`，兼容字段 |
| `detail` | string | 机器可读细节，如 `stopped` / `awakened` / `restarted` / `replay` / `busy` / `target-not-allowed` / `stale-request` / `start-or-verify-failed:xxx` |
| `wake_at` | float\|null | 唤醒时刻（`sleeping` 时有效） |
| `operation` | string\|null | `"sleep"` 或 `"restart"` |

**状态语义**（插件侧 `utils/watchdog.py::reconcile()` 的判定依据）：

| `status` | 含义 | 插件动作 |
| --- | --- | --- |
| `sleeping` | 目标已 stop，正在等到 `wake_at` | **不结算**，保留 `watchdog.pending`，下轮继续等 |
| `completed` + `ok=true` | 全部目标已 start 且 `running()`+健康复检通过 | 调用 `on_sleep_done()`（清 `forced_at`/`sleep_armed`、记 `last_sleep_at`），清 pending，通知 |
| `failed` + `ok=false` | 终态失败 | 对各目标 `bump_fail_streak`，清 pending，升级 CRIT 通知 |
| `rejected` + `ok=false` | 请求被拒（重放 / 宿主忙 / 校验失败） | 同上按失败结算 |
| `recovery_pending` | 中间态：start 或健康复检未过，义务仍保留在 ledger | **不结算**，宿主下一 tick 自动重试；插件继续持有 pending |

> 只有 `completed`/`failed`/`rejected` 是终态。`sleeping` 与 `recovery_pending` 都**必须**保留 `watchdog.pending`——否则插件会误判"已睡完"而漏记失败。

### 7.2 `deploy/guardian-watchdog.py`（宿主执行器，纯 stdlib）

**设计要点**：单文件、纯标准库（`argparse/json/math/os/pathlib/re/subprocess/tempfile/time`），**不 import 任何 AstrBot 或第三方库**，因此宿主机不需要装 Python 依赖，也不会因插件版本变化而失效。Linux 走 systemd oneshot（一 tick 一次），Windows 可 `python guardian-watchdog.py --poll-interval 30` 前台轮询。

关键结构（阅读源码请对照 `deploy/guardian-watchdog.py`）：

| 组件 | 职责 |
| --- | --- |
| `Config` | 从环境变量读取 `GUARDIAN_COMPOSE_DIR`（默认 `/opt/astrbot-stack`）、`GUARDIAN_REQUEST_FILE` / `GUARDIAN_RESULT_FILE`（默认 `<compose_dir>/data/guardian/` 下）、`GUARDIAN_ALLOWED_TARGETS`、`GUARDIAN_MAX_AGE_SEC`（硬上限 120s）、`GUARDIAN_CMD_TIMEOUT`、`GUARDIAN_DOCKER_BACKEND`（`compose`/`container`）；校验路径互不重叠 |
| `AdvisoryLock` | 非阻塞 OS 锁（POSIX `fcntl.flock` / Windows `msvcrt.locking`），**从不 unlink 锁文件**（避免 inode 竞争） |
| `Docker` | `compose`（默认，`docker compose --project-directory <dir> ...`）或 `container` 后端；`action()` 执行 stop/start/restart；`running()` 用 `compose ps -q` + `docker inspect` 复检 `Running` 且 `Health.Status == healthy` |
| `Watchdog` | ledger 状态机：`validate_request` → `consume` → `stop`/`restart` → `recover` → `finish` |

**ledger（`watchdog_state.json`）**：`{"active": <job|null>, "seen": {nonce: ts}}`。`active` 是**持久化的义务**，字段含 `phase`（`stopping`/`sleeping`/`starting`/`restarting`）、`attempted[]`、`started[]`、`failed`、`detail`、`wake_at`。`seen` 记录已受理 nonce（`save()` 时按 `max_age+60` 清理旧记录）。

**tick 流程**（`Watchdog.tick()`，每个 timer 触发执行一次）：

```
① 取 flock；抢不到 → 直接返回（不阻塞 systemd）
② load_ledger() 并做严格结构校验（非法即抛 invalid-ledger → recovery_pending/rejected）
③ 若 ledger.active 非空 → resume(active)      # 崩溃/睡眠中的任务优先续跑
④ 若 request.json.processing 存在 → consume(previous=True)   # 上次崩溃留下的 claim
⑤ 若 request.json 存在 → os.replace(request → .processing)   # 原子 claim，防并发
   → consume()：校验 → 查重/查忙 → 受理进 ledger → stop/restart
⑥ 写 result.json（原子 rename + fsync）；终态时 finish() 清 active
```

**关键安全设计**：
- **`.processing` claim 文件**：请求先被 `os.replace` 改名为 `request.json.processing` 再消费，保证同一请求不会被两个 tick 同时处理，也不会被插件新发布的请求覆盖（发布侧检测到 `.processing` 存在即放弃）。
- **严格 JSON**：`strict_json` 拒绝 `NaN`/`Infinity`（`parse_constant` 抛错），避免畸形请求污染 ledger。
- **路径不相交校验**：`request`/`processing`/`result`/`state`/`lock`/`log` 六个路径 resolve 后必须两两不同。
- **非终态不丢义务**：任何 `OSError`/`ValueError` 都落到 `recovery_pending`（有 active job）或 `rejected`（无），绝不静默吞掉。

`deploy/guardian-watchdog.sh` 只是兼容包装：

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${GUARDIAN_PYTHON:-${PYTHON:-python3}}" "$HERE/guardian-watchdog.py" "$@"
```

依赖：`python3`（≥3.8 即可）+ `docker`（含 compose 插件，或 `GUARDIAN_DOCKER_BACKEND=container`）。执行器只调用 `docker compose stop/start/restart <target>` 与 `docker inspect`，**不接受任何自由命令**。

### 7.3 `deploy/guardian-watchdog.service`

```ini
# Host watchdog: one tick per invocation, including saved wake deadlines.
[Unit]
Description=Guardian Host Watchdog (Docker restart and timed sleep)
Documentation=file:/usr/local/bin/guardian-watchdog.py
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
TimeoutStartSec=40min
Environment=GUARDIAN_COMPOSE_DIR=/opt/astrbot-stack
Environment="GUARDIAN_ALLOWED_TARGETS=astrbot napcat"
Environment=GUARDIAN_MAX_AGE_SEC=120
ExecStart=/usr/local/bin/guardian-watchdog.sh
Nice=5
```

要点：
- **`TimeoutStartSec=40min`**：不是笔误。一次睡眠任务可能包含 `stop` + 最长 120 分钟的等待 + `start`——但执行器是"每 tick 推进一步"，等待阶段只返回 `sleeping` 不阻塞，所以 40 分钟只覆盖最坏的单次 docker 命令 + 恢复重试。若你把 `GUARDIAN_CMD_TIMEOUT` 调大或 compose 后端很慢，需同步放宽。
- **`GUARDIAN_COMPOSE_DIR=/opt/astrbot-stack`**：执行器据此推导 `<dir>/data/guardian/restart_request.json`，必须与 compose 实际部署目录一致。
- `Nice=5`：宿主机重启动作让出 CPU，避免与容器争抢。
- 未启用 `ProtectSystem`/`ReadWritePaths`：执行器需读 compose 文件、写 `<compose_dir>/data/guardian/` 与 `/var/log/`，且要调 `docker`。若宿主加固要求，可加 `ReadWritePaths=/opt/astrbot-stack /var/log`，但**不要**把 `ProtectSystem=strict` 与 docker CLI 混用而不验证（docker CLI 需要访问 socket 与配置）。

### 7.4 `deploy/guardian-watchdog.timer`

```ini
# Check host requests and persisted wake deadlines every 30 seconds.
[Unit]
Description=Guardian Watchdog Timer (poll requests and wake deadlines)
Requires=guardian-watchdog.service

[Timer]
OnBootSec=60s
OnUnitActiveSec=30s
AccuracySec=5s
Unit=guardian-watchdog.service

[Install]
WantedBy=timers.target
```

用 **`OnUnitActiveSec=30s`**（自上次**激活**起 30s 再触发），**不是** `OnUnitInactiveSec`。这是刻意的：睡眠任务可能长时间处于 `sleeping`（一次 oneshot 里不阻塞、只是返回 `sleeping` 状态），timer 必须**持续**每 30s 唤醒执行器去检查 `wake_at` 是否到点，否则睡下就再也不会醒。oneshot 单次执行很快返回，所以不会堆叠；`AccuracySec=5s` 允许 systemd 合并唤醒以省电。同时 `Requires=guardian-watchdog.service` 保证依赖的 oneshot 单元存在。

> ⚠️ v0.1.0 文档写的是 `OnUnitInactiveSec=30s`——那会导致"上一次跑完才开始计时下一次"，配合长睡眠会让唤醒严重延迟。**以本版为准。**

### 7.5 安装与启用

```bash
# 1) 部署 compose 栈（把仓库里的 deploy/ 与 src/ 放到宿主机固定目录）
sudo mkdir -p /opt/astrbot-stack
sudo cp -a deploy/docker-compose.yml deploy/.env.example /opt/astrbot-stack/
sudo cp -a src /opt/astrbot-stack/          # 插件源码（会挂进 astrbot 容器）
cd /opt/astrbot-stack && sudo cp .env.example .env   # 填 DOCKER_GID 等

# 2) 安装宿主看门狗（.sh 与 .py 必须放在同一目录，.sh 靠相对路径找 .py）
sudo install -m 0755 deploy/guardian-watchdog.sh /usr/local/bin/guardian-watchdog.sh
sudo install -m 0644 deploy/guardian-watchdog.py /usr/local/bin/guardian-watchdog.py
sudo install -m 0644 deploy/guardian-watchdog.service /etc/systemd/system/
sudo install -m 0644 deploy/guardian-watchdog.timer   /etc/systemd/system/

# 3) 若部署目录不是默认值，改 service 里的 GUARDIAN_COMPOSE_DIR
sudo systemctl edit --full guardian-watchdog.service   # 或直接改 /etc/systemd/system/...

# 4) 起容器 + 起 timer
docker compose -f /opt/astrbot-stack/docker-compose.yml up -d
sudo systemctl daemon-reload
sudo systemctl enable --now guardian-watchdog.timer
systemctl list-timers guardian-watchdog.timer
tail -f /var/log/guardian-watchdog.log          # 执行器自己的日志

# 5) 端到端自检：手工造一个 v2 睡眠请求（duration=0 → 等价重启），30~60s 后查结果
sudo python3 -c "
import json,time,uuid
json.dump({'version':2,'operation':'sleep','nonce':uuid.uuid4().hex,'ts':time.time(),
 'targets':['napcat'],'duration_min':0,'reason':'smoke','requested_by':'manual:install_check'},
 open('/opt/astrbot-stack/data/guardian/restart_request.json','w'))"
sleep 40; cat /opt/astrbot-stack/data/guardian/restart_result.json
# 期望 {"ok": true, "status": "completed", "detail": "restarted", ...}
```

失败自检：`target-not-allowed` → 查 `GUARDIAN_ALLOWED_TARGETS`；`stale-request` → 查时钟（`timedatectl`、`chronyc tracking`）；`recovery_pending` → 目标起不来，看 `detail` 里的 `start-or-verify-failed:<target>` 与 `docker ps -a`；`busy` → 上一个任务还没跑完，等下一 tick。

### 7.6 `deploy/.env.example`

```dotenv
# cp .env.example .env 后按实际值修改
TZ=Asia/Shanghai                 # cron 按此时区解释
DOCKER_GID=999                   # getent group docker / stat -c '%g' /var/run/docker.sock
NAPCAT_UID=0                     # NapCat 运行用户（0 = root）
NAPCAT_GID=0
```

> `.env` 只管 compose 的容器编排变量。**宿主看门狗的环境变量不走 `.env`**——它们写在 `guardian-watchdog.service` 的 `Environment=` 行里（见 §7.3），或用 `systemctl edit` 覆盖。v0.1.0 文档里 `GUARDIAN_RESTART_DIR` 写进 `.env` 的做法已废弃。

## 8. 插件侧：写重启/睡眠请求（`watchdog` 策略）

> 实现位置：`src/astrbot_plugin_guardian/utils/watchdog.py::submit_sleep()`，由 `actions/sleep.py::SleepAction.execute()` 在"目标含本体容器"时调用。**不存在** `actions/self_restart.py`（v0.1.0 文档的臆造文件名）。

```python
# src/astrbot_plugin_guardian/utils/watchdog.py（节选，v2 发布逻辑）
async def submit_sleep(engine, spec, targets, duration, reason) -> str:
    path = Path(engine.config.safety.watchdog_request_path)
    if not engine.config.safety.watchdog_request_path:
        raise DockerError("watchdog_request_path 未配置")
    if engine.state.get("watchdog.pending"):
        raise DockerError("已有未完成的宿主机任务，不能覆盖")
    nonce = uuid.uuid4().hex
    payload = {"version": 2, "operation": "sleep", "nonce": nonce, "ts": time.time(),
               "targets": targets, "duration_min": duration, "reason": reason,
               "requested_by": "astrbot_plugin_guardian"}
    engine.state.set("watchdog.pending", {"nonce": nonce, "targets": targets,
                                          "ts": payload["ts"], "operation": "sleep"})
    # 先记账再发布：宿主机可能立刻杀掉本进程。
    # 快照 accounting，若发布从未可见，则改判为"诚实失败"（而非假成功）。
    accounting = engine.state.capture(("cooldowns", "action_history", "fail_streak"))
    engine.safety.commit(spec, True)
    spec.params["_watchdog_committed"] = True
    await engine.state.flush(force=True)
    ...
    def publish():
        # 校验 watchdog.pending 已落盘；.processing 存在则拒绝（宿主正在消费）
        ...
        os.link(temporary, path)     # 原子 no-clobber 发布：绝不覆盖已排队请求
    try:
        await asyncio.to_thread(publish)
    except Exception as exc:
        # 未发布成功 → 回滚乐观记账 + 记真实失败（消耗的配额/冷却不退还，
        # 使"宿主链路坏掉"能降级为只告警，而不是无限重试）
        engine.state.set("watchdog.pending", None)
        engine.state.restore(accounting)
        engine.safety.commit(spec, False)
        await engine.state.flush(force=True)
        raise DockerError(...)
    return nonce
```

**结果配对**：`engine` 每轮调用 `utils/watchdog.py::reconcile()` 读 `restart_result.json`，按 `nonce + targets + operation` 三重匹配，只有 `completed`/`failed`/`rejected` 终态才结算（见 §7.1 状态表）。读不到不算失败——重启后进程已换代，靠审计与宿主日志兜底。

动作层调用点（`actions/sleep.py`，安全校验之后）：

```python
# actions/sleep.py（节选）
decision = self.engine.safety.authorize(spec)
if not decision.allowed:
    return result(False, decision.reason)
if spec.dry_run or decision.dry_run:
    return result(True, f"[dry-run] 将睡眠 {targets}，duration={duration}min，strategy=...")
...
if any(self.config.is_self(t) for t in targets):          # 目标含本体 → 走宿主机看门狗
    nonce = await submit_sleep(self.engine, spec, targets, duration, reason)
    return result(True, f"宿主机睡眠请求已提交（nonce={nonce}）...", "watchdog-submitted")
# 目标不含本体 → 插件自己 stop/sleep/start 或 restart（不走看门狗）
```

> 注意：**"提交成功"≠"睡完"**。`submit_sleep()` 返回只代表请求已原子发布到共享卷；真正的睡眠完成由宿主执行器推进，并由 `reconcile()` 在后续轮次结算。

## 9. 安全约束（纵深防御）

| 层 | 约束 | 位置 | 违反后果 |
| --- | --- | --- | --- |
| L1 配置 | `safety.allowed_containers` ∪ `containers.managed` 白名单 | 插件 | 拒绝 + 审计 |
| L2 安全层 | 冷却 / 熔断 / dry-run | `core/safety.py` | 抑制 + 升级告警 |
| L3 权限 | `llm_tools.require_admin` + 危险确认 | `tools/llm_tools.py` | 非管理员拒绝（R4） |
| L4 协议 | 请求文件只含枚举目标，无自由命令 | `restart_request.json` | 无法诱导宿主机执行任意命令 |
| L5 宿主机 | **再次**校验 `targets ⊆ GUARDIAN_ALLOWED_TARGETS` + 字符集 + nonce + 新鲜度 | `deploy/guardian-watchdog.py` | `status=rejected` |

L5 的意义：即使容器被攻破、请求文件被改成 `targets: ["postgres"]`，宿主机也只拒绝并记录 `status=rejected / detail=target-not-allowed`。**容器内写权限 ≠ 宿主机执行权限**，这是本设计最重要的边界。补充规则：共享目录 `./data` 只挂给 `astrbot` 一个容器；目标名须匹配 `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$`（杜绝 `--force`、`$(...)` 注入）；`nonce` 须匹配 `^[a-zA-Z0-9_-]{1,128}$`；插件只读结果文件，不据其反向触发动作（避免环状触发）。

## 10. `docker_api` 策略（备选）

> ⚠️ v0.1.0 文档把这段代码放在"`actions/self_restart.py`"下——该文件不存在。实际 `docker_api` 能力由 `utils/dockerclient.py`（`DockerClient.restart()`，走 Engine API）承载，`actions/sleep.py` 在"目标不含本体"时直接调用它。以下片段仅示意 Engine API 调用形态。

```python
# utils/dockerclient.py（Engine API 直连示意）：POST /containers/{name}/restart
import aiohttp

async def restart_via_docker_api(socket_path: str, container: str,
                                 api_version: str = "v1.44", wait_sec: int = 10,
                                 timeout: float = 10.0) -> int:
    url = f"http://localhost/{api_version}/containers/{container}/restart?t={wait_sec}"
    connector = aiohttp.UnixConnector(path=socket_path)
    async with aiohttp.ClientSession(connector=connector,
            timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.post(url) as resp:
            await resp.read()
            return resp.status          # 204 = 成功
```

> **重要**：`docker_api` 策略**只能用于"非本体容器"**。若目标是本体（`config.is_self()`），插件进程会被自己的 SIGTERM 杀死，`docker_api` 之后没有任何代码能跑（详见 §5）。因此对本体，`actions/sleep.py` 恒走看门狗；`docker_api` 策略对本体无效。

时序（仅适用于非本体容器）：可以先 `await ctx.send_message(...)` 回执再调用；因为插件进程不会死，无需"先回执再重启"的抢时间技巧。

风险提示：① 在途请求可能中断（若误用于本体）；② 日志丢失（同上）；③ 重启风暴——必须保留 `action_cooldown_sec ≥ 600`、`max_actions_per_hour ≤ 6`、失败即停（B00 §10 第 6 条）；④ 审计缺口——建议宿主机旁路记录 `docker events --filter container=astrbot --filter event=restart`；⑤ 依赖 socket 写权限，组权限错会 `Permission denied`（R2）。

## 11. `socket-proxy`（技术预览，Roadmap）

直挂 `/var/run/docker.sock` 等于把宿主机 root 权限交给容器。最小权限替代方案：

```yaml
# 技术预览：本期未纳入正式交付
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy:latest
    container_name: guardian-socket-proxy
    restart: unless-stopped
    environment:
      CONTAINERS: "1"    # 只放行只读查询 + 重启所需最小集合
      POST: "1"
      RESTART: "1"
      EXEC: "0"          # 显式关闭：exec / 镜像 / 卷 / 网络 / 认证
      IMAGES: "0"
      AUTH: "0"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [astrbot-net]
```

| 项 | 直挂 socket（本期） | socket-proxy（预览） |
| --- | --- | --- |
| 权限面 | 全量 Docker API（等价 root） | 白名单端点 |
| 对 `docker.socket_path` 的影响 | 直接可用 | 需改 `DOCKER_HOST=tcp://...:2375`，`utils/dockerclient.py` 需支持 |
| 本期状态 | 默认 | 未交付，列为 B14 Roadmap |

## 12. Arch Linux 特有注意事项

| 项 | 说明 / 命令 |
| --- | --- |
| systemd 版本 | 滚动更新，systemd ≥ 250 是常态；本单元只用 `oneshot` / `OnUnitActiveSec` / `AccuracySec` / `Environment=`，兼容无忧（`systemctl --version`） |
| 服务名 | `docker` 包提供的是 `docker.service`，不是 `dockerd.service`（`systemctl status docker`） |
| 组变更生效 | `usermod -aG docker <user>` 需重新登录；容器侧靠 `group_add` 规避 |
| pacman 升级 docker | 升级后旧 `dockerd` 仍占 socket，可能版本错配：`pacman -Syu docker && sudo systemctl restart docker` |
| socket 重建 | 重启 docker 会重建 `/var/run/docker.sock`，GID 可能变化 → 重跑 `stat -c '%g' /var/run/docker.sock` 并核对 `.env` |
| firewalld / nftables | Docker 自动插入 `DOCKER` 链**绕过 `INPUT` 默认策略**，不要以为默认 DROP 就安全（`nft list ruleset \| grep 6099`） |
| 时钟 | cron 与新鲜度校验都依赖时钟（`timedatectl`、`chronyc tracking`、`docker exec astrbot date`） |

**6099 暴露风险专项**：NapCat WebUI 是攻击面，公网暴露等于邀请扫描器。三选一收敛：① compose 改为 `127.0.0.1:6099:6099`；② `firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.0.0.0/8" port port="6099" protocol="tcp" accept'`；③ 只走 SSH 隧道 `ssh -L 6099:127.0.0.1:6099 host`。无论哪种，先 `firewall-cmd --permanent --remove-port=6099/tcp` 清理历史放行。

## 13. 升级与回滚

```bash
# 升级：插件侧（插件目录是 bind mount，改完重启容器即可）
cp -a data data.bak.$(date +%Y%m%d%H%M)
git -C /opt/astrbot-stack/src pull --ff-only
docker compose -f /opt/astrbot-stack/docker-compose.yml up -d astrbot && docker logs -f astrbot | grep -i guardian
# 升级：宿主机侧（动看门狗：.sh 与 .py 必须一起更新）
sudo cp -a /usr/local/bin/guardian-watchdog.sh /usr/local/bin/guardian-watchdog.sh.bak
sudo cp -a /usr/local/bin/guardian-watchdog.py /usr/local/bin/guardian-watchdog.py.bak
sudo install -m 0755 deploy/guardian-watchdog.sh /usr/local/bin/guardian-watchdog.sh
sudo install -m 0644 deploy/guardian-watchdog.py /usr/local/bin/guardian-watchdog.py
sudo install -m 0644 deploy/guardian-watchdog.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart guardian-watchdog.timer
# 回滚
git -C /opt/astrbot-stack/src checkout <last_good_tag>
docker compose -f /opt/astrbot-stack/docker-compose.yml restart astrbot
mv data data.broken && mv data.bak.YYYYMMDDHHMM data    # 仅数据损坏时（会丢最新事件）
sudo cp -a /usr/local/bin/guardian-watchdog.sh.bak /usr/local/bin/guardian-watchdog.sh
sudo cp -a /usr/local/bin/guardian-watchdog.py.bak /usr/local/bin/guardian-watchdog.py
sudo systemctl restart guardian-watchdog.timer
```

回滚触发条件：升级后 30 分钟内出现「连续重启 ≥ 3 次」「`/guardian status` 无响应」「事件循环阻塞告警」之一。

## 14. 卸载

```bash
# 顺序很重要：先停 timer，再停容器。反过来会出现"容器已停、看门狗仍在轮询"的日志噪声。
sudo systemctl disable --now guardian-watchdog.timer && sudo systemctl stop guardian-watchdog.service
sudo rm -f /etc/systemd/system/guardian-watchdog.{service,timer}
sudo rm -f /usr/local/bin/guardian-watchdog.{sh,py}
sudo systemctl daemon-reload
docker compose -f /opt/astrbot-stack/docker-compose.yml down    # 保留数据卷/目录，便于回滚
rm -rf /opt/astrbot-stack/data /opt/astrbot-stack/napcat/config # 不可逆
```

## 15. 验收标准（对应 C01 / C04）

| # | 步骤 | 预期 | 证据 | 用例 |
| --- | --- | --- | --- | --- |
| A1 | `docker compose up -d` | 两容器 `Up` | `docker compose ps` | C01-01 |
| A2 | 容器内 `ls -l /var/run/docker.sock` | 可见，属组 GID == `.env` 的 `DOCKER_GID` | 命令输出 | C01-02 |
| A3 | 插件加载 | 日志出现 guardian 启动行，无 Traceback | `docker logs astrbot` | C01-03 / G-INSTALL |
| A4 | `/guardian status` | 返回快照文本，`docker_daemon=ok` | 聊天截图 + 日志 | C01-04 |
| A5 | 手工写 v2 请求 `targets=["napcat"], duration_min=0` | ≤ 60s 内结果 `status=completed, ok=true`，`napcat` `StartedAt` 更新 | 两文件 + `docker inspect` | C04-05 |
| A6 | 写请求 `targets=["postgres"]` | `status=rejected, detail=target-not-allowed`，容器未重启 | 结果文件 + 宿主日志 | C04-06 / G-SAFETY |
| A7 | 请求 `ts` 改为 1 小时前 | `status=rejected, detail=stale-request` | 结果文件 | C04-07 |
| A8 | 同一 nonce 连写两次 | 第二次 `status=rejected, detail=replay`，只重启一次 | 宿主日志行数 | C04-08 |
| A9 | `systemctl stop docker` 后巡检 | `docker_daemon` 转 `crit`，插件降级告警不崩溃 | `/guardian status` | C04-09 / R2 |
| A10 | `.env` 的 GID 改错后重建 | 探针报权限错误并告警，无未捕获异常 | 日志 + status | C04-10 |
| A11 | `duration_min=1` 的睡眠请求 | 先 `status=sleeping`（`wake_at` 有效），约 1 分钟后转为 `completed` | 两次读结果文件 | C04-11 |
| A12 | 目标起不来（如镜像损坏） | `status=recovery_pending`，下一 tick 自动重试；插件保持 `watchdog.pending` | 结果文件 + 审计 | C04-12 |

## 16. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-10-13 | v0.1.0 | 初版：compose 编排、GID 说明、三策略对比、看门狗脚本/单元/定时器、安装/升级/回滚/卸载、C01/C04 验收 |
| 2026-10-14 | v0.2.0 | **对齐实现**：① 宿主执行器改为 `guardian-watchdog.py`（纯 stdlib，双平台），`.sh` 降级为包装；② 协议升级 schema v2（`operation/targets/duration_min` + `status/ok`），补全状态机与 `.processing` claim、`os.link` no-clobber 发布；③ systemd 单元实测值（`/usr/local/bin`、`TimeoutStartSec=40min`、`OnUnitActiveSec=30s`、`GUARDIAN_COMPOSE_DIR=/opt/astrbot-stack`）；④ 删除臆造的 `actions/self_restart.py`，改为 `utils/watchdog.py::submit_sleep()` + `actions/sleep.py`；⑤ 删除不存在的"宿主指数退避状态文件"，改为 ledger `recovery_pending`；⑥ compose 对齐（`name: astrbot-stack`、`./data` 共享、无独立 `guardian-data` 卷）；⑦ 补 A11/A12 验收 |
