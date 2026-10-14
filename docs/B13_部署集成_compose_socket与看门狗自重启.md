# B13 · 部署集成（Compose / Docker Socket / 看门狗自重启）

> 上游权威规格：`B00_项目总览与需求规格.md`；配置键名一律沿用 B00 第 7 节，冲突以 B00 为准。
> 版本：v0.1.0　|　日期：2026-10-13

## 1. 目标

| # | 目标 | 落地物 | 验收 |
| --- | --- | --- | --- |
| P1 | 一键起双容器（`napcat` + `astrbot`） | `deploy/docker-compose.yml` | C01 |
| P2 | 插件经 `docker.sock` 读状态、重启白名单容器 | `group_add` + `docker.socket_path` | C04 |
| P3 | 本体容器可被安全重启（不做容器内自杀） | 宿主机看门狗 systemd 单元 | C04 / G-STABILITY |
| P4 | 重启请求有来源、新鲜度、白名单三重校验 | `guardian-watchdog.sh` | C12 |
| P5 | 可退避、可观测、可升级、可回滚、可卸载 | journald + `.env` + 本文 | G-INSTALL |

## 2. 部署拓扑

```
┌──────────────────────────── Arch Linux 宿主机 ─────────────────────────────┐
│ systemd                                                                    │
│  ├── docker.service ──► dockerd ──► /var/run/docker.sock (0660 root:docker)│
│  ├── guardian-watchdog.timer (OnBootSec=60s, OnUnitInactiveSec=30s)        │
│  │     └── guardian-watchdog.service (Type=oneshot, User=root)             │
│  │            └── guardian-watchdog.sh: flock → 读共享卷请求                │
│  │                 → 校验 nonce/ts/白名单 → docker compose restart <target> │
│  │                 → 写结果 → 失败指数退避                                   │
│  └── docker compose -f /opt/astrobot/deploy/docker-compose.yml             │
│         ├── container: napcat  (OneBot11 :3000, WebUI :6099)               │
│         │      volume: ./napcat/config → /app/napcat/config                │
│         └── container: astrbot (WebUI :6185, API/WS :6199)                 │
│                volume: ./data / 插件源码 / docker.sock / guardian-data      │
│                group_add: [${DOCKER_GID}] ← 免 root 访问 socket            │
└────────────────────────────────────────────────────────────────────────────┘
```

三条边界：**插件只读容器状态**（走 socket）、**插件只写请求文件**（不执行重启）、**宿主机只读请求文件并执行重启**（不信任容器内输入）。

## 3. `deploy/docker-compose.yml`（完整）

```yaml
# deploy/docker-compose.yml —— Guardian 双容器编排（Arch Linux）
name: astrbots
services:
  napcat:
    image: mlikiowa/napcat-docker:latest
    container_name: napcat
    restart: unless-stopped
    ports:
      - "3000:3000"            # OneBot11 HTTP / 反向 WS
      - "6099:6099"            # NapCat WebUI（暴露风险见 §12）
    volumes:
      - ./napcat/config:/app/napcat/config
    environment:
      TZ: "Asia/Shanghai"
      NAPCAT_UID: "${NAPCAT_UID:-0}"
      NAPCAT_GID: "${NAPCAT_GID:-0}"
    networks: [guardian-net]
  astrbot:
    image: soulter/astrbot:latest
    container_name: astrbot
    restart: unless-stopped
    ports:
      - "6185:6185"            # AstrBot WebUI
      - "6199:6199"            # AstrBot API / 反向 WS
    volumes:
      - ./data:/AstrBot/data
      - ./src/astrbot_plugin_guardian:/AstrBot/data/plugins/astrbot_plugin_guardian
      - /var/run/docker.sock:/var/run/docker.sock
      - guardian-data:/guardian
    group_add:
      - "${DOCKER_GID:?DOCKER_GID 未设置：先执行 getent group docker}"
    environment:
      TZ: "Asia/Shanghai"
      GUARDIAN_RESTART_DIR: "/guardian"
    depends_on: [napcat]
    networks: [guardian-net]
volumes:
  guardian-data:
    name: guardian-data
networks:
  guardian-net:
    name: guardian-net
```

要点：`container_name` 必须等于 B00 `containers.astrbot` / `containers.napcat` 默认值；`TZ` 规避 R7（cron 按容器本地时区解释）；`restart: unless-stopped` 只覆盖「进程退出」，**不覆盖**「容器活着但 QQ 掉线」「内存泄漏」，后者由探针负责；`depends_on` 只保证启动顺序，不保证 NapCat 已登录（登录由 B05 探针负责）。

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

看门狗破局：① 请求与执行**物理分离**（容器被杀时宿主机脚本仍在跑，能写完结果）；② **nonce 去重 + 新鲜度**抗旧请求重放；③ 宿主机侧**指数退避**；④ 插件侧还有 `action_cooldown_sec`、`max_actions_per_hour`、失败即停三重约束（B00 §10 第 4、6 条）。

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

## 7. 看门狗实现

### 7.1 请求 / 结果文件协议（schema v1）

`restart_request.json`（容器 → 宿主机）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `nonce` | string | UUID4 hex，去重与幂等键 |
| `ts` | float | 生成时刻 epoch 秒，宿主机据此判新鲜度 |
| `target` | string | 目标容器名，必须 ∈ 宿主机白名单 |
| `requested_by` | string | `llm:guardian_restart_container` / `cmd:/guardian restart` / `auto:memory_leak` |
| `reason` | string | 人类可读原因，写入审计 |

（另有固定字段 `schema: 1`。）`restart_result.json`（宿主机 → 容器）：`nonce`（回填配对）、`target`、`outcome`（`ok|failed|denied|skipped`）、`detail`、`finished_ts`。宿主机侧真实路径用 `docker volume inspect guardian-data -f '{{ .Mountpoint }}'` 取得（通常 `/var/lib/docker/volumes/guardian-data/_data`），写入 `/etc/default/guardian-watchdog` 的 `GUARDIAN_RESTART_DIR`。

### 7.2 `deploy/guardian-watchdog.sh`（完整）

```bash
#!/usr/bin/env bash
# 宿主机侧自重启看门狗（systemd oneshot 调用，不常驻）
set -euo pipefail
RESTART_DIR="${GUARDIAN_RESTART_DIR:-/var/lib/guardian}"
COMPOSE_DIR="${GUARDIAN_COMPOSE_DIR:-/opt/astrobot/deploy}"
COMPOSE_FILE="${GUARDIAN_COMPOSE_FILE:-${COMPOSE_DIR}/docker-compose.yml}"
ALLOWED_TARGETS="${GUARDIAN_ALLOWED_TARGETS:-astrbot napcat}"
MAX_AGE_SEC="${GUARDIAN_MAX_AGE_SEC:-180}"; BACKOFF_MAX="${GUARDIAN_BACKOFF_MAX:-3600}"
STATE_DIR="${GUARDIAN_STATE_DIR:-/var/lib/guardian}"
REQ_FILE="${RESTART_DIR}/restart_request.json"; RES_FILE="${RESTART_DIR}/restart_result.json"
NONCE_FILE="${STATE_DIR}/watchdog.last_nonce"; FAIL_FILE="${STATE_DIR}/watchdog.fails"
BACKOFF_FILE="${STATE_DIR}/watchdog.backoff_until"
log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }
mkdir -p "$STATE_DIR" "$RESTART_DIR"
exec 9>"${STATE_DIR}/.watchdog.lock"; flock -n 9 || { log "locked; exit"; exit 0; }
NOW="$(date +%s)"
if [[ -f "$BACKOFF_FILE" ]]; then
  UNTIL="$(cat "$BACKOFF_FILE" 2>/dev/null || echo 0)"
  [[ "$UNTIL" =~ ^[0-9]+$ ]] && (( NOW < UNTIL )) && { log "backoff until ${UNTIL}; skip"; exit 0; }
fi
[[ -f "$REQ_FILE" ]] || exit 0
IFS=$'\t' read -r NONCE TS TARGET REQUESTED_BY < <(python3 - "$REQ_FILE" <<'PY'
import json, sys
try: d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception: print("PARSE_ERROR\t0\t-\t-"); raise SystemExit(0)
print("\t".join([str(d.get("nonce","")), str(int(d.get("ts",0))),
                 str(d.get("target","")), str(d.get("requested_by","-"))]))
PY
)
write_result() {   # $1=outcome $2=detail
python3 - "$RES_FILE" "$NONCE" "$TARGET" "$1" "$2" "$REQUESTED_BY" <<'PY'
import json, os, sys, tempfile, time
res, nonce, target, outcome, detail, by = sys.argv[1:7]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(res) or ".", prefix=".res.", suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump({"schema": 1, "nonce": nonce, "target": target, "outcome": outcome,
               "detail": detail, "requested_by": by, "finished_ts": time.time(),
               "watchdog": "guardian-watchdog"}, fh, ensure_ascii=False)
    fh.flush(); os.fsync(fh.fileno())
os.chmod(tmp, 0o660); os.replace(tmp, res)      # 原子替换
PY
}
# 校验：可解析 / nonce 去重 / 新鲜度（容忍 30s 负偏）/ 字符集 / 白名单（纵深防御）
if [[ -z "${NONCE:-}" || "$NONCE" == "PARSE_ERROR" ]]; then
  log "unparsable request; quarantine"; mv -f "$REQ_FILE" "${REQ_FILE}.bad.${NOW}" 2>/dev/null || true; exit 1
fi
if [[ -f "$NONCE_FILE" && "$(cat "$NONCE_FILE")" == "$NONCE" ]]; then
  log "nonce ${NONCE} already processed; drop"; rm -f "$REQ_FILE"; exit 0
fi
AGE=$(( NOW - TS ))
if (( AGE < -30 || AGE > MAX_AGE_SEC )); then
  log "stale request age=${AGE}s; drop"; write_result skipped stale_request; rm -f "$REQ_FILE"; exit 0
fi
if [[ ! "$TARGET" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
  log "illegal target '${TARGET}'; refuse"; write_result denied illegal_target_charset; rm -f "$REQ_FILE"; exit 1
fi
ALLOWED=0; for t in $ALLOWED_TARGETS; do [[ "$t" == "$TARGET" ]] && ALLOWED=1; done
if (( ALLOWED == 0 )); then
  log "target '${TARGET}' not in host allowlist; refuse"; write_result denied target_not_allowed; rm -f "$REQ_FILE"; exit 1
fi
[[ -f "$COMPOSE_FILE" ]] || { log "compose file missing"; write_result failed compose_file_missing; exit 1; }
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose); else COMPOSE=(docker-compose); fi
log "restart target='${TARGET}' by='${REQUESTED_BY}' nonce='${NONCE}'"
if (cd "$COMPOSE_DIR" && "${COMPOSE[@]}" -f "$COMPOSE_FILE" restart "$TARGET"); then
  echo "$NONCE" > "$NONCE_FILE"; echo 0 > "$FAIL_FILE"; rm -f "$BACKOFF_FILE" "$REQ_FILE"
  write_result ok compose_restart_ok; log "restart ok"; exit 0
else
  RC=$?; write_result failed "compose_restart_rc_${RC}"; rm -f "$REQ_FILE"
  FAILS=$(( $(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1 )); echo "$FAILS" > "$FAIL_FILE"
  DELAY=$(( 30 * (1 << (FAILS - 1)) )); (( DELAY > BACKOFF_MAX )) && DELAY=$BACKOFF_MAX
  echo $(( NOW + DELAY )) > "$BACKOFF_FILE"; log "restart FAILED rc=${RC}; backoff ${DELAY}s"; exit 1
fi
```

依赖：`bash`、`flock`（util-linux）、`python3`、`docker`（含 compose 插件）。脚本只执行 `restart` 子命令，不接受任何额外参数。

### 7.3 `deploy/guardian-watchdog.service`

```ini
[Unit]
Description=Guardian 自重启看门狗（消费容器内重启请求）
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=oneshot
EnvironmentFile=-/etc/default/guardian-watchdog
ExecStart=/opt/astrobot/deploy/guardian-watchdog.sh
User=root
TimeoutStartSec=300
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/guardian /var/lib/docker/volumes/guardian-data

[Install]
WantedBy=multi-user.target
```

加固项遇权限问题先注释逐条排查。

### 7.4 `deploy/guardian-watchdog.timer`

```ini
[Unit]
Description=定时触发 Guardian 看门狗（每 30 秒）

[Timer]
OnBootSec=60s
OnUnitInactiveSec=30s
Unit=guardian-watchdog.service

[Install]
WantedBy=timers.target
```

用 `OnUnitInactiveSec` 而非 `OnUnitActiveSec`：上一次 oneshot（含重启等待）跑完才计时下一次，天然避免堆叠。

### 7.5 安装与启用

```bash
sudo mkdir -p /opt/astrobot && sudo cp -a ./deploy ./docs ./src /opt/astrobot/
sudo chmod +x /opt/astrobot/deploy/guardian-watchdog.sh
docker volume create guardian-data
MP="$(docker volume inspect guardian-data -f '{{ .Mountpoint }}')"   # 记下该路径
sudo tee /etc/default/guardian-watchdog >/dev/null <<EOF
GUARDIAN_RESTART_DIR=${MP}
GUARDIAN_COMPOSE_DIR=/opt/astrobot/deploy
GUARDIAN_COMPOSE_FILE=/opt/astrobot/deploy/docker-compose.yml
GUARDIAN_ALLOWED_TARGETS="astrbot napcat"
GUARDIAN_MAX_AGE_SEC=180
GUARDIAN_BACKOFF_MAX=3600
EOF
sudo install -m 0644 /opt/astrobot/deploy/guardian-watchdog.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now guardian-watchdog.timer
sudo systemctl start guardian-watchdog.service
systemctl list-timers guardian-watchdog.timer; journalctl -t guardian-watchdog -n 50 --no-pager
# 端到端自检：造请求重启 napcat，35s 后查结果
sudo python3 -c "
import json,time,uuid; json.dump({'schema':1,'nonce':uuid.uuid4().hex,'ts':time.time(),
 'target':'napcat','requested_by':'manual:install_check','reason':'smoke'},
 open('${MP}/restart_request.json','w'))"
sleep 35; cat "${MP}/restart_result.json"   # 期望 outcome=ok
```

失败自检：`target_not_allowed` → 查 `GUARDIAN_ALLOWED_TARGETS`；`stale_request` → 查时钟（`timedatectl`、`chronyc tracking`）。

### 7.6 `deploy/.env.example`

```dotenv
# cp deploy/.env.example deploy/.env 后按实际值修改
DOCKER_GID=998                   # getent group docker / stat -c '%g' /var/run/docker.sock
NAPCAT_UID=0
NAPCAT_GID=0
TZ=Asia/Shanghai
GUARDIAN_RESTART_DIR=/guardian   # 容器内挂载点，一般不改
```

## 8. 插件侧：写重启请求（`watchdog` 策略）

```python
# src/astrbot_plugin_guardian/actions/self_restart.py
import json, os, tempfile, time, uuid
from pathlib import Path

RESTART_DIR = Path(os.environ.get("GUARDIAN_RESTART_DIR", "/guardian"))
REQ_NAME = "restart_request.json"

def write_restart_request(target: str, requested_by: str, reason: str = "") -> dict:
    """原子写重启请求；返回载荷（含 nonce，供后续配对结果）。"""
    RESTART_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "nonce": uuid.uuid4().hex, "ts": time.time(),
               "target": target, "requested_by": requested_by, "reason": reason[:200]}
    fd, tmp = tempfile.mkstemp(dir=str(RESTART_DIR), prefix=".req.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            fh.flush(); os.fsync(fh.fileno())          # 先落盘（R6 缓解）
        os.chmod(tmp, 0o660)
        os.replace(tmp, RESTART_DIR / REQ_NAME)        # 原子替换：宿主机永不读到半截文件
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return payload
```

结果文件由插件按 `nonce` 配对读取；读不到不算失败——重启后进程已换代，靠审计与 journald 兜底。动作层调用点（安全校验之后）：

```python
# actions/container_actions.py（节选）
async def execute(self, spec: ActionSpec, guard: SafetyGuard) -> ActionResult:
    if spec.target == self.self_container:                  # astrbot 本体
        if not self.cfg.safety.allow_self_restart:
            return ActionResult(ok=False, detail="self_restart_disabled", elapsed_ms=0)
        strategy = self.cfg.safety.self_restart_strategy
        if strategy == "disabled":
            return ActionResult(ok=False, detail="strategy_disabled", elapsed_ms=0)
        if strategy == "watchdog":
            p = write_restart_request(spec.target,
                    spec.params.get("requested_by", "auto:guardian"),
                    spec.params.get("reason", ""))
            return ActionResult(ok=True, elapsed_ms=0,
                                detail=f"restart_request_queued nonce={p['nonce']}")
        return await self._restart_via_docker_api(spec.target)   # docker_api，见 §10
    return await self._restart_via_api(spec.target)              # 非本体容器：直接走 Engine API
```

## 9. 安全约束（纵深防御）

| 层 | 约束 | 位置 | 违反后果 |
| --- | --- | --- | --- |
| L1 配置 | `safety.allowed_containers` ∪ `containers.managed` 白名单 | 插件 | 拒绝 + 审计 |
| L2 安全层 | 冷却 / 熔断 / dry-run | `core/safety.py` | 抑制 + 升级告警 |
| L3 权限 | `llm_tools.require_admin` + 危险确认 | `tools/llm_tools.py` | 非管理员拒绝（R4） |
| L4 协议 | 请求文件只含枚举目标，无自由命令 | `restart_request.json` | 无法诱导宿主机执行任意命令 |
| L5 宿主机 | **再次**校验 `target ∈ GUARDIAN_ALLOWED_TARGETS` | `guardian-watchdog.sh` | `outcome=denied` |

L5 的意义：即使容器被攻破、请求文件被改成 `target: "postgres"`，宿主机也只拒绝并记录 `target_not_allowed`。**容器内写权限 ≠ 宿主机执行权限**，这是本设计最重要的边界。补充规则：共享卷只挂给 `astrbot` 一个容器；请求/结果文件权限 `0660`；目标名须匹配 `^[A-Za-z0-9][A-Za-z0-9_.-]*$`（杜绝 `--force`、`$(...)` 注入）；插件只读结果文件，不据其反向触发动作（避免环状触发）。

## 10. `docker_api` 策略（备选）

```python
# actions/self_restart.py（docker_api 策略）：POST /containers/{name}/restart
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

时序必须「**先回执，再重启**」：先 `await ctx.send_message(...)` 告诉用户，再 `await asyncio.sleep(2.0)` 留出推送时间，最后才 `restart_via_docker_api(...)`——该调用之后的代码大概率执行不完，因为进程已被 SIGTERM。

风险提示：① 在途请求中断；② 日志丢失（见 §5）；③ 重启风暴——必须保留 `action_cooldown_sec ≥ 600`、`max_actions_per_hour ≤ 6`、失败即停（B00 §10 第 6 条）；④ 审计缺口——建议宿主机旁路记录 `docker events --filter container=astrbot --filter event=restart`；⑤ 依赖 socket 写权限，组权限错会 `Permission denied`（R2）。

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
    networks: [guardian-net]
```

| 项 | 直挂 socket（本期） | socket-proxy（预览） |
| --- | --- | --- |
| 权限面 | 全量 Docker API（等价 root） | 白名单端点 |
| 对 `docker.socket_path` 的影响 | 直接可用 | 需改 `DOCKER_HOST=tcp://...:2375`，`utils/dockerclient.py` 需支持 |
| 本期状态 | 默认 | 未交付，列为 B14 Roadmap |

## 12. Arch Linux 特有注意事项

| 项 | 说明 / 命令 |
| --- | --- |
| systemd 版本 | 滚动更新，systemd ≥ 250 是常态；本单元只用 `oneshot` / `OnUnitInactiveSec` / `ReadWritePaths`，兼容无忧（`systemctl --version`） |
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
git -C src/astrbot_plugin_guardian pull --ff-only
docker compose -f deploy/docker-compose.yml up -d astrbot && docker logs -f astrbot | grep -i guardian
# 升级：宿主机侧（动看门狗）
sudo cp -a /opt/astrobot /opt/astrobot.bak.$(date +%Y%m%d%H%M)
sudo install -m 0755 deploy/guardian-watchdog.sh /opt/astrobot/deploy/guardian-watchdog.sh
sudo systemctl daemon-reload && sudo systemctl restart guardian-watchdog.timer
# 回滚
git -C src/astrbot_plugin_guardian checkout <last_good_tag>
docker compose -f deploy/docker-compose.yml restart astrbot
mv data data.broken && mv data.bak.YYYYMMDDHHMM data    # 仅数据损坏时（会丢最新事件）
sudo cp -a /opt/astrobot.bak.YYYYMMDDHHMM/deploy/guardian-watchdog.sh /opt/astrobot/deploy/
sudo systemctl restart guardian-watchdog.timer
```

回滚触发条件：升级后 30 分钟内出现「连续重启 ≥ 3 次」「`/guardian status` 无响应」「事件循环阻塞告警」之一。

## 14. 卸载

```bash
# 顺序很重要：先停 timer，再停容器。反过来会出现"容器已停、看门狗仍在轮询"的日志噪声。
sudo systemctl disable --now guardian-watchdog.timer && sudo systemctl stop guardian-watchdog.service
sudo rm -f /etc/systemd/system/guardian-watchdog.{service,timer} /etc/default/guardian-watchdog
sudo systemctl daemon-reload
docker compose -f deploy/docker-compose.yml down      # 保留数据卷，便于回滚
docker volume rm guardian-data && rm -rf data napcat/config   # 不可逆
```

## 15. 验收标准（对应 C01 / C04）

| # | 步骤 | 预期 | 证据 | 用例 |
| --- | --- | --- | --- | --- |
| A1 | `docker compose up -d` | 两容器 `Up` | `docker compose ps` | C01-01 |
| A2 | 容器内 `ls -l /var/run/docker.sock` | 可见，属组 GID == `.env` 的 `DOCKER_GID` | 命令输出 | C01-02 |
| A3 | 插件加载 | 日志出现 guardian 启动行，无 Traceback | `docker logs astrbot` | C01-03 / G-INSTALL |
| A4 | `/guardian status` | 返回快照文本，`docker_daemon=ok` | 聊天截图 + 日志 | C01-04 |
| A5 | 手工写请求 target=napcat | ≤ 35s 内结果 `ok`，`napcat` `StartedAt` 更新 | 两文件 + `docker inspect` | C04-05 |
| A6 | 写请求 target=postgres | `denied / target_not_allowed`，容器未重启 | 结果文件 + journald | C04-06 / G-SAFETY |
| A7 | 请求 `ts` 改为 1 小时前 | `skipped / stale_request` | 结果文件 | C04-07 |
| A8 | 同一 nonce 连写两次 | 第二次日志 `already processed`，只重启一次 | journald 行数 | C04-08 |
| A9 | `systemctl stop docker` 后巡检 | `docker_daemon` 转 `crit`，插件降级告警不崩溃 | `/guardian status` | C04-09 / R2 |
| A10 | `.env` 的 GID 改错后重建 | 探针报权限错误并告警，无未捕获异常 | 日志 + status | C04-10 |

## 16. 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-10-13 | v0.1.0 | 初版：compose 编排、GID 说明、三策略对比、看门狗脚本/单元/定时器、安装/升级/回滚/卸载、C01/C04 验收 |
