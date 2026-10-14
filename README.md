# AstrbotS · 守护系统 Guardian

面向 **Arch Linux + Docker Compose（napcat / astrbot 双容器）** 的 AstrBot 守护插件工程。
本目录同时包含**开发文档（B 系列）**、**测试文档（C 系列）**、**插件源码**与**部署编排**。

---

## 目录导航

```
AstrbotS/
├── docs/          B00–B15 开发/施工文档；C00–C15 测试文档
├── src/astrbot_plugin_guardian/   插件源码（直接放进 AstrBot 的 data/plugins/）
├── deploy/        docker-compose.yml + 看门狗（systemd 单元/定时器/脚本）
└── tests/         自检测试（无需 AstrBot 即可运行）
```

### 建议阅读顺序

1. `docs/B00_项目总览与需求规格.md` —— **唯一权威规格**，先读它
2. `docs/B15_生命周期等级与自主睡眠机制.md` —— 核心机制（等级 + 自主睡眠 + 30 天保证）
3. `docs/B01` → `B14` —— 按模块的施工细节
4. `docs/C00` → `C15` —— 测试用例与验收判据（C13 为极端/故障注入）
5. `src/astrbot_plugin_guardian/README.md` —— 安装与配置速查

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

`tests/` 下共 4 个自检脚本，覆盖核心逻辑、探针、插件装载与看门狗：

| 脚本 | 覆盖 |
| --- | --- |
| `test_core_logic.py` | cron 解析、生命周期等级、配置校验、安全护栏、判定引擎、调度器、内存斜率 |
| `test_probes_e2e.py` | 9 个探针 + 4 个动作 + 模式装配 + 通知器（使用 fixtures 假数据） |
| `test_plugin_load.py` | 插件装载、命令/工具/钩子注册、提示词注入、硬上限睡眠、零关键词反证 |
| `test_watchdog.sh` | 看门狗放行 / 白名单拒绝 / 过期拒绝 / 畸形忽略 / 失败记录 |

运行：`bash tests/run_all.sh`（Python 自检需要 `aiohttp`）

---

## 许可证

本项目遵循 **GNU Affero General Public License v3.0（AGPL-3.0）**。

Copyright (C) 2026 Steven-Wang-120

AGPL-3.0 是强著佐权（copyleft）协议。特别注意 **第 13 条**：若你将本软件修改后
以网络服务的形式提供给用户使用，你必须向这些用户提供对应版本的完整源代码。
完整协议文本见仓库根目录的 [LICENSE](LICENSE)。
