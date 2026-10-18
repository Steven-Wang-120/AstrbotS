#!/usr/bin/env bash
# Guardian 宿主看门狗 —— Linux 一键安装 / 更新
#
# 为什么需要这个脚本：插件（容器内）把睡眠请求写到
# /AstrBot/data/guardian/restart_request.json，映射到宿主是
# <compose>/data/astrbot/guardian/restart_request.json；而看门狗白名单里的
# 容器名又必须和实际 `docker compose ps` 出来的名字一致。这三处任何一处
# 手工写错，请求都会写出去无人消费，而且**失败是静默的**。
# 本脚本把这三处全部自动探测。
#
# 用法：
#   sudo ./install-watchdog.sh                      # 在当前目录向上找 compose.yml
#   sudo ./install-watchdog.sh --compose-dir /srv/astrbot
#   sudo ./install-watchdog.sh --targets astrbot-local napcat-local
#   sudo ./install-watchdog.sh --dry-run            # 只打印，不落地
#   sudo ./install-watchdog.sh --uninstall
#
# 幂等：重复执行只更新配置与会话单元，不会重复注册。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system
ENV_DIR=/etc/guardian
ENV_FILE="$ENV_DIR/guardian-watchdog.env"
BIN_DIR=/usr/local/bin
SERVICE_NAME=guardian-watchdog.service
TIMER_NAME=guardian-watchdog.timer

COMPOSE_DIR=""
TARGETS=""
DATA_ROOT=""
DRY_RUN=0
UNINSTALL=0

die() { printf '错误：%s\n' "$1" >&2; exit 1; }
note() { printf '  %s\n' "$1"; }
step() { printf '\n== %s ==\n' "$1"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --compose-dir) COMPOSE_DIR="${2:-}"; shift 2 ;;
    --targets) TARGETS="${2:-}"; shift 2 ;;
    --data-root) DATA_ROOT="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "未知参数 $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "需要 root（systemd 单元默认在 /etc 下）"
command -v docker >/dev/null 2>&1 || die "找不到 docker 命令"
command -v systemctl >/dev/null 2>&1 || die "找不到 systemctl"

# --------------------------------------------------------------------- #
if [ "$UNINSTALL" -eq 1 ]; then
  step "卸载"
  systemctl disable --now "$TIMER_NAME" 2>/dev/null || true
  rm -f "$UNIT_DIR/$TIMER_NAME" "$UNIT_DIR/$SERVICE_NAME"
  rm -f "$BIN_DIR/guardian-watchdog.sh" "$BIN_DIR/guardian-watchdog.py"
  systemctl daemon-reload
  note "已移除 systemd 单元与脚本；$ENV_FILE 保留（内含你的容器白名单）"
  exit 0
fi

# --------------------------------------------------------------------- #
step "1/6 定位 compose 目录"
if [ -z "$COMPOSE_DIR" ]; then
  probe="$(pwd)"
  while [ "$probe" != "/" ]; do
    if [ -f "$probe/compose.yml" ] || [ -f "$probe/compose.yaml" ] || \
       [ -f "$probe/docker-compose.yml" ] || [ -f "$probe/docker-compose.yaml" ]; then
      COMPOSE_DIR="$probe"; break
    fi
    probe="$(dirname "$probe")"
  done
fi
[ -n "$COMPOSE_DIR" ] || die "找不到 compose.yml，请用 --compose-dir 指定"
COMPOSE_DIR="$(cd "$COMPOSE_DIR" && pwd)"
note "compose 目录：$COMPOSE_DIR"

# --------------------------------------------------------------------- #
step "2/6 探测数据目录"
if [ -z "$DATA_ROOT" ]; then
  # 优先相信 compose 文件里 `./data:/AstrBot/data` 这类挂载；
  # 找不到就退回 compose 目录下的 data/。
  DATA_ROOT="$(awk '
    /^[[:space:]]*-[[:space:]]*[^:]+:\/AstrBot\/data[[:space:]]*$/ {
      line = $0
      sub(/^[[:space:]]*-[[:space:]]*/, "", line)
      sub(/:\/AstrBot\/data[[:space:]]*$/, "", line)
      if (line ~ /^\//) print line; else { sub(/^\.\//, "", line); print "'"$COMPOSE_DIR"'/" line }
      exit
    }' "$COMPOSE_DIR"/compose.y*ml "$COMPOSE_DIR"/docker-compose.y*ml 2>/dev/null || true)"
  [ -n "$DATA_ROOT" ] || DATA_ROOT="$COMPOSE_DIR/data"
fi
[ -d "$DATA_ROOT" ] || die "数据目录不存在：$DATA_ROOT（先用 docker compose up -d 起一次）"
note "数据目录：$DATA_ROOT"
[ -d "$DATA_ROOT/guardian" ] || note "提示：$DATA_ROOT/guardian 尚未创建，看门狗启动时会自动建"

# --------------------------------------------------------------------- #
step "3/6 探测容器名"
if [ -z "$TARGETS" ]; then
  raw="$(cd "$COMPOSE_DIR" && docker compose ps --format '{{.Name}}' 2>/dev/null || true)"
  if [ -z "$raw" ]; then
    raw="$(cd "$COMPOSE_DIR" && docker compose ps --format json 2>/dev/null \
           | sed -n 's/.*"\([Nn]ame\)":[[:space:]]*"\([^"]*\)".*/\2/p' || true)"
  fi
  TARGETS="$(printf '%s\n' "$raw" | tr -d '\r' | awk 'NF' | grep -Evx 'guardian-kernel' | sort -u | tr '\n' ' ' | sed 's/ *$//')"
fi
[ -n "$TARGETS" ] || die "读不到运行中的容器，请用 --targets 手工指定（如 --targets astrbot-local napcat-local）"
count="$(printf '%s\n' $TARGETS | wc -l)"
[ "$count" -le 10 ] || die "容器数量 $count 超过看门狗上限 10"
note "允许的目标：$TARGETS"

KERNEL_CONTAINER="$(printf '%s\n' "$(cd "$COMPOSE_DIR" && docker compose ps --format '{{.Name}}' 2>/dev/null | tr -d '\r')" \
                    | grep -i 'kernel' | head -n1 || true)"
[ -n "$KERNEL_CONTAINER" ] || KERNEL_CONTAINER=guardian-kernel
note "内核容器：$KERNEL_CONTAINER"

# --------------------------------------------------------------------- #
step "4/6 生成 $ENV_FILE"
[ -f "$HERE/guardian-watchdog.py" ] || die "脚本同目录缺少 guardian-watchdog.py"
if [ "$DRY_RUN" -eq 1 ]; then
  note "[dry-run] 将写入："
  note "  GUARDIAN_COMPOSE_DIR=$COMPOSE_DIR"
  note "  GUARDIAN_DATA_ROOT=$DATA_ROOT"
  note "  GUARDIAN_ALLOWED_TARGETS=$TARGETS"
  note "  GUARDIAN_KERNEL_CONTAINER=$KERNEL_CONTAINER"
  exit 0
fi
mkdir -p "$ENV_DIR"
[ -f "$ENV_FILE" ] && cp -a "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
umask 077
cat >"$ENV_FILE" <<EOF
# 由 deploy/install-watchdog.sh 生成 $(date -Iseconds)
# 重新执行安装脚本即可刷新；手改会被下次安装覆盖。
GUARDIAN_COMPOSE_DIR=$COMPOSE_DIR
GUARDIAN_DATA_ROOT=$DATA_ROOT
GUARDIAN_ALLOWED_TARGETS=$TARGETS
GUARDIAN_DOCKER_BACKEND=compose
GUARDIAN_MAX_AGE_SEC=120
GUARDIAN_CMD_TIMEOUT=120
GUARDIAN_LOG_FILE=/var/log/guardian-watchdog.log
GUARDIAN_KERNEL_GUARD=1
GUARDIAN_KERNEL_CONTAINER=$KERNEL_CONTAINER
GUARDIAN_KERNEL_HEARTBEAT_TIMEOUT=90
GUARDIAN_KERNEL_RESTART_COOLDOWN=300
EOF
chmod 600 "$ENV_FILE"
note "已写入 $ENV_FILE"

# --------------------------------------------------------------------- #
step "5/6 安装可执行文件"
install -m 755 "$HERE/guardian-watchdog.sh" "$BIN_DIR/guardian-watchdog.sh"
install -m 644 "$HERE/guardian-watchdog.py" "$BIN_DIR/guardian-watchdog.py"
note "已安装到 $BIN_DIR"

# --------------------------------------------------------------------- #
step "6/6 注册 systemd 定时器"
install -m 644 "$HERE/$SERVICE_NAME" "$UNIT_DIR/$SERVICE_NAME"
install -m 644 "$HERE/$TIMER_NAME" "$UNIT_DIR/$TIMER_NAME"
systemctl daemon-reload
systemctl enable --now "$TIMER_NAME"
note "已启用 $TIMER_NAME（每 30 秒一次）"

# --------------------------------------------------------------------- #
step "自检"
touch /var/log/guardian-watchdog.log 2>/dev/null || true
if GUARDIAN_COMPOSE_DIR="$COMPOSE_DIR" GUARDIAN_DATA_ROOT="$DATA_ROOT" \
   GUARDIAN_ALLOWED_TARGETS="$TARGETS" GUARDIAN_LOG_FILE=/var/log/guardian-watchdog.log \
   python3 "$BIN_DIR/guardian-watchdog.py" --once; then
  note "单次执行成功"
else
  note "单次执行失败，请查看 /var/log/guardian-watchdog.log"
fi

cat <<EOF

完成。下一步在 WebUI 里确认：

  safety.self_restart_strategy = watchdog
  safety.allow_self_restart     = true      # 否则只有 NapCat 会睡，本体不参与

验证：journalctl -u guardian-watchdog.service -n 20 --no-pager
EOF
