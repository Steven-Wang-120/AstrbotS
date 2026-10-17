# 守护系统 Guardian · `astrbot_plugin_guardian`

AstrBot 双模式健康守护插件。面向 **Arch Linux + Docker Compose** 的
`napcat` / `astrbot` 双容器部署。

> 设计规格见 `../../docs/B00_项目总览与需求规格.md`（唯一权威），
> 实现说明见 `../../docs/B01`–`B15`，测试用例见 `../../docs/C00`–`C15`。

---

## 1. 能力概览

| 能力 | 说明 |
| --- | --- |
| 容器健康守护 | 检测容器不存在/未运行/OOMKilled/健康检查失败/重启次数增长，自动启动或重启并复核 |
| QQ 掉线重登 | **每 20 分钟**（可配）独立巡检 QQ 登录态，三条证据链交叉验证，连续 N 轮失败才判定掉线，按策略重启 NapCat 或调用 WebUI 快速登录 |
| 内存泄漏防护 | 采集容器内存，滑动窗口 + 最小二乘估算增长速率（MB/小时），超阈值告警，可选自动重启 |
| ROS2 守护 | 期望节点/话题存在性、话题频率、`ros2 doctor`；恢复命令仅来自配置白名单 |
| 生命周期自主睡眠 | 按容器运行时长分级（阈值**前端可配**），每级向 Bot 注入不同提示词引导其自主决定休息；30 天硬上限强制真睡眠 |
| LLM 工具 | 9 个工具供 Bot 自主调用（健康报告、巡检、诊断、事件、睡眠、重启、切模式/档位） |
| 通知 | 推送至指定会话，限流 + 去重 + 档位静默 + 失效 UMO 跳过 |
| 安全护栏 | 白名单 / 冷却 / 每小时熔断 / dry-run / 受保护容器 / 参数规范化 / 连续失败降级 |

### 自主触发铁律

本插件**绝不匹配对话关键词**。动作只来自三类触发源：

1. 调度器周期巡检（`guardian:patrol`、`guardian:qq`）
2. 生命周期等级推进（基于容器运行时长）
3. Bot 主动调用工具（`guardian_request_sleep` 等）

用户说「我要睡觉了」但 LLM 未调用工具时，插件不会做任何事——这是**设计如此**。

---

## 2. 安装

### 2.1 部署 AstrBot + NapCat（宿主机）

```bash
# Arch Linux 依赖
sudo pacman -S --needed docker docker-compose jq
sudo systemctl enable --now docker

# 获取 docker 组 GID（用于 group_add）
stat -c '%g' /var/run/docker.sock

cd /opt/astrbot-stack
cp <repo>/deploy/.env.example .env   # 填入 DOCKER_GID
cp <repo>/deploy/docker-compose.yml .
mkdir -p data napcat/config
docker compose up -d
```

### 2.2 安装插件

把 `astrbot_plugin_guardian/` 整个目录放到 AstrBot 的插件目录：

```bash
cp -r src/astrbot_plugin_guardian /opt/astrbot-stack/data/plugins/
```

然后在 AstrBot WebUI → 插件 页面重载该插件（或重启容器）。
插件依赖 `aiohttp`，AstrBot 通常已内置；若缺失会在插件安装时按 `requirements.txt` 安装。

### 2.3 部署自重启看门狗（强烈建议）

```bash
sudo install -m 755 deploy/guardian-watchdog.sh /usr/local/bin/guardian-watchdog.sh
sudo install -m 644 deploy/guardian-watchdog.service /etc/systemd/system/
sudo install -m 644 deploy/guardian-watchdog.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now guardian-watchdog.timer
systemctl status guardian-watchdog.timer
```

看门狗每 30 秒检查一次 `data/guardian/restart_request.json`，校验
**目标白名单 + 请求新鲜度（120s）** 后执行 `docker compose restart`。
容器内进程不会自杀，重启由宿主机代劳。

---

## 3. 配置

在 AstrBot WebUI → 插件 → 守护系统 中配置（对应 `_conf_schema.json`）。

**必填项**

| 键 | 说明 |
| --- | --- |
| `containers.astrbot` / `containers.napcat` | 本体 / 协议端**容器名**（Docker `container_name`，**不是** compose 服务名）。这是全部"目标名"的唯一来源 |
| `containers.managed` | 动作白名单，填上面两个容器名，例如 `["astrbot-local", "napcat-local"]`；漏填会在启动日志里被点名告警 |
| `qq.onebot_http_base` | 例如 `http://napcat:3000` |
| `notify.umo_list` | 告警推送目标会话 UMO（可留空，仅记日志） |

> `lifecycle.sleep_targets` / `uptime_targets` 默认用符号别名 `@self` / `@napcat`，会自动跟随
> `containers.*`，**改名时不需要改这两项**。`safety.protected_containers` 只填**额外**要保护的
> 容器——本体始终受保护，不用重复写。

**账密登录恢复（B17，v0.6.0 起）**

QQ 掉线后如果会话已过期，`restart_container` 重启多少次都回不来。填下面四项即可让插件
在"重启 → 等待就绪 → 账密登录 → 复核写回"的阶梯里自动补登录（见 `docs/B17`）。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `qq.account` | `""` | QQ 号（纯数字 5–12 位）。**留空即退化为 v0.5.0 行为**：只重启，不登录 |
| `qq.password` | `""` | 密码。**建议填 32 位小写 md5**；填明文会以明文形式保存在 AstrBot 配置 JSON 中 |
| `qq.webui_public_base` | `""` | 浏览器侧地址（如 `http://127.0.0.1:6099`），只用于生成直达链接 |
| `qq.restart_grace_sec` | `3600` | 容器启动距今小于该值时跳过重启，避免维护重启后重复中断 |

其余调优键：`qq.login_ready_timeout`（90s，等 QQ 到登录页）、`qq.login_retry_interval`
（3s，轮询间隔）、`qq.password_fail_hold_sec`（21600s，密码失败后的静默期，防试错锁号）。

`qq.relogin_method` 取值语义（v0.6.0 变更）：

| 值 | 行为 |
| --- | --- |
| `restart_container`（默认） | 有条件重启 + 完整登录恢复阶梯 |
| `login_only` | 不动容器，只做"等就绪 → 账密登录 → 复核写回" |
| `webui_quick_login` | **已更名**，加载时自动迁移为 `login_only` 并告警（旧实现打的是 404 路由） |
| `disabled` | 只告警 |

> 登录失败分三类：密码错（`qq.login_failed`）、需验证码（`qq.login_captcha`）、需新设备验证
> （`qq.login_new_device`）。三类都**不会**再追加重启，且失败后 6 小时内不再尝试账密。
> QQ 离线时插件**不发 QQ 通知**（发了必失败，还会把管理员 UMO 拉黑），改走
> `/api/plug/astrbot_plugin_guardian/login` 登录状态页 + 落盘 `guardian/last_login_error.txt`。

**首次部署建议**

1. 保持 `dry_run: true`（默认）观察若干天，日志中会出现 `[dry-run] 将...`。
2. 确认巡检结论正确后，再关闭 `dry_run`。
3. 若要启用本体自重启，需同时：`safety.allow_self_restart: true` +
   `safety.self_restart_strategy: "watchdog"` + 看门狗已运行。
4. `lifecycle.levels` 决定分级阈值与注入提示词，可按需调整
   （默认：168h / 360h / 600h / 720h，720h = 30 天硬上限）。

**生命周期等级默认表**

| 等级 | 运行时长 | 动作 |
| --- | --- | --- |
| L0 常规 | 0–168h | none |
| L1 轻提醒 | ≥168h | notify |
| L2 建议休息 | ≥360h | notify |
| L3 准备睡眠 | ≥600h | arm_sleep（武装，等 Bot 自主发起） |
| L4 强制睡眠 | ≥720h | force_sleep（插件强制） |

提示词支持模板变量：`{uptime_hours}` `{uptime_days}` `{level_name}`
`{next_level_hours}` `{remaining_hours}` `{last_sleep_at}`。

---

## 4. 使用

### 4.1 命令（人）

```
/guardian status                 # 健康快照（含运行时长与等级）
/guardian check [scope]          # 立即巡检
/guardian level                  # 生命周期详情
/guardian sleep [分钟] [原因]    # 立即真睡眠（管理员）
/guardian login                  # 立即执行一次登录恢复（管理员）
/guardian webui                  # NapCat WebUI 直达链接（管理员，含 token）
/guardian mode <server|ros2>     # 切换模式（管理员）
/guardian profile <p>            # 切换档位（管理员）
/guardian restart <容器>         # 重启容器（管理员）
/guardian incidents [n]          # 最近事件
/guardian diagnose <目标|all>    # 诊断
/guardian jobs                   # 下次任务计划
/guardian safety                 # 安全自检结果
/guardian help
别名：/守护
```

### 4.2 LLM 工具（Bot 自主调用）

`guardian_health_report` · `guardian_lifecycle_status` · `guardian_run_check` ·
`guardian_diagnose` · `guardian_list_incidents` · `guardian_request_sleep` ·
`guardian_restart_container` · `guardian_set_mode` · `guardian_set_profile`

破坏性工具默认要求管理员（`llm_tools.require_admin`），且容器参数必须在白名单内。

---

## 5. 目录结构

```
astrbot_plugin_guardian/
├── main.py              AstrBot 适配层：命令 / LLM 工具 / 钩子 / 生命周期
├── runtime.py           运行时装配：配置→状态→客户端→引擎→调度器
├── metadata.yaml        插件元数据
├── _conf_schema.json    WebUI 配置 schema
├── requirements.txt
├── core/                models config state audit bus policy safety lifecycle scheduler engine registry
├── modes/               base server ros2 factory
├── probes/              base docker_probes container_memory qq_login uptime ros2_nodes ros2_topics ros2_doctor
├── actions/             base container_actions qq_actions ros2_actions profile_actions sleep
├── notify/              notifier
├── tools/               llm_tools
└── utils/               dockerclient napcat shell ros2_runner textutil
```

**数据落盘**：`<AstrBot data>/guardian/`
- `state.json` — 运行状态（模式、档位、冷却、熔断、采样窗口、等级状态）
- `audit.jsonl` — 追加式审计（含被抑制的动作）
- `restart_request.json` — 看门狗请求（共享卷）

---

## 6. 开发与测试

```bash
# 纯逻辑自检（无需 AstrBot、无需 pytest）
python tests/test_core_logic.py

# 端到端自检（含探针解析，需要 aiohttp）
python tests/test_probes_e2e.py
```

修改代码后在 WebUI 插件页点「重载插件」即可热更新；`terminate()` 会严格取消
所有定时任务，避免重载后重复巡检。

---

## 7. 已知限制

- 不做 WebUI 可视化面板（命令 + 通知 + 状态文件承载）。
- 不提供任意 shell 执行能力（安全红线）。
- NapCat 若需人工扫码，重登会失败并升级为 FATAL 通知。
- `dry_run=true` 时 30 天保证降级为「强制告警」，不会真正重启。
