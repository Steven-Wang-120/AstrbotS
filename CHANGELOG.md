# 变更记录

本文件记录 `astrbot_plugin_guardian` 的用户可见变更。格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

「不兼容变更」会以 **不兼容** 标出，升级前请先读该条。

---

## [0.8.0] - 2026-10-18

这一版的主题是**让 Bot 自己开口说要睡了**，以及**看门狗不再需要手动装**。

### 新增

- **主动播报：Bot 到点自己去跟管理员报备**（`core/announce.py`）
  - 按生命周期等级、作息窗口、最近是否有人说话，判定「该不该开口」；
    具体说什么由 LLM 拿着人格和最近对话现场决定，插件里**零成品句子**。
  - 三类事件：睡前报备（`sleep`）、睡醒报备（`resume`）、
    「困了但还没睡成」报备（`deferred`）。
  - 防重复用**事件键**（如 `sleep:L3:601`）而不是时间窗口——Bot 说完就去睡，
    睡下后不可能再发第二条，频率限制在这个场景里没有约束对象。
  - 收件人来自 `state.json` 的 `announce.admin_umo` / `announce.last_umo`
    （运行时状态，**不是**配置项），由 `observe_message` 填充，
    **仍然不读消息内容**。管理员的消息绕过 10 秒合并窗口，避免报备发错人。
  - 新增 `/guardian announce [sleep|deferred|resume]` 预览命令，
    不发送、不占坑、不改状态。
  - 配置项：`lifecycle.sleepy_announce`、`lifecycle.sleepy_announce_max_unanswered`、
    `lifecycle.post_sleep_announce`，以及 `announce.*` 模板（见 `_conf_schema.json`）。
    模板支持 14 个占位符；**写错的花括号会原样留在消息里**，便于一眼看出模板配错。

- **被问「你困吗」**（`guardian_drowsy` 工具 + `/guardian drowsy` 命令）
  - 与主动播报刻意分开：被问时只交出**事实**（在线多久、几级、离窗口多远），
    由正在回话的模型自己组织语言，内核不再调 provider——否则对话里会出现两套话。
  - `drowsy_report()` 不带闸门，任何等级都有答案；`sleepy_facts()`
    才回答「要不要主动开口」。

- **看门狗随插件自带并自动安装**（`core/watchdog_install.py`）
  - 插件加载时通过已挂载的 `docker.sock` 起一个一次性容器
    （宿主根挂到 `/host`、`PidMode: host`），在里面执行
    `deploy/install-watchdog.sh`，装完立即删除。
  - compose 目录从容器标签 `com.docker.compose.project.working_dir` 自动探测，
    不需要手填。
  - 退出口：`safety.auto_install_watchdog=false` 则完全不动宿主机。
  - `deploy/` 改为**随插件分发**——没有脚本只能退化成提示。
  - 平台边界如实返回：Windows / macOS 宿主机直接给手动命令；
    宿主无可用镜像（`alpine`/`busybox`/`debian-slim`）时失败并提示，**绝不自动拉镜像**。

### 变更

- `/guardian announce` 在低等级下也能看到输出：新增 `preview_facts()`，
  假设等级已到、其余取真实时钟与配置。此前预览复用决策函数，
  等于「要等 25 天才能确认它会说什么」。
- 看门狗自检在 `allow_self_restart=false` 时不再静默返回空串，
  改为说明「当前不影响，将来开启会坏」。
- 版本号口径统一：`metadata.yaml`、`main.py`、`__init__.py`、README 一致为 `0.8.0`。
- 插件市场描述去掉已退役的 `dry-run` 表述，改为「白名单、冷却、每小时熔断、
  连续失败降级」四重约束。

### 修复

- 主动消息在 `notify.umo_list` 为空时**没有收件人**（所有 `Notifier.push`
  退化成一行 `notify.no_target` 日志），现由 `announce.admin_umo` /
  `announce.last_umo` 兜底。
- `claim_announce` 改为在 **LLM 生成成功之后**才调用，避免一次网络超时
  就把这次播报机会烧掉、而 Bot 其实一句没说出来。

### 测试

- 新增 `tests/test_watchdog_install.py`（14 项）、`tests/test_announce.py`，
  后者覆盖判定矩阵 / 幂等 / 路由 / 输出清洗 / 旧状态兼容。

---

## [0.7.0] - 2026-10-18

主题：**作息窗口**——困意由运行时长决定，但「现在能不能睡」由时段决定。

### 新增

- **作息窗口**（`core/timewindow.py`，`circadian.*` 配置）
  - `circadian.enabled` / `asleep_start` / `asleep_end` / `max_defer_hours` /
    `quiet_minutes`。窗口只做**闸门，不做触发器**。
  - 支持跨零点；`asleep_start == asleep_end` 表示零长度窗口，
    即关闭该特性（想要全天请用 `enabled: false`）。
  - 窗口外的自主睡眠会推迟并记审计 `sleep.deferred`；管理员命令与定时任务
    不受窗口约束。
- **顺延上限优先于「有人说话」**：`max_defer_hours`（默认 12h）排在
  `recent_talk` 判据之前。否则「整夜都有人在聊天」会让顺延被无限推后，
  30 天保证被聊穿。超限时无视窗口执行，并记 `sleep.defer_override`。
- **本地时间与时段的语义化描述注入**（`core/timefmt.py`），供提示词与播报使用。
- 困意可见性（`inject_prompt`）受窗口门控；`level_index` / `sleep_armed`
  仍只由 uptime 决定，不受窗口影响。
- 跨平台看门狗安装脚本（`deploy/install-watchdog.{sh,ps1}`）。

### 变更

- **不兼容**：`lifecycle.levels` 中的阈值与提示词仍可配，但等级推进的
  **睡眠动作**现在要先过作息窗口闸门；依赖「到点立刻睡」的部署需设
  `circadian.enabled: false`。
- 定时重启配置改为「几点 + 星期几」，不再是 cron 表达式。
- 前端移除四项可由 Docker 推断的字段（容器名、镜像、socket 路径等），
  改为自动探测 / 协商。

### 修复

- `tz_offset_h` 在无 `TZ` 的容器里恒为 `0`（`time.timezone` 的默认值）。
  改按 `TZ` → `astimezone` → `altzone` → `timezone` 的顺序取值，并落盘到
  `state.json`。时区错了会让窗口判定整体偏移，属于硬伤。
- 看门狗请求路径与容器名对齐，`GUARDIAN_DOCKER_BACKEND` 的
  「服务名 vs 容器名」语义写进文档。
- 等级表条目补回 `__template_key`，修复前端显示「未指定模板」。
- `build_plugin_zip.py` 不再硬编码产物版本号，改为从 `metadata.yaml` 读取
  （唯一真相源），避免版本一升就打错包名。

---

## [0.6.0] - 2026-10-16

主题：**账密登录恢复阶梯**——会话失效时，重启多少次都回不来。

### 新增

- **账密登录恢复阶梯**（`actions/qq_actions.py`，`utils/napcat.py`，B17）
  - 流程：预检是否已在线 → 有条件重启 → 等就绪 → 账密登录 → 复核在线 → 写回。
  - `qq.account` / `qq.password`（建议 32 位小写 md5）/ `qq.webui_public_base` /
    `qq.restart_grace_sec`。**`qq.account` 留空即退化为 0.5.0 行为**：只重启，不登录。
  - `qq.relogin_method`：`restart_container`（默认）/ `login_only` / `disabled`。
    原 `webui_quick_login` 已更名，加载时自动迁移为 `login_only` 并告警。
  - 登录失败分三类：密码错、需验证码、需新设备验证。三类都**不追加重启**，
    且失败后 6 小时内不再尝试账密，防试错锁号。
  - QQ 离线时**不发 QQ 通知**（发了必失败，还可能把管理员 UMO 拉黑），
    改走登录状态页 `/api/plug/astrbot_plugin_guardian/login` + 落盘
    `last_login_error.txt`。
- `/guardian login`、`/guardian webui` 命令（管理员）。
- 新增 `qq.activity_evidence_sec`：把「最近有消息事件」作为**弱正据**，
  在没有 OneBot 11 HTTP 的部署里这是唯一可用的证据链。

### 修复

- **被踢下线但未重启时**就绪判据不成立（`qrcodeurl` 为空）→ 阶梯直接返回
  `not_ready`。新增 `_kicked_offline()`，命中 `KickedOffLine` / `下线通知` 时
  绕开宽限期直接重启。
- **手动 `/guardian login` 会重启健康的容器**：补 [1.5] 预检，已在线则直接
  走成功收工路径，不重启、不发密码。
- NapCat 对失效 Credential 返回 **HTTP 200 + `{"code":-1,"message":"Unauthorized"}`**，
  旧代码只看状态码，把错误体当成「未登录」正常结果 → 90 秒空转。
  新增 `_is_auth_failure()` 与缓存清除重试。
- 就绪窗口超时后明明已在线却被判失败：超时后先用 `GetQQLoginInfo.online`
  复核一次，在线即判成功。
- 登录失败后 `qq.last_failure` / `qq.auth_hold_until` 未登记进 `PERSISTED_KEYS`，
  落盘即被丢弃 → 已登记。
- 静默期/成功时间延迟约一个巡检周期才落盘（`flush()` 20s 防抖），进程被强杀
  即丢失 → 失败与成功出口均强制立即落盘。

---

## [0.5.0] - 2026-10-15

主题：**独立内核**——把守护逻辑从 AstrBot 进程里拆出来。

### 新增

- **独立内核服务**（`kernel/`、`ipc/`）：内核跑在独立进程，不 `import astrbot`，
  通过 IPC 与适配层通信。`python -m astrbot_plugin_guardian.kernel` 可直接启动。
- **动作直连（D2-b）**：内核自己通过 `docker.sock` 执行动作，并立即复检
  `StartedAt` + 健康状态，不再依赖看门狗转手，也没有 timer 延迟。
- 看门狗心跳守护 `KernelGuard` + 数据目录迁移。
- `@self` / `@napcat` 目标别名，改名时不需要同步改多处。
- 容器名单一事实来源：`containers.astrbot` / `containers.napcat` 是全部
  「目标名」的唯一出处；`is_protected()` 改为「本体无条件受保护 **或** 在额外列表」。

### 变更

- **不兼容**：`safety.protected_containers` 默认值由 `["astrbot"]` 改为 `[]`
  （本体已无条件受保护，重复写反而在改名后失效）。旧默认值会在加载时迁移并告警。
- **不兼容**：彻底移除全局 `dry_run` 开关，空跑降级为动作级语义。
- 仓库整体改为「仓库根即插件根」，不再是 `src/astrbot_plugin_guardian/` 布局。
  仓库名 `AstrbotS` 与插件名无关，AstrBot 以解压后的目录名作为插件目录名。

### 修复

- 容器名散落在多处 → 改名后本体保护**静默失效**。改为结构化判定。
- 巡检 finding 触发动作时抛 `NameError`，动作被静默吞掉。
- 自愈重连后对「有副作用方法」静默重试 → 重复执行风险。
- `docker stop/restart` 必然超时 → 被误判为「daemon 不可达」。
- 适配层调内核没设超时，长动作会中途断连。
- 内核自己的巡检会把「正在睡眠的目标」提前唤醒，并制造假阳性复核。
- `_verify_restarted` 把「启动中」当失败、且只等 8s → 每次复核都假阴性。
  现把 `starting` 视为过渡态，仅 `unhealthy` 判失败，默认等待 30s。

---

## [0.4.0] 及更早

0.4.0 之前是文档冻结与骨架搭建期（里程碑 M1–M8），没有对外发布版本。
主要能力在此阶段一次性成型：

- 容器健康守护、QQ 掉线重登、内存泄漏防护、ROS2 节点/话题守护；
- 生命周期等级与自主睡眠（默认 168h / 360h / 600h / 720h，720h = 30 天硬上限）；
- 9 个 LLM 工具、告警通知（限流 + 去重 + 档位静默）、
  安全护栏（白名单 / 冷却 / 每小时熔断 / 受保护容器 / 连续失败降级）；
- 运行环境自述探针 `env_platform`（从宿主机侧采集系统 / 内核 / 架构 / 内存 / 时区）。

---

## 版本与提交对照

仓库目前只有 `v0.5.0` 打了 tag，其余版本以 `metadata.yaml` 的 `version` 字段
和发布提交为准。查某个版本的实际改动：

```bash
git log --oneline v0.5.0..HEAD        # 0.5.0 之后的全部提交
git show <commit>                     # 单个发布的完整说明
```
