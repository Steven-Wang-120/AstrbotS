# AstrbotS · 守护系统 Guardian

面向 **Arch Linux + Docker Compose（napcat / astrbot 双容器）** 的 AstrBot 守护插件工程。
本目录同时包含**开发文档（B 系列）**、**测试文档（C 系列）**、**插件源码**与**部署编排**。

---

## 目录导航

```
AstrbotS/
├── docs/          B00–B17 开发/施工文档；C00–C17 测试文档；D00–D02 实机验收/缺陷报告
├── src/astrbot_plugin_guardian/   插件源码（直接放进 AstrBot 的 data/plugins/）
├── deploy/        docker-compose.yml + 看门狗（systemd 单元/定时器/脚本）+ 内核镜像
└── tests/         自检测试（无需 AstrBot 即可运行）
```

### 建议阅读顺序

1. `docs/B00_项目总览与需求规格.md` —— **唯一权威规格**，先读它
2. `docs/B15_生命周期等级与自主睡眠机制.md` —— 核心机制（等级 + 自主睡眠 + 30 天保证）
3. `docs/B16_独立内核服务架构.md` —— 三域分离（内核/适配/看门狗）与 M0–M5 迁移（**已完成**）
4. `docs/B01` → `B14` —— 按模块的施工细节
5. `docs/C00` → `C17` —— 测试用例与验收判据（C13 为极端/故障注入，C16 为独立内核专项，C17 为账密登录恢复专项）
6. `docs/B17` —— **QQ 账密登录恢复阶梯**（v0.6.0）：会话失效时的无人值守恢复、失败分级、离线可观测
7. `docs/D02` —— **B17 真机验收报告**：K-01..K-10 结果、环境阻塞根因、缺陷 F22–F25
8. `src/astrbot_plugin_guardian/README.md` —— 安装与配置速查

---

## 快速开始

```bash
# 1) 部署双容器（Arch Linux）
cd /opt/astrbot-stack
cp <repo>/deploy/.env.example .env      # 填 DOCKER_GID（stat -c '%g' /var/run/docker.sock）
cp <repo>/deploy/docker-compose.yml .
mkdir -p data napcat/config && docker compose up -d

# 2) 安装插件
cp -r <repo>/src/astrbot_plugin_guardian /opt/astrbot-stack/data/plugins/
#   然后在 AstrBot WebUI → 插件 页重载

# 3) 部署自重启看门狗（推荐）
sudo install -m 755 deploy/guardian-watchdog.sh /usr/local/bin/guardian-watchdog.sh
sudo install -m 644 deploy/guardian-watchdog.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now guardian-watchdog.timer

# 4) 运行自检
bash tests/run_all.sh
```

首次部署请保持 `dry_run: true`，观察日志确认判定正确后再关闭。

---

## 三条核心约定

1. **双模式**：`server`（容器 / QQ / 资源）与 `ros2`（节点 / 话题），可切换。
2. **QQ 登录态独立巡检**：默认每 **20 分钟**（`qq.check_interval`）检查一次，掉线按策略重登。
3. **自主触发，零关键词**：插件**绝不**匹配对话关键词。动作只来自
   ① 调度器周期巡检 ② 生命周期等级推进 ③ Bot 主动调用工具。
   生命周期等级按容器运行时长分级（阈值**前端可配**），每级注入不同提示词引导 Bot
   自主决定休息；`max_uptime_hours`（默认 720h = 30 天）为硬上限，到达强制真睡眠。

---

## 自检结果

`tests/` 下共 **7 个自检脚本 + 1 个看门狗脚本**，合计 **312 项断言全绿**，覆盖核心逻辑、
探针、插件装载、内核解耦、内核 IPC 契约、**M4/M5 内核守护与直连动作**、对抗性边界与看门狗：

| 脚本 | 覆盖 | 项数 |
| --- | --- | --- |
| `test_core_logic.py` | cron 解析、生命周期等级、配置校验、安全护栏、判定引擎、调度器、内存斜率；**+ 容器名一致性（本体永远受保护、`@self`/`@napcat` 别名）** | 38 |
| `test_probes_e2e.py` | 9 个探针 + 4 个动作 + 模式装配 + 通知器（fixtures 假数据）+ F11 活跃度弱正据；**+ `self_health` 标签跟随容器名** | 35 |
| `test_plugin_load.py` | 插件装载、命令/工具/钩子注册、提示词注入、硬上限睡眠、零关键词反证 | 25 |
| `test_kernel_decoupling.py` | 内核侧模块不得 `import astrbot`（静态守卫 + 阻断 astrbot 的动态导入） | 5 |
| `test_kernel_ipc.py` | **内核 IPC 契约**（B16 §10）：全 method 请求/响应/错误/超时/断连；断连必须显式失败；D4-a+ 自修复有界性 C1–C4；inproc 与 ipc 两后端结果一致性；独立内核进程（M3）验收；**+ 重连后破坏性方法不重试、长任务超时放大** | 40 |
| `test_kernel_guard.py` | **M4/M5 专项**：数据目录迁移幂等/非覆盖/可回滚、`direct_actions` 生效判定、`StartedAt`+健康复核（含 `starting` 过渡态）、直连睡眠清 `forced_at`、docker 失效回退看门狗 | 17 |
| `test_adversarial.py` | **对抗性边界**：非法/自相矛盾输入、状态竞争、非有限数值、归因正确性；**+ 巡检动作全链路、睡眠窗口闸门、改名后本体仍受保护** | 77 |
| `test_qq_login_recovery.py` | **账密登录恢复专项**（C17 §3 T-01..T-15）：配置归一化/别名迁移、Credential 缓存与 401 单次重换、`PasswordLogin` 四类解析（`code==0` 非成功）、有条件重启判据、失败不追加重启、静默期（含**失败/成功均强制落盘**）、就绪等待、闭环写回、凭据零泄漏、dry-run 零副作用、通知不误拉黑、IPC 契约、域边界 | 64 |
| `test_watchdog.sh`（`test_watchdog.py`） | 看门狗放行 / 白名单拒绝 / 过期拒绝 / 畸形忽略 / 失败记录 + **内核心跳守护**（心跳超时重启、reload 请求单一重启路径、冷却、非阻塞锁） | 75 |

运行：`bash tests/run_all.sh`（Python 自检需要 `aiohttp`）

> **容器名只有一个来源**：`containers.astrbot` / `containers.napcat`（填 Docker **容器名**，
> 不是 compose 服务名）。本体容器无条件受保护；策略列表可用 `@self` / `@napcat` 别名自动跟随。
> 详见 [docs/B12](docs/B12_安全权限与危险操作保护.md) §4.1 与
> [docs/B13](docs/B13_部署集成_compose_socket与看门狗自重启.md) §2.1。

> **真机验收（2026-10-16）**：在本机真实 docker（`napcat-local` + `astrbot-local` + 临时
> `guardian-kernel` 容器）上跑通 `C16` K2/K3/K4/K9–K13，过程中又定位并修复了 **7 项真机专属
> 缺陷 F14–F20**（离线单测测不出）。证据与复现见 [docs/C16](docs/C16_独立内核服务专项测试.md) §5，
> 缺陷档案见 [docs/D00](docs/D00_实机联调缺陷定位报告.md) §3.1 / §5.3。

---

## 独立内核（B16）

守护内核可以从 AstrBot 进程里搬出来独立运行，从而根治"重启 astrbot = 自杀"的
自杀悖论（S1）。分阶段推进：M0 解耦 → M1 拆 kernel/adapter → **M2 IPC** →
**M3 独立进程** → **M4 看门狗心跳守护 + 数据迁移** → **M5 动作直连化**。
**M0–M5 已全部落地**，本地自检 **312 项**全绿、真机 **K2/K3/K4/K9–K13 全部通过**，
验收见 [docs/C16](docs/C16_独立内核服务专项测试.md)（§3.5 真机项、§5 真机执行记录）。

已完成的 M2–M5 用法：

```bash
# 独立启动内核（无 AstrBot 依赖）
python -m astrbot_plugin_guardian.kernel --selftest     # 冒烟：起→自 ping→跑一轮→退出
python -m astrbot_plugin_guardian.kernel                # 常驻
python -m astrbot_plugin_guardian.kernel --print-config # 看解析后的配置与归一化告警

# 切换到独立内核：插件配置里把 kernel.mode 从 inproc 改成 ipc
# 并让两边 kernel.shared_dir 指向同一个共享卷
```

`kernel.mode=inproc` 是默认值，也是**回滚路径**：行为与拆分前完全一致。

### 三域职责与 M4/M5 的分工

| 域 | 角色 | M4/M5 之后 |
| --- | --- | --- |
| **A 内核** `guardian-kernel` | 想（决策）+ 看（观察）+ 记（状态） | **直连 docker.sock 执行动作**并自行复检 `StartedAt`+健康（`kernel.direct_actions`，默认 true；`false` 回退经看门狗） |
| **B 适配层**（AstrBot 插件） | 说（对话/工具/提示词面） | 无状态、无 docker 权限；断连走 D4-a+ 有界自修复阶梯 |
| **C 宿主看门狗** | 做（真正动容器）+ 兜底 | **心跳守护**：内核心跳超时或收到 reload 请求 → 单一路径重启内核容器；保留"最后兜底"请求路径 |

- **数据目录迁移**：内核首次启动若发现旧数据（AstrBot 插件数据目录）而新位置无状态，
  会**幂等复制**（只复制不删除，旧目录保留为只读副本可回滚），配置项 `kernel.legacy_data_dir`。
- **看门狗心跳守护**：`GUARDIAN_KERNEL_GUARD=1`（默认），心跳超时 `GUARDIAN_KERNEL_HEARTBEAT_TIMEOUT=90s`
  即 `docker compose restart guardian-kernel`；心跳文件不存在时**不擅自重启**（内核从未部署）。

生产部署见 [deploy/docker-compose.yml](deploy/docker-compose.yml) 的
`guardian-kernel` 服务、[deploy/kernel/](deploy/kernel/) 与
[deploy/guardian-watchdog.service](deploy/guardian-watchdog.service)。

---

## 许可证

本项目遵循 **GNU Affero General Public License v3.0（AGPL-3.0）**。

Copyright (C) 2026 Steven-Wang-120

AGPL-3.0 是强著佐权（copyleft）协议。特别注意 **第 13 条**：若你将本软件修改后
以网络服务的形式提供给用户使用，你必须向这些用户提供对应版本的完整源代码。
完整协议文本见仓库根目录的 [LICENSE](LICENSE)。
