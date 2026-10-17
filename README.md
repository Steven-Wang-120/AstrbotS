# AstrbotS · 守护系统 Guardian

面向 **Arch Linux + Docker Compose（napcat / astrbot 双容器）** 的 AstrBot 守护插件工程。

插件本体是 `src/astrbot_plugin_guardian/`，可直接放进 AstrBot 的 `data/plugins/`；
`deploy/` 提供容器编排与宿主机看门狗。**插件使用说明见
[`src/astrbot_plugin_guardian/README.md`](src/astrbot_plugin_guardian/README.md)**。

- 插件名：`astrbot_plugin_guardian` · 版本 `0.6.0` · 要求 AstrBot `>= 4.5.7`
- 依赖：`aiohttp`（AstrBot 通常已内置）
- 许可：AGPL-3.0

---

## 目录结构

```text
AstrbotS/
├── src/astrbot_plugin_guardian/   插件本体（直接放进 AstrBot 的 data/plugins/）
├── deploy/                        docker-compose.yml + 看门狗（systemd 单元/定时器/脚本）+ 内核镜像
├── README.md                      本文件：仓库总览
└── LICENSE
```

> `docs/`（B/C/D 系列开发与验收文档）与 `tests/`（自检脚本）属于**本地开发资料，
> 未纳入版本库**，因此本 README 不提供指向它们的链接。

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
#   然后在 AstrBot WebUI → 插件 页点「重载插件」

# 3) 部署自重启看门狗（推荐）
sudo install -m 755 deploy/guardian-watchdog.sh /usr/local/bin/guardian-watchdog.sh
sudo install -m 644 deploy/guardian-watchdog.service /etc/systemd/system/
sudo install -m 644 deploy/guardian-watchdog.timer   /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now guardian-watchdog.timer
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

## 独立内核（M0–M5 已完成）

守护内核可以从 AstrBot 进程里搬出来独立运行，从而根治「重启 astrbot = 自杀」的
自杀悖论。分阶段推进：M0 解耦 → M1 拆 kernel/adapter → M2 IPC →
M3 独立进程 → M4 看门狗心跳守护 + 数据迁移 → M5 动作直连化。

```bash
# 独立启动内核（无 AstrBot 依赖）
python -m astrbot_plugin_guardian.kernel --selftest     # 冒烟：起→自 ping→跑一轮→退出
python -m astrbot_plugin_guardian.kernel                # 常驻
python -m astrbot_plugin_guardian.kernel --print-config # 看解析后的配置与归一化告警

# 切换到独立内核：插件配置里把 kernel.mode 从 inproc 改成 ipc
# 并让两边 kernel.shared_dir 指向同一个共享卷
```

`kernel.mode=inproc` 是默认值，也是**回滚路径**：行为与拆分前完全一致。

### 三域职责

| 域 | 角色 | M4/M5 之后 |
| --- | --- | --- |
| **A 内核** `guardian-kernel` | 想（决策）+ 看（观察）+ 记（状态） | **直连 docker.sock 执行动作**并自行复检 `StartedAt`+健康（`kernel.direct_actions`，默认 true；`false` 回退经看门狗） |
| **B 适配层**（AstrBot 插件） | 说（对话/工具/提示词面） | 无状态、无 docker 权限；断连走 D4-a+ 有界自修复阶梯 |
| **C 宿主看门狗** | 做（真正动容器）+ 兜底 | **心跳守护**：内核心跳超时或收到 reload 请求 → 单一路径重启内核容器；保留「最后兜底」请求路径 |

- **数据目录迁移**：内核首次启动若发现旧数据（AstrBot 插件数据目录）而新位置无状态，
  会**幂等复制**（只复制不删除，旧目录保留为只读副本可回滚），配置项 `kernel.legacy_data_dir`。
- **看门狗心跳守护**：`GUARDIAN_KERNEL_GUARD=1`（默认），心跳超时
  `GUARDIAN_KERNEL_HEARTBEAT_TIMEOUT=90s` 即 `docker compose restart guardian-kernel`；
  心跳文件不存在时**不擅自重启**（内核从未部署）。

生产部署见 [deploy/docker-compose.yml](deploy/docker-compose.yml) 的
`guardian-kernel` 服务、[deploy/kernel/](deploy/kernel/) 与
[deploy/guardian-watchdog.service](deploy/guardian-watchdog.service)。

---

## 本地自检

`tests/` 下有 8 个 Python 自检脚本 + 1 个看门狗脚本，覆盖核心逻辑、探针、插件装载、
内核解耦、IPC 契约、内核守护、对抗性边界、账密登录恢复与看门狗：

| 脚本 | 覆盖 |
| --- | --- |
| `test_core_logic.py` | cron 解析、生命周期等级、配置校验、安全护栏、判定引擎、调度器、内存斜率、容器名一致性 |
| `test_probes_e2e.py` | 9 个探针 + 4 个动作 + 模式装配 + 通知器 + 活跃度弱正据 |
| `test_plugin_load.py` | 插件装载、命令/工具/钩子注册、提示词注入、硬上限睡眠、零关键词反证 |
| `test_kernel_decoupling.py` | 内核侧模块不得 `import astrbot`（静态守卫 + 动态阻断） |
| `test_kernel_ipc.py` | 内核 IPC 契约：请求/响应/错误/超时/断连、自修复有界性、双后端一致性 |
| `test_kernel_guard.py` | 数据目录迁移幂等、`direct_actions` 生效判定、`StartedAt`+健康复核 |
| `test_adversarial.py` | 对抗性边界：非法输入、状态竞争、非有限数值、归因正确性 |
| `test_qq_login_recovery.py` | 账密登录恢复专项：配置归一化、Credential 缓存与重换、失败分级、静默期、闭环写回、凭据零泄漏 |
| `test_watchdog.sh` / `test_watchdog.py` | 看门狗放行/白名单拒绝/过期拒绝 + 内核心跳守护 |

运行：`bash tests/run_all.sh`（需要 `aiohttp`；用 `PYTHON=<解释器>` 指定 Python）。

> **容器名只有一个来源**：`containers.astrbot` / `containers.napcat`（填 Docker **容器名**，
> 不是 compose 服务名）。本体容器无条件受保护；策略列表可用 `@self` / `@napcat`
> 别名自动跟随。

---

## 许可证

本项目遵循 **GNU Affero General Public License v3.0（AGPL-3.0）**。

Copyright (C) 2026 Steven-Wang-120

AGPL-3.0 是强著佐权（copyleft）协议。特别注意 **第 13 条**：若你将本软件修改后
以网络服务的形式提供给用户使用，你必须向这些用户提供对应版本的完整源代码。
完整协议文本见仓库根目录的 [LICENSE](LICENSE)。
