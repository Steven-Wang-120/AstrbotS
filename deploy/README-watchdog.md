# 宿主看门狗（Guardian Host Watchdog）

宿主机上跑的小程序，负责两件**容器自己做不到**的事：

1. **定时睡眠/重启**：把 AstrBot 容器（以及 NapCat）真的停下来、过一会儿再拉起来。
   容器停在那里时没法自己醒来，所以执行方必须在容器外面。
2. **内核心跳守护**：`guardian-kernel` 卡死时把它重启。容器无法可靠地重启自己
   （自杀式重启后不保证能回来），所以同样必须由宿主来。

它只依赖 Python 3 标准库，不需要 `pip install` 任何东西。

---

## 为什么这次要改路径

插件（容器内）写请求文件的位置是：

```
/AstrBot/data/guardian/restart_request.json
```

而 `deploy/docker-compose.yml` 把宿主的 `./data` 挂到容器的 `/AstrBot/data`，所以
宿主机上对应的真实位置是：

```
<compose 目录>/data/guardian/restart_request.json
```

历史版本的看门狗默认值少写了一层（`<compose>/data/guardian`），容器名也写死成
`astrbot napcat`，与实际的 `astrbot-local napcat-local` 对不上。三处不一致的结果
是：**请求写出去无人消费，而且失败是静默的**——面板上显示"已提交"，容器其实没睡。

现在的做法是不猜：安装脚本问 `docker compose ps` 要真实容器名，并从 compose
文件里的 `:/AstrBot/data` 挂载反推数据目录。

---

## 安装

### Linux（systemd）

```bash
# 在 compose.yml 所在目录（或任意子目录，脚本会向上找）
sudo ./install-watchdog.sh

# 显式指定
sudo ./install-watchdog.sh --compose-dir /srv/astrbot-stack

# 只看看会写入什么
sudo ./install-watchdog.sh --dry-run

# 卸载
sudo ./install-watchdog.sh --uninstall
```

脚本做六件事：

1. 向上查找 `compose.yml` / `compose.yaml` / `docker-compose.yml`
2. 从 `:/AstrBot/data` 挂载反推宿主数据目录
3. `docker compose ps` 读真实容器名（自动排除 `guardian-kernel`）
4. 写 `/etc/guardian/guardian-watchdog.env`（权限 600，旧的自动备份）
5. 装 `guardian-watchdog.{sh,py}` 到 `/usr/local/bin`
6. 注册并启用 `guardian-watchdog.timer`（每 30 秒一次），最后跑一次自检

### Windows（计划任务）

以**管理员**身份打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\install-watchdog.ps1

# 只看看会写入什么
powershell -ExecutionPolicy Bypass -File .\install-watchdog.ps1 -DryRun

# 容器名探测失败时手工指定
powershell -ExecutionPolicy Bypass -File .\install-watchdog.ps1 -Targets astrbot-local,napcat-local

# 卸载
powershell -ExecutionPolicy Bypass -File .\install-watchdog.ps1 -Uninstall
```

脚本在 `C:\ProgramData\guardian\` 下生成：

| 文件 | 作用 |
|---|---|
| `guardian-watchdog.env` | 环境变量（路径、白名单、时限） |
| `run-watchdog.ps1` | 薄包装：注入 env 后跑一次 tick |
| `guardian-watchdog.log` | 运行日志 |

计划任务名 `GuardianWatchdog`，每 30 秒触发一次，`MultipleInstances=IgnoreNew`
（上一轮没跑完时不会叠加）。

> **两个脚本都必须保留 UTF-8 BOM**。PowerShell 5.1 在没有 BOM 时会按 ANSI 解码，
> 中文注释会被解坏，进而把 `<# ... #>` 注释块误当成代码。

---

## 装完之后必须做的事

在 AstrBot WebUI 的 Guardian 插件配置里确认两项，**否则睡眠只会作用于 NapCat，
本体不会参与**：

```
safety.self_restart_strategy = watchdog
safety.allow_self_restart     = true
```

然后重载插件。启动日志里出现下面这行就说明看门狗没被认到：

```
[guardian] 启动告警：未检测到宿主看门狗……
```

---

## 手工部署（不用安装脚本）

```bash
# 1. 复制两个脚本
sudo install -m 755 guardian-watchdog.sh /usr/local/bin/guardian-watchdog.sh
sudo install -m 644 guardian-watchdog.py /usr/local/bin/guardian-watchdog.py

# 2. 写环境变量，最少要这三项
sudo mkdir -p /etc/guardian
sudo tee /etc/guardian/guardian-watchdog.env >/dev/null <<'EOF'
GUARDIAN_COMPOSE_DIR=/opt/astrbot-stack
GUARDIAN_DATA_ROOT=/opt/astrbot-stack/data
GUARDIAN_ALLOWED_TARGETS=astrbot-local napcat-local
EOF

# 3. 装单元并启用
sudo install -m 644 guardian-watchdog.service /etc/systemd/system/
sudo install -m 644 guardian-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now guardian-watchdog.timer
```

变量清单见 `guardian-watchdog.env.example`，每一项都有注释。

---

## 验证

```bash
# 看门狗是否在跑
systemctl list-timers guardian-watchdog.timer
journalctl -u guardian-watchdog.service -n 30 --no-pager

# 手工投递一个请求，60 秒内应出现 restart_result.json
# 注意路径里要有 data 之后的 astrbot 段
echo '{"version":1,"target":"napcat-local","nonce":"manualtest01","ts":'$(date +%s)'}' \
  | sudo tee /opt/astrbot-stack/data/guardian/restart_request.json
ls -l /opt/astrbot-stack/data/guardian/
```

Windows：

```powershell
Get-ScheduledTaskInfo -TaskName GuardianWatchdog
Get-Content C:\ProgramData\guardian\guardian-watchdog.log -Tail 30
```

---

## 目录约定

```
<compose 目录>/
├── compose.yml
└── data/                        <- GUARDIAN_DATA_ROOT
    └── guardian/
        ├── restart_request.json     插件写入（容器内 /AstrBot/data/guardian/）
        ├── restart_result.json      看门狗写回
        ├── watchdog_state.json      看门狗账本（含 5 分钟 mtime 自检依据）
        ├── watchdog_state.lock
        ├── kernel_heartbeat.json    内核心跳
        ├── kernel_reload_request.json
        └── watchdog_kernel_state.json
```

看门狗自身状态文件（`watchdog_state.json`）的修改时间是插件启动自检的依据：
5 分钟内没被更新过，就视为看门狗没在运行。
