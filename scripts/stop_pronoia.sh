#!/usr/bin/env bash
# 安全停止 Pronoia：只终止与 uvicorn/vite 特征匹配的本地服务。

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="${PRONOIA_RUN_DIR:-$ROOT_DIR/.run}"
LAUNCH_LABEL="${PRONOIA_LAUNCH_LABEL:-com.pronoia.desktop}"
BACKEND_PORT="${PRONOIA_PORT:-8000}"
FRONTEND_PORT="${PRONOIA_FRONTEND_PORT-5173}"

if [ -t 1 ]; then
  GREEN='\033[0;32m'
  AMBER='\033[0;33m'
  RED='\033[0;31m'
  BOLD='\033[1m'
  RESET='\033[0m'
else
  GREEN=''
  AMBER=''
  RED=''
  BOLD=''
  RESET=''
fi

ok() { printf "%b\n" "${GREEN}✓${RESET} $*"; }
warn() { printf "%b\n" "${AMBER}›${RESET} $*"; }
fail() { printf "%b\n" "${RED}✗ $*${RESET}" >&2; }

PID_LIST=''

append_pid() {
  local candidate="$1"
  case "$candidate" in
    ''|*[!0-9]*) return ;;
  esac
  case " $PID_LIST " in
    *" $candidate "*) ;;
    *) PID_LIST="$PID_LIST $candidate" ;;
  esac
}

for pid_file in "$RUN_DIR/backend.pid" "$RUN_DIR/frontend.pid"; do
  if [ -f "$pid_file" ]; then
    append_pid "$(sed -n '1p' "$pid_file" 2>/dev/null || true)"
  fi
done

PORT_LIST="$BACKEND_PORT"
if [ -n "$FRONTEND_PORT" ] && [ "$FRONTEND_PORT" != "$BACKEND_PORT" ]; then
  PORT_LIST="$PORT_LIST $FRONTEND_PORT"
fi

for port in $PORT_LIST; do
  for listener_pid in $(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
    append_pid "$listener_pid"
  done
done

printf "\n%b\n" "${BOLD}Pronoia · 停止本地服务${RESET}"
printf "%s\n\n" "────────────────────────────────"

STOPPED=0
if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
  if launchctl print "gui/$(id -u)/$LAUNCH_LABEL" >/dev/null 2>&1; then
    warn "正在停止 macOS 后台服务…"
    launchctl remove "$LAUNCH_LABEL" >/dev/null 2>&1 || true
    STOPPED=1
    sleep 0.5
  fi
fi

for pid in $PID_LIST; do
  if ! kill -0 "$pid" 2>/dev/null; then
    continue
  fi
  command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$command_line" in
    *uvicorn*app.main:app*|*vite/bin/vite.js*|*node_modules/.bin/vite*)
      warn "正在停止 PID ${pid}…"
      kill "$pid" 2>/dev/null || true
      STOPPED=1
      ;;
    *)
      warn "跳过 PID ${pid}：进程特征不属于 Pronoia。"
      ;;
  esac
done

if [ "$STOPPED" -eq 1 ]; then
  count=0
  while [ "$count" -lt 20 ]; do
    active=0
    for pid in $PID_LIST; do
      if kill -0 "$pid" 2>/dev/null; then
        active=1
      fi
    done
    [ "$active" -eq 0 ] && break
    sleep 0.25
    count=$((count + 1))
  done
  ok "停止信号已发送。"
else
  ok "没有需要停止的 Pronoia 服务。"
fi

unlink "$RUN_DIR/backend.pid" 2>/dev/null || true
unlink "$RUN_DIR/frontend.pid" 2>/dev/null || true

for port in $PORT_LIST; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "端口 $port 仍有进程监听；为避免误杀，启动器未强制终止它。"
  fi
done

printf "\n可再次双击「Pronoia 启动器.command」启动。\n\n"
