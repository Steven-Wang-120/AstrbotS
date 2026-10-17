# 守护系统 Guardian · `astrbot_plugin_guardian`

AstrBot 双模式健康守护插件。面向 **Arch Linux + Docker Compose** 的
`napcat` / `astrbot` 双容器部署，提供容器自愈、QQ 掉线重登、内存泄漏防护、
生命周期自主睡眠与 ROS2 守护。

- 插件名：`astrbot_plugin_guardian`
- 版本：`0.6.0`
- 要求：AstrBot `>= 4.5.7`
- 依赖：`aiohttp`（AstrBot 通常已内置）
- 支持平台：`aiocqhttp` / `qq_official` / `telegram` / `lark` / `dingtalk` / `discord`

> 本 README 是插件的完整使用说明，不依赖仓库外的任何文档。
> **仓库根目录就是插件根目录**：`metadata.yaml`、`main.py`、`core/` 等都在根上，
> 因此整份仓库可以直接当成一个插件安装（仓库名 `AstrbotS` 与插件名无关，
> AstrBot 以解压后的**目录名**作为插件目录名）。
>
> 持久化数据全部写在 AstrBot 的 `data/plugin_data/astrbot_plugin_guardian/` 下，
> 插件目录本身不写数据，升级/重装不会丢状态。

---

## 1. 能力概览

| 能力 | 说明 |
| --- | --- |
| 容器健康守护 | 检测容器不存在 / 未运行 / OOMKilled / 健康检查失败 / 重启次数增长，自动启动或重启并复核 |
| QQ 掉线重登 | 默认每 20 分钟独立巡检登录态，三条证据链交叉验证，连续 N 轮失败才判定掉线；按策略重启 NapCat 并走账密登录阶梯 |
| 内存泄漏防护 | 采集容器内存，滑动窗口 + 最小二乘估算增长速率（MB/小时），超阈值告警，可选自动重启 |
| ROS2 守护 | 期望节点 / 话题存在性、话题频率、`ros2 doctor`；恢复命令仅来自配置白名单 |
| 生命周期自主睡眠 | 按容器运行时长分级（阈值前端可配），每级向 Bot 注入不同提示词引导其自主决定休息；30 天硬上限强制真睡眠 |
| LLM 工具 | 9 个工具供 Bot 自主调用（健康报告、巡检、诊断、事件、睡眠、重启、切模式 / 档位） |
| 通知 | 推送到指定会话，限流 + 去重 + 档位静默 + 失效 UMO 跳过 |
| 安全护栏 | 白名单 / 冷却 / 每小时熔断 / dry-run / 受保护容器 / 参数规范化 / 连续失败降级 |

### 自主触发铁律

本插件**绝不匹配对话关键词**。动作只来自三类触发源：

1. 调度器周期巡检（`guardian:patrol`、`guardian:qq`）
2. 生命周期等级推进（基于容器运行时长）
3. Bot 主动调用工具（`guardian_request_sleep` 等）

用户说「我要睡觉了」但 LLM 未调用工具时，插件不会做任何事——这是**设计如此**。

---

## 2. 安装

### 2.1 手动安装

**方式一：上传压缩包（推荐）**

在仓库根执行 `python build_plugin_zip.py`，产出 `astrbot_plugin_guardian-0.6.0.zip`，
然后在 AstrBot WebUI → 插件 → 安装插件 → 上传文件。压缩包根目录是
`astrbot_plugin_guardian/`，其下直接有 `metadata.yaml`，符合 AstrBot 的
`inspect_plugin_archive()` 校验。

**方式二：拷贝整个仓库**

```bash
git clone <repo> /tmp/AstrbotS
mkdir -p <AstrBot>/data/plugins/astrbot_plugin_guardian
cp -r /tmp/AstrbotS/. <AstrBot>/data/plugins/astrbot_plugin_guardian/
rm -rf <AstrBot>/data/plugins/astrbot_plugin_guardian/{.git,tests,deploy,docs,.venv}
```

**目录名必须是 `astrbot_plugin_guardian`**——AstrBot 用它作为插件目录名
（`root_dir_name`），并据此定位 `data/plugin_data/astrbot_plugin_guardian/`。
仓库叫什么名字不影响安装。

安装完成后在 WebUI → 插件 页面点击「重载插件」。插件依赖 `aiohttp`，
AstrBot 通常已内置；缺失时会按 `requirements.txt` 自动安装。

### 2.2 前置条件

- 宿主机已安装 Docker，且 AstrBot 容器内可访问 `/var/run/docker.sock`。
- 需要守护的容器使用固定 `container_name`（见 §3 配置）。
- QQ 掉线重登需要 NapCat 的 WebUI 端口可达，且已设置 WebUI token。

---

## 3. 配置

在 AstrBot WebUI → 插件 → 守护系统 中配置（对应 `_conf_schema.json`）。
配置会落在 AstrBot 的 `data/config/astrbot_plugin_guardian_config.json`。

**必填项**

| 键 | 说明 |
| --- | --- |
| `containers.astrbot` / `containers.napcat` | 本体 / 协议端**容器名**（Docker `container_name`，**不是** compose 服务名）。这是全部「目标名」的唯一来源 |
| `containers.managed` | 动作白名单，填上面两个容器名，例如 `["astrbot-local", "napcat-local"]`；漏填会在启动日志里被点名告警 |
| `qq.onebot_http_base` | 例如 `http://napcat:3000` |
| `notify.umo_list` | 告警推送目标会话 UMO（可留空，仅记日志） |

> `lifecycle.sleep_targets` / `uptime_targets` 默认使用符号别名 `@self` / `@napcat`，
> 会自动跟随 `containers.*`，**改名时不需要改这两项**。
> `safety.protected_containers` 只填**额外**要保护的容器——本体始终受保护，不用重复写。

### 3.1 账密登录恢复

QQ 掉线后如果会话已过期，`restart_container` 重启多少次都回不来。填下面四项即可让插件
在「重启 → 等待就绪 → 账密登录 → 复核写回」的阶梯里自动补登录。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `qq.account` | `""` | QQ 号（纯数字 5–12 位）。**留空即退化为 v0.5.0 行为**：只重启，不登录 |
| `qq.password` | `""` | 密码。**建议填 32 位小写 md5**；填明文会以明文形式保存在 AstrBot 配置 JSON 中 |
| `qq.webui_public_base` | `""` | 浏览器侧地址（如 `http://127.0.0.1:6099`），仅用于生成直达链接 |
| `qq.restart_grace_sec` | `3600` | 容器启动距今小于该值时跳过重启，避免维护重启后重复中断（被踢下线不受此限制） |

其余调优键：`qq.login_ready_timeout`（90s，等 QQ 到登录页）、`qq.login_retry_interval`
（3s，轮询间隔）、`qq.password_fail_hold_sec`（21600s，密码失败后的静默期，防试错锁号）。

`qq.relogin_method` 取值语义：

| 值 | 行为 |
| --- | --- |
| `restart_container`（默认） | 有条件重启 + 完整登录恢复阶梯 |
| `login_only` | 不动容器，只做「等就绪 → 账密登录 → 复核写回」 |
| `webui_quick_login` | **已更名**，加载时自动迁移为 `login_only` 并告警 |
| `disabled` | 只告警 |

> 登录失败分三类：密码错（`qq.login_failed`）、需验证码（`qq.login_captcha`）、需新设备验证
> （`qq.login_new_device`）。三类都**不会**追加重启，且失败后 6 小时内不再尝试账密。
> QQ 离线时插件**不发 QQ 通知**（发了必失败，还可能把管理员 UMO 拉黑），改走
> `/api/plug/astrbot_plugin_guardian/login` 登录状态页 + 落盘 `last_login_error.txt`。

### 3.2 首次部署建议

1. 保持 `dry_run: true`（默认）观察若干天，日志中会出现 `[dry-run] 将...`。
2. 确认巡检结论正确后，再关闭 `dry_run`。
3. 若要启用本体自重启，需同时：`safety.allow_self_restart: true` +
   `safety.self_restart_strategy: "watchdog"` + 看门狗已运行（见 §7）。
4. `lifecycle.levels` 决定分级阈值与注入提示词，可按需调整
   （默认 168h / 360h / 600h / 720h，720h = 30 天硬上限）。

### 3.3 生命周期等级默认表

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

```text
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
```

别名：`/守护`。

### 4.2 LLM 工具（Bot 自主调用）

`guardian_health_report` · `guardian_lifecycle_status` · `guardian_run_check` ·
`guardian_diagnose` · `guardian_list_incidents` · `guardian_request_sleep` ·
`guardian_restart_container` · `guardian_set_mode` · `guardian_set_profile`

破坏性工具默认要求管理员（`llm_tools.require_admin`），且容器参数必须在白名单内。

---

## 5. 数据与持久化

所有运行数据写在 AstrBot 数据目录下，**不写插件目录**：

```text
<AstrBot>/data/plugin_data/astrbot_plugin_guardian/
├── state.json             运行状态（模式、档位、冷却、熔断、采样窗口、等级状态）
├── audit.jsonl            追加式审计（含被抑制的动作）
├── restart_request.json   看门狗重启请求（需与宿主机共享卷）
└── last_login_error.txt   QQ 登录失败原因（离线可读）
```

登录状态页也可通过 `/api/plug/astrbot_plugin_guardian/login` 打开（需 dashboard 登录态）。

---

## 6. 安全说明

- 所有动作目标必须命中 `containers.managed` 白名单；本体容器默认受保护。
- 冷却（`safety.action_cooldown_sec`）与每小时熔断（`safety.max_actions_per_hour`）双限流。
- `dry_run` 打开时只记录不执行。
- 不提供任意 shell 执行能力；ROS2 恢复命令仅来自配置白名单。
- 密码建议填 md5，避免明文落盘；插件日志与审计**不记录**密码、credential 或 md5。

---

## 7. 自重启看门狗（可选）

容器内进程不能可靠地自杀重启，需要宿主机上的看门狗代劳：

```bash
sudo install -m 755 deploy/guardian-watchdog.sh /usr/local/bin/guardian-watchdog.sh
sudo install -m 644 deploy/guardian-watchdog.service /etc/systemd/system/
sudo install -m 644 deploy/guardian-watchdog.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now guardian-watchdog.timer
systemctl status guardian-watchdog.timer
```

看门狗每 30 秒检查一次 `restart_request.json`，校验**目标白名单 + 请求新鲜度（120s）**
后执行 `docker compose restart`。

---

## 8. 目录结构

仓库根即插件根。带 `*` 的是**仓库自身**的东西，不随插件包分发
（打包脚本与 `.dockerignore` 都排除）。

```text
astrbot_plugin_guardian/        ← 仓库根 = 插件根
├── metadata.yaml          *插件元数据（必须在根，AstrBot 据此识别）
├── main.py                *AstrBot 适配层：命令 / LLM 工具 / 钩子 / 生命周期
├── kernel_runtime.py      *内核装配（零 astrbot 依赖）
├── adapter_runtime.py      AstrBot 侧运行时助手（数据目录解析等）
├── runtime.py              兼容壳，转发到 adapter_runtime
├── __init__.py            *包入口
├── _conf_schema.json      *WebUI 配置 schema
├── requirements.txt       *依赖
├── core/                  models config state audit bus policy safety lifecycle scheduler engine registry log
├── modes/                 base server ros2 factory
├── probes/                base docker_probes container_memory qq_login uptime ros2_nodes ros2_topics ros2_doctor
├── actions/               base container_actions qq_actions ros2_actions profile_actions sleep
├── ipc/                   protocol server client backend methods
├── kernel/                __main__ service（`python -m astrbot_plugin_guardian.kernel`）
├── notify/                notifier
├── tools/                 llm_tools
├── utils/                 dockerclient napcat shell ros2_runner textutil migration watchdog
├── logo.png            * 插件图标（市场展示用）
├── LICENSE             * 许可证
├── README.md           * 本文件
│
├── tests/              * 自检套件（不打包）：test_*.py + run_all.sh + _pluginloader.py
├── deploy/             * 部署资产（不打包）：docker-compose.yml、kernel/Dockerfile、看门狗
├── docs/               * 开发文档（不打包，不入库）
├── pyproject.toml      * ruff 配置
├── build_plugin_zip.py * 打包脚本
├── .dockerignore       * 镜像构建忽略
└── .gitignore          *
```

> `main.py` 顶部的注释说明为什么不再使用 `@register` 装饰器：AstrBot 自 v3.5.19 起
> 自动识别继承 `Star` 的类作为插件类，旧装饰器已标记 DEPRECATED。

---

## 9. 常见问题

**Q：配置改了不生效？**
在 WebUI 插件页点「重载插件」，或重启 AstrBot 容器。

**Q：QQ 一直登录失败？**
看 `data/plugin_data/astrbot_plugin_guardian/last_login_error.txt`，或打开登录状态页。
`needCaptcha` 需要人工在手机上处理一次验证；`new_device` 需要先在手机 QQ 的
「登录设备管理」里确认设备。

**Q：改了容器名之后巡检报「目标不在白名单」？**
同步修改 `containers.astrbot` / `containers.napcat` 和 `containers.managed`。

**Q：`dry_run` 打开时 30 天保证还会执行吗？**
不会，会降级为「强制告警」，不会真正重启。

---

## 10. 开发与测试

插件采用三域分离：内核（`core/` `utils/` `actions/` `probes/` `modes/` `ipc/` `kernel/`）
不 import `astrbot`，适配层（`main.py` `adapter_runtime.py`）才依赖 AstrBot API，
便于脱离 AstrBot 做纯逻辑自检。

跑全部自检（`run_all.sh` 会逐个执行 `tests/test_*.py`）：

```bash
PYTHON=./.venv/Scripts/python.exe bash tests/run_all.sh   # Windows
PYTHON=python3 bash tests/run_all.sh                       # Linux/macOS
```

单独跑一个：

```bash
./.venv/Scripts/python.exe tests/test_kernel_ipc.py
```

> **关于导入路径**：仓库根就是包内部，无法直接 `import astrbot_plugin_guardian`。
> `tests/_pluginloader.py` 提供两个垫片：`install()` 把仓库根注册成同名包（进程内），
> `subprocess_env()` 建一个临时目录并放 junction/symlink，供
> `python -m astrbot_plugin_guardian.kernel` 型子进程使用。

提交前请用 ruff 检查（配置见仓库根 `pyproject.toml`）：

```bash
ruff check .
```

> `ruff format .` 会重排大量既有文件，只在你想统一格式时运行。

---

## 11. 已知限制

- 不做 WebUI 可视化面板（命令 + 通知 + 状态文件承载）。
- 不提供任意 shell 执行能力（安全红线）。
- NapCat 若需人工扫码，重登会失败并升级为 FATAL 通知。
- `dry_run=true` 时 30 天保证降级为「强制告警」，不会真正重启。

---

## 12. 许可

AGPL-3.0，见仓库根目录 `LICENSE`。
