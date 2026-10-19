# 守护系统 Guardian · `astrbot_plugin_guardian`

AstrBot 双模式健康守护插件。面向 **Arch Linux + Docker Compose** 的
`napcat` / `astrbot` 双容器部署，提供容器自愈、QQ 掉线重登、内存泄漏防护、
生命周期自主睡眠与 ROS2 守护。

- 插件名：`astrbot_plugin_guardian`
- 版本：`0.8.0`
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
| 作息窗口 | 困意由运行时长决定，「现在能不能睡」由时段决定；窗口外的自主睡眠会推迟，顺延超上限则无视窗口 |
| 主动播报 | Bot 到点自己去跟管理员说要睡了 / 我回来了；说什么由 LLM 决定，插件只给判定与事实 |
| LLM 工具 | 10 个工具供 Bot 自主调用（健康报告、巡检、诊断、事件、困意、睡眠、重启、切模式 / 档位） |
| 通知 | 推送到指定会话，限流 + 去重 + 档位静默 + 失效 UMO 跳过 |
| 安全护栏 | 白名单 / 冷却 / 每小时熔断 / 受保护容器 / 参数规范化 / 连续失败降级 |
| 运行环境自述 | `env_platform` 探针从宿主机侧读取系统 / 内核 / 架构 / 内存 / 时区，无需手填 |
| 看门狗自带 | 插件携带安装脚本，加载时自动装到 Linux 宿主机；做不到时如实返回手动命令，绝不假装成功 |

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

在仓库根执行 `python build_plugin_zip.py`，产出 `astrbot_plugin_guardian-0.8.0.zip`，
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

**不需要填的东西**：运行环境（系统 / 内核 / 架构 / 内存 / 时区）由 `env_platform`
探针从宿主机侧采集，命令 `/guardian env` 或启动日志里可直接看到，没有需要手填的
「目标系统」输入框。

### 3.1 首次部署建议

1. 先把 `containers.*` / `containers.managed` 填对，`/guardian safety` 里不应再有
   「白名单为空」告警。
2. 先用 `profile=maintenance` 跑一次 `/guardian check`：maintenance 档位暂停自动
   动作，只出巡检结论，可在不产生任何副作用的前提下核对判据。
3. 确认巡检结论正确后把档位调回 `normal`。若还想再稳一轮，可把
   `containers.managed` 先留空（所有自动重启都会被拒绝），观察几天日志里
   被拒绝的理由，再放开。
4. 本体自重启另需 `safety.allow_self_restart: true` +
   `safety.self_restart_strategy: "watchdog"` + 看门狗已运行（见 §7）。
5. `lifecycle.levels` 决定分级阈值与注入提示词，可按需调整
   （默认 168h / 360h / 600h / 720h，720h = 30 天硬上限）。
6. `circadian` 决定本体什么时候**允许**睡（默认 02:00–07:00）。它只做闸门，
   不做触发器——困意仍由运行时长决定，窗口只决定"现在能不能睡"。

### 3.1.1 作息窗口

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `circadian.enabled` | `true` | 关闭后困意到了就立即睡，不再等待时段 |
| `circadian.asleep_start` | `"02:00"` | 允许入睡时刻（本地时间），支持跨零点 |
| `circadian.asleep_end` | `"07:00"` | 应当醒来时刻。与 `asleep_start` 相同 = **关闭该特性** |
| `circadian.max_defer_hours` | `12` | 顺延上限。超过即无视窗口强制入睡，保证 30 天内必真睡一次 |
| `circadian.quiet_minutes` | `10` | 最近这么多分钟内有对话就不打断；顺延超限后不再理会 |

两条容易踩的坑：

- **别把 `start` 和 `end` 填成一样再期望"全天"。** 相同值表示零长度窗口，直接关闭
  该特性；想要不设限请用 `circadian.enabled: false`。
- **时区来自 `env_platform` 探针，不是容器里的 `TZ`。** 探针尚未跑过时按东八区
  （`+8`）计算；若部署在其他时区，首轮巡检后会自动纠正，日志里的
  「现在：…」可直接核对。

窗口外的自主睡眠会被推迟并记审计 `sleep.deferred`；管理员命令与定时任务
不受窗口约束。

### 3.2 账密登录恢复

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
/guardian drowsy                 # 此刻困到什么程度（事实层，不影响状态）
/guardian announce [事件]        # 预览「现在会主动说什么」，不发送（见 §4.3）
/guardian env                    # 运行环境指纹（系统/内核/架构/内存/时区）
/guardian sleep [分钟] [原因]    # 立即真睡眠（管理员）
/guardian login                  # 立即执行一次登录恢复（管理员）
/guardian webui                  # NapCat WebUI 直达链接（管理员，含 token）
/guardian mode <server|ros2>     # 切换模式（管理员）
/guardian profile <p>            # 切换档位（管理员）
/guardian restart <容器>         # 重启容器（管理员）
/guardian reset                  # 清零内核自修复额度（管理员，仅 ipc 模式）
/guardian incidents [n]          # 最近事件
/guardian diagnose <目标|all>    # 诊断
/guardian jobs                   # 下次任务计划
/guardian safety                 # 安全自检结果
/guardian help
```

别名：`/守护`。

> `/guardian drowsy` 与 `/guardian announce` 的区别：前者回答「我现在困到什么程度」，
> 任何等级都有答案；后者回答「我现在会不会主动开口」，低等级下本就没有该说的话。
> 预览命令用的是「假设等级已到」的事实，所以不必真等 25 天 uptime 才能核对模板。

### 4.2 LLM 工具（Bot 自主调用）

`guardian_health_report` · `guardian_lifecycle_status` · `guardian_drowsy` ·
`guardian_run_check` · `guardian_diagnose` · `guardian_list_incidents` ·
`guardian_request_sleep` · `guardian_restart_container` · `guardian_set_mode` ·
`guardian_set_profile`

共 10 个。破坏性工具默认要求管理员（`llm_tools.require_admin`），且容器参数必须在白名单内。

> `guardian_drowsy` 只交出**事实**（在线多久、几级、离窗口多远），不调用模型——
> 用户本来就在跟模型对话，由正在回话的那一轮带人格组织语言。
> 主动播报（Bot 自己开口）走另一条路，见 §4.3。

### 4.3 主动播报（Bot 到点自己说要睡了）

到点困了，Bot 会主动发一条消息告诉管理员自己要睡了，免得管理员以为它挂了。
**说什么由 LLM 决定，插件里没有成品句子**——插件只提供「该不该开口」的判定
和一组结构化事实（在线多久、几级、离窗口多远、这次睡多久、上次为什么没睡成）。

| 配置键 | 默认 | 说明 |
| --- | --- | --- |
| `lifecycle.sleepy_announce` | `on_arm` | `off` 关闭 / `on_arm` 只在真睡得成时说（推荐）/ `always` 想睡就说（睡不成也说一声） |
| `lifecycle.sleepy_announce_max_unanswered` | `3` | 连续这么多次没人回应就暂停主动消息（`0` = 不限制，任一条用户消息立即清零） |
| `lifecycle.post_sleep_announce` | `true` | 睡醒后说一句「我回来了」（靠事件键去重，超过 6 小时的那次不补报） |
| `lifecycle.sleepy_prompt` | 空 | 睡前提示词模板，留空用内置 |
| `lifecycle.sleepy_deferred_prompt` | 空 | 「想睡但没睡成」模板，仅在 `always` 下使用 |
| `lifecycle.resume_prompt` | 空 | 睡醒报备模板，留空 = 跟着插件升级拿到新文案 |

> 只在 **L3 准备睡眠 / L4 强制睡眠** 时才可能开口。L1、L2 靠对话里的提示词渗透，
> 不走主动消息。

三类事件：睡前报备（`sleep`）、睡醒报备（`resume`）、「困了但还没睡成」（`deferred`）。

模板支持 14 个占位符：`{{current_time}}` `{{uptime}}` `{{level_id}}` `{{level_name}}`
`{{remaining}}` `{{duration}}` `{{targets}}` `{{period}}` `{{to_window}}`
`{{defer_reason}}` `{{wait_text}}` `{{slept}}` `{{forced}}` `{{reason}}`。

防重复用**事件键**（如 `sleep:L3:601`）而不是时间窗口——Bot 说完就去睡，
睡下后不可能再发第二条，频率限制在这里没有约束对象。

收件人取自 `state.json` 的 `announce.admin_umo` / `announce.last_umo`
（运行时状态，不是配置项），由消息观察填充，**不读消息内容**。
`notify.umo_list` 为空时仍能送达。

模板里的花括号**写错会原样留在消息里**——这是刻意不做静默兜底，
否则关键事实被吞掉也没人发现。

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
- 连续失败达阈值自动降级为只告警，不会无限重试。
- 不提供任意 shell 执行能力；ROS2 恢复命令仅来自配置白名单。
- 密码建议填 md5，避免明文落盘；插件日志与审计**不记录**密码、credential 或 md5。

---

## 7. 自重启看门狗（可选）

容器内进程不能可靠地自杀重启，需要宿主机上的看门狗代劳。

### 7.1 自动安装（Linux 宿主机，默认开启）

插件加载时会**自动**尝试把看门狗装到宿主机上，不需要你手动敲任何命令。
机制：插件通过已挂载的 `/var/run/docker.sock` 起一个**一次性临时容器**，
把宿主根挂到 `/host`、`PidMode: host`（`systemctl` 要看得见宿主的 systemd），
在里面执行 `deploy/install-watchdog.sh`，装完立即删除该容器。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `safety.auto_install_watchdog` | `true` | 关掉就完全不动宿主机 |
| `safety.watchdog_compose_dir` | 空 | 留空=从容器标签 `com.docker.compose.project.working_dir` 自动读 |

行为约定（**宁可明说做不到，也不假装成功**）：

- 已装且账本新鲜 → 跳过，不重复注册；
- 宿主没有可用镜像（`alpine` / `busybox` / `debian-slim`）→ 失败并给出手动命令，**绝不自动拉镜像**；
- Docker socket 没挂进容器 → 失败并说明原因；
- **Windows / macOS 宿主机 → 直接返回手动提示**。Linux 容器无法执行
  `powershell.exe`，这是平台硬限制，不是没实现。Windows 请执行一次
  `deploy/install-watchdog.ps1`。

> 安全前提：这一步把宿主根以读写方式临时暴露给一个容器。它不新增权限——
> 插件本来就能通过 socket 控制所有容器——但它确实是特权操作。
> 不认可这一点就把 `auto_install_watchdog` 设为 `false`，改为手动安装。

### 7.2 手动安装（Linux）

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
├── __init__.py            *包入口（`__version__` 与 metadata 对齐）
├── _conf_schema.json      *WebUI 配置 schema
├── requirements.txt       *依赖
├── core/                  models config state audit bus policy safety lifecycle
│                          scheduler engine registry log
│                          announce timefmt timewindow watchdog_install
├── modes/                 base server ros2 factory
├── probes/                base docker_probes container_memory qq_login uptime
│                          env_platform ros2_nodes ros2_topics ros2_doctor
├── actions/               base container_actions qq_actions ros2_actions profile_actions sleep
├── ipc/                   protocol server client backend methods
├── kernel/                __main__ service（`python -m astrbot_plugin_guardian.kernel`）
├── notify/                notifier
├── tools/                 llm_tools
├── utils/                 dockerclient napcat shell ros2_runner textutil migration
│                          osinfo watchdog
├── logo.png            * 插件图标（市场展示用）
├── LICENSE             * 许可证
├── README.md           * 本文件
├── CHANGELOG.md          版本变更记录（随包分发，用户可直接查这次改了什么）
│
├── tests/              * 自检套件（不打包）：test_*.py + run_all.sh + _pluginloader.py
├── deploy/               部署资产（**随插件分发**）：guardian-watchdog.py、
│                         install-watchdog.{sh,ps1}、systemd unit、
│                         guardian-watchdog.{sh,env.example}、docker-compose.yml
├── docs/               * 开发文档（不打包，不入库）
├── pyproject.toml      * ruff 配置
├── build_plugin_zip.py * 打包脚本
├── .dockerignore       * 镜像构建忽略
└── .gitignore          *
```

> 上面列出的是**模块**文件，每个目录下另有 `__init__.py` 未逐一写出。

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

**Q：想先「只看不动手」试跑一段时间怎么办？**
把 `profile` 设为 `maintenance`：该档位暂停周期巡检与所有自动动作，但
`/guardian check` 仍可手动跑并出完整结论。若既要继续周期巡检、又不想真的动手，
把 `containers.managed` 留空——所有自动重启都会以「不在动作白名单内」被拒，
审计里能看到完整的拒绝理由。观察满意后再放开。

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
- 环境指纹是**本机（容器内**）视角：内存/磁盘反映的是 AstrBot 容器，不是宿主机总量。

---

## 12. 许可

AGPL-3.0，见仓库根目录 `LICENSE`。
