#!/usr/bin/env bash
# Pronoia 本机启动脚本（自定义端口版，避开常用的 8000/5173）
#
# 与 ./start.sh（前台运行、固定 8000/5173）的区别：
#   - 后端/前端端口默认 27531 / 27532，可用环境变量 FEVER_BACKEND_PORT / FEVER_FRONTEND_PORT 覆盖
#   - setsid 后台运行（脱离启动会话的进程组，终端关闭/会话超时不连带杀服务），日志写入 .run/（沿用 scripts/dev.sh 的约定）
#   - 以「端口是否有监听」为运行状态的唯一真相；pid 文件只作记录展示（setsid 会 fork，启动时的 $! 不可靠）
#   - 端口被未知进程占用时直接报错退出，绝不误杀共享机器上的其他服务
#
# 用法:
#   ./start_local.sh            # 启动（已在运行则跳过）
#   ./start_local.sh stop       # 停止（按端口找进程组）
#   ./start_local.sh restart    # 重启
#   ./start_local.sh status     # 查看运行状态
#   ./start_local.sh logs backend|frontend   # 跟踪日志
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$ROOT/backend/.venv/bin/python"
LOG_DIR="$ROOT/.run"
BACKEND_PORT="${FEVER_BACKEND_PORT:-27531}"
FRONTEND_PORT="${FEVER_FRONTEND_PORT:-27532}"
BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
FRONTEND_URL="http://127.0.0.1:$FRONTEND_PORT"

mkdir -p "$LOG_DIR"

# ---- 基础工具 -------------------------------------------------------------
# 端口是否有人监听；port_pids 输出监听进程 pid（可能多个，取第一个）
port_busy() { lsof -t -i:"$1" >/dev/null 2>&1; }
port_pids() { lsof -t -i:"$1" 2>/dev/null | head -1; }

# 等端口释放（最多 n 秒），避免 restart 时撞上残留
wait_port_free() {
  local port="$1" n="$2" i=0
  while [ $i -lt "$n" ]; do
    port_busy "$port" || return 0
    sleep 1; i=$((i+1))
  done
}

check_env() {
  if [ ! -f "$ROOT/.env" ]; then
    echo "[start_local] 缺少 $ROOT/.env，请先 cp .env.example .env 并填入 ARK_API_KEY"
    exit 1
  fi
  if [ ! -x "$VENV_PY" ]; then
    echo "[start_local] 缺少后端虚拟环境，请先: uv venv backend/.venv && uv pip install -r backend/requirements.txt --python backend/.venv/bin/python"
    exit 1
  fi
  if [ ! -d "$ROOT/frontend/node_modules" ]; then
    echo "[start_local] 缺少前端依赖，请先: cd frontend && npm install"
    exit 1
  fi
}

# ---- 子命令 ---------------------------------------------------------------
do_start() {
  # 已在运行的判定以端口监听为准
  if port_busy "$BACKEND_PORT" && port_busy "$FRONTEND_PORT"; then
    echo "[start_local] 已在运行（backend :$BACKEND_PORT · frontend :$FRONTEND_PORT），跳过启动"
    return 0
  fi
  # 只有一半在跑属于异常状态，正确地失败并提示
  for pair in "backend:$BACKEND_PORT" "frontend:$FRONTEND_PORT"; do
    local name="${pair%%:*}" port="${pair##*:}"
    if port_busy "$port"; then
      echo "[start_local] 错误: 端口 $port 已被进程 $(port_pids "$port") 占用。"
      echo "  若是本脚本启动的残留: 先 $0 stop；若是其他服务: 换端口 FEVER_${name^^}_PORT=<port> $0"
      exit 1
    fi
  done

  echo "[start_local] 启动后端 uvicorn :$BACKEND_PORT"
  (cd "$ROOT/backend" && setsid "$VENV_PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" \
    > "$LOG_DIR/backend.log" 2>&1 < /dev/null &)

  echo "[start_local] 启动前端 vite :$FRONTEND_PORT"
  (cd "$ROOT/frontend" && FEVER_BACKEND_PORT="$BACKEND_PORT" FEVER_FRONTEND_PORT="$FRONTEND_PORT" \
    setsid npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort \
    > "$LOG_DIR/frontend.log" 2>&1 < /dev/null &)

  # 等待后端 health 就绪（最多 30s），逐秒探测
  local i=0
  echo -n "[start_local] 等待后端就绪"
  while [ $i -lt 30 ]; do
    if curl -sf -m 2 "$BACKEND_URL/api/health" >/dev/null 2>&1; then echo " ok"; break; fi
    echo -n "."; sleep 1; i=$((i+1))
  done
  if [ "$i" -ge 30 ]; then
    echo " 超时（查看 $LOG_DIR/backend.log）"
    exit 1
  fi

  # 探测真实监听 pid 回写 pid 文件（仅记录用途，setsid fork 导致启动时的 $! 不可靠）
  port_pids "$BACKEND_PORT"  > "$LOG_DIR/backend.pid"  2>/dev/null || true
  port_pids "$FRONTEND_PORT" > "$LOG_DIR/frontend.pid" 2>/dev/null || true

  echo ""
  echo "后端:  $BACKEND_URL   (pid $(cat "$LOG_DIR/backend.pid"))   日志: $LOG_DIR/backend.log"
  echo "前端:  $FRONTEND_URL  (pid $(cat "$LOG_DIR/frontend.pid"))  日志: $LOG_DIR/frontend.log"
  echo "停止:  $0 stop"
}

do_stop() {
  local stopped=0
  for pair in "backend:$BACKEND_PORT" "frontend:$FRONTEND_PORT"; do
    local name="${pair%%:*}" port="${pair##*:}" pid pgid
    pid="$(port_pids "$port")"
    if [ -z "$pid" ]; then
      echo "[start_local] $name :$port 未运行"
      continue
    fi
    # 按进程组发信号，一并终止 npm→sh→vite 的整条派生链
    pgid="$(ps -o pgid= -p "$pid" | tr -d ' ')"
    kill -- -"$pgid" 2>/dev/null || kill "$pid" 2>/dev/null || true
    wait_port_free "$port" 10
    echo "[start_local] 已停止 $name :$port (pid=$pid)"
    stopped=1
  done
  rm -f "$LOG_DIR/backend.pid" "$LOG_DIR/frontend.pid"
}

do_status() {
  local pid
  pid="$(port_pids "$BACKEND_PORT")"
  if [ -n "$pid" ]; then
    echo "后端  :$BACKEND_PORT  运行中 (pid=$pid)  health: $(curl -sf -m 2 "$BACKEND_URL/api/health" || echo '不可达')"
  else
    echo "后端  :$BACKEND_PORT  未运行"
  fi
  pid="$(port_pids "$FRONTEND_PORT")"
  if [ -n "$pid" ]; then
    echo "前端  :$FRONTEND_PORT  运行中 (pid=$pid)"
  else
    echo "前端  :$FRONTEND_PORT  未运行"
  fi
}

do_logs() {
  local name="${1:-backend}"
  case "$name" in
    backend) tail -f "$LOG_DIR/backend.log" ;;
    frontend) tail -f "$LOG_DIR/frontend.log" ;;
    *) echo "用法: $0 logs backend|frontend"; exit 1 ;;
  esac
}

# ---- 入口 -------------------------------------------------------------
check_env
case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  status)  do_status ;;
  logs)    shift; do_logs "${1:-backend}" ;;
  *) echo "用法: $0 [start|stop|restart|status|logs backend|frontend]"; exit 1 ;;
esac
