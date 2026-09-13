#!/usr/bin/env bash
# Pronoia macOS 一键启动：构建检查 + 单端口 FastAPI + 默认浏览器。

set -u
set -o pipefail
umask 077

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="${PRONOIA_RUN_DIR:-$ROOT_DIR/.run}"
LOG_FILE="$RUN_DIR/pronoia.log"
ERROR_LOG_FILE="$RUN_DIR/pronoia-error.log"
PREVIOUS_LOG_FILE="$RUN_DIR/pronoia.previous.log"
PREVIOUS_ERROR_LOG_FILE="$RUN_DIR/pronoia-error.previous.log"
PID_FILE="$RUN_DIR/backend.pid"
LOCK_DIR="$RUN_DIR/launcher.lock"
PYTHON_DEPS_STAMP="$RUN_DIR/backend-requirements.sha256"
FRONTEND_DEPS_STAMP="$RUN_DIR/frontend-lock.sha256"
APP_PORT="${PRONOIA_PORT:-8000}"
BIND_HOST="${PRONOIA_BIND_HOST:-127.0.0.1}"
APP_HOST="${PRONOIA_APP_HOST:-127.0.0.1}"
APP_SCHEME="${PRONOIA_APP_SCHEME:-http}"
APP_URL="${APP_SCHEME}://${APP_HOST}:${APP_PORT}"
LAUNCH_LABEL="${PRONOIA_LAUNCH_LABEL:-com.pronoia.desktop}"
WORKSPACE_TITLE="${PRONOIA_WORKSPACE_TITLE:-Pronoia · 本地研究工作台}"
STOP_HINT="${PRONOIA_STOP_HINT:-双击桌面的「Pronoia 停止器.command」}"

mkdir -p "$RUN_DIR"

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

info() { printf "%b\n" "${AMBER}›${RESET} $*"; }
ok() { printf "%b\n" "${GREEN}✓${RESET} $*"; }
fail() { printf "%b\n" "${RED}✗ $*${RESET}" >&2; }

browser_entry_url() {
  local build_id=''
  local dist_index="$ROOT_DIR/frontend/dist/index.html"
  if [ -f "$dist_index" ]; then
    build_id="$(cksum < "$dist_index" 2>/dev/null | awk '{print $1}')"
  fi
  if [ -n "$build_id" ]; then
    printf '%s/?ui=%s' "$APP_URL" "$build_id"
  else
    printf '%s' "$APP_URL"
  fi
}

release_lock() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  info "另一个启动器正在工作，等待服务就绪…"
  count=0
  while [ "$count" -lt 30 ]; do
    response="$(curl -fsS --max-time 2 "$APP_URL/api/health" 2>/dev/null || true)"
    case "$response" in
      *'"ok":true'*)
        ok "Pronoia 已运行 · $APP_URL"
        if [ "${PRONOIA_NO_OPEN:-0}" != "1" ]; then
          open "$(browser_entry_url)" 2>/dev/null || true
        fi
        exit 0
        ;;
    esac
    sleep 0.5
    count=$((count + 1))
  done
  fail "启动任务仍未结束。请稍后再双击一次，或查看 $LOG_FILE。"
  exit 1
fi
trap release_lock EXIT INT TERM

backend_ready() {
  local response
  response="$(curl -fsS --max-time 2 "$APP_URL/api/health" 2>/dev/null || true)"
  case "$response" in
    *'"ok":true'*) return 0 ;;
    *) return 1 ;;
  esac
}

port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

wait_for_backend() {
  local count=0
  while [ "$count" -lt 120 ]; do
    if backend_ready; then
      return 0
    fi
    sleep 0.5
    count=$((count + 1))
  done
  return 1
}

file_digest() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$1" | awk '{print $NF}'
  else
    return 1
  fi
}

update_source_if_possible() {
  if [ "${PRONOIA_AUTO_UPDATE:-1}" != "1" ]; then
    info "已按设置跳过远程版本检查"
    return 0
  fi
  if [ ! -d "$ROOT_DIR/.git" ]; then
    info "本地源码模式：未连接 Git 远程仓库，继续检查本地更新"
    return 0
  fi
  if ! command -v git >/dev/null 2>&1; then
    info "未找到 Git，跳过远程版本检查"
    return 0
  fi
  if ! git -C "$ROOT_DIR" remote get-url origin >/dev/null 2>&1; then
    info "没有 origin 远程仓库，跳过远程版本检查"
    return 0
  fi
  if [ -n "$(git -C "$ROOT_DIR" status --porcelain 2>/dev/null)" ]; then
    info "检测到本地修改，为避免覆盖代码，跳过远程更新"
    return 0
  fi

  info "检查远程版本更新…"
  if git -C "$ROOT_DIR" pull --ff-only --quiet; then
    ok "远程版本检查完成"
  else
    info "远程更新暂不可用，继续启动现有本地版本"
  fi
}

choose_python() {
  local candidate=''
  if [ -n "${PRONOIA_PYTHON:-}" ] && [ -x "$PRONOIA_PYTHON" ]; then
    candidate="$PRONOIA_PYTHON"
  elif [ -x "$ROOT_DIR/backend/.venv/bin/python" ]; then
    candidate="$ROOT_DIR/backend/.venv/bin/python"
  elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    candidate="$ROOT_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    candidate="$(command -v python3)"
  fi
  [ -n "$candidate" ] || return 1
  if ! "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    fail "Pronoia 需要 Python 3.10 或更高版本。当前解释器不符合要求：$candidate"
    return 1
  fi
  printf "%s" "$candidate"
}

ensure_backend_dependencies() {
  local selected_python="$1"
  local requirements_hash
  local recorded_hash=''
  local dependencies_ok=0
  requirements_hash="$(file_digest "$ROOT_DIR/backend/requirements.txt")" || {
    fail "无法计算 Python 依赖清单摘要。"
    return 1
  }
  if [ -f "$PYTHON_DEPS_STAMP" ]; then
    recorded_hash="$(sed -n '1p' "$PYTHON_DEPS_STAMP" 2>/dev/null || true)"
  fi
  if "$selected_python" -c 'import fastapi, uvicorn, openai, akshare, pandas, requests, dotenv, yfinance' >/dev/null 2>&1; then
    dependencies_ok=1
  fi
  if [ "$dependencies_ok" -eq 1 ] && [ "$recorded_hash" = "$requirements_hash" ]; then
    printf "%s\n" "$requirements_hash" > "$PYTHON_DEPS_STAMP"
    printf "%s" "$selected_python"
    return 0
  fi

  info "检测到 Python 依赖更新，正在同步独立运行环境…" >&2
  if [ ! -x "$ROOT_DIR/backend/.venv/bin/python" ]; then
    "$selected_python" -m venv "$ROOT_DIR/backend/.venv" || return 1
  fi
  "$ROOT_DIR/backend/.venv/bin/python" -m pip install -r "$ROOT_DIR/backend/requirements.txt" >&2 || return 1
  printf "%s\n" "$requirements_hash" > "$PYTHON_DEPS_STAMP"
  printf "%s" "$ROOT_DIR/backend/.venv/bin/python"
}

frontend_needs_build() {
  local dist_index="$ROOT_DIR/frontend/dist/index.html"
  local fingerprint_file="$ROOT_DIR/frontend/dist/.pronoia-source.sha256"
  local expected_fingerprint current_fingerprint
  if [ ! -f "$dist_index" ]; then
    return 0
  fi
  if [ -f "$fingerprint_file" ] && [ -x "$SCRIPT_DIR/frontend_source_fingerprint.sh" ]; then
    expected_fingerprint="$(sed -n '1p' "$fingerprint_file" 2>/dev/null || true)"
    current_fingerprint="$("$SCRIPT_DIR/frontend_source_fingerprint.sh" "$ROOT_DIR" 2>/dev/null || true)"
    if [ -n "$expected_fingerprint" ] && [ "$expected_fingerprint" = "$current_fingerprint" ]; then
      return 1
    fi
    return 0
  fi
  if [ "$ROOT_DIR/frontend/package.json" -nt "$dist_index" ] || \
     [ "$ROOT_DIR/frontend/package-lock.json" -nt "$dist_index" ] || \
     [ "$ROOT_DIR/frontend/vite.config.ts" -nt "$dist_index" ]; then
    return 0
  fi
  if find "$ROOT_DIR/frontend/src" -type f -newer "$dist_index" -print -quit 2>/dev/null | grep -q .; then
    return 0
  fi
  return 1
}

record_frontend_fingerprint() {
  local fingerprint
  [ -x "$SCRIPT_DIR/frontend_source_fingerprint.sh" ] || return 0
  fingerprint="$("$SCRIPT_DIR/frontend_source_fingerprint.sh" "$ROOT_DIR")" || return 1
  printf '%s\n' "$fingerprint" > "$ROOT_DIR/frontend/dist/.pronoia-source.sha256"
}

ensure_frontend_build() {
  local lock_hash
  local recorded_hash=''
  local install_required=0
  # The programmer ZIP carries a freshly compiled UI.  It can therefore start
  # without Node.js; Node/npm are only needed after frontend source changes.
  if ! frontend_needs_build; then
    if [ ! -f "$ROOT_DIR/frontend/dist/.pronoia-source.sha256" ]; then
      record_frontend_fingerprint || return 1
    fi
    ok "前端构建已是最新版本"
    return 0
  fi
  lock_hash="$(file_digest "$ROOT_DIR/frontend/package-lock.json")" || {
    fail "无法计算前端依赖锁摘要。"
    return 1
  }
  if [ -f "$FRONTEND_DEPS_STAMP" ]; then
    recorded_hash="$(sed -n '1p' "$FRONTEND_DEPS_STAMP" 2>/dev/null || true)"
  fi
  if [ ! -f "$ROOT_DIR/frontend/node_modules/vite/bin/vite.js" ]; then
    install_required=1
  elif [ -n "$recorded_hash" ] && [ "$recorded_hash" != "$lock_hash" ]; then
    install_required=1
  fi
  if [ "$install_required" -eq 1 ]; then
    if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
      fail "前端依赖需要更新，但未找到 Node.js/npm。请先安装 Node.js 18 或更高版本。"
      return 1
    fi
    info "检测到前端依赖更新，正在同步…"
    (cd "$ROOT_DIR/frontend" && npm install --no-audit --no-fund) || return 1
  fi
  printf "%s\n" "$lock_hash" > "$FRONTEND_DEPS_STAMP"

  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    fail "前端需要重新构建，但未找到 Node.js/npm。请先安装 Node.js 18 或更高版本。"
    return 1
  fi
  if [ ! -f "$ROOT_DIR/frontend/node_modules/vite/bin/vite.js" ]; then
    info "首次运行：正在安装前端依赖…"
    (cd "$ROOT_DIR/frontend" && npm install --no-audit --no-fund) || return 1
  fi
  info "检测到界面更新，正在生成最新前端…"
  (cd "$ROOT_DIR/frontend" && npm run build) || return 1
  record_frontend_fingerprint || return 1
  ok "前端构建完成"
}

printf "\n%b\n" "${BOLD}${WORKSPACE_TITLE}${RESET}"
printf "%s\n\n" "────────────────────────────────"

if [ ! -f "$ROOT_DIR/.env" ]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  info "已创建基础配置；可在网页的模型连接中填写 API Key 并设为默认模型。"
fi

update_source_if_possible

if ! ensure_frontend_build; then
  fail "前端构建失败，Pronoia 未启动。"
  exit 1
fi

if backend_ready; then
  ok "Pronoia 已运行 · $APP_URL"
else
  if port_in_use "$APP_PORT"; then
    fail "端口 $APP_PORT 已被其他程序占用，未启动 Pronoia。"
    printf "可检查：lsof -nP -iTCP:%s -sTCP:LISTEN\n" "$APP_PORT"
    exit 1
  fi

  PYTHON_BIN="$(choose_python)" || {
    fail "未找到可用的 Python 3。"
    exit 1
  }
  PYTHON_BIN="$(ensure_backend_dependencies "$PYTHON_BIN")" || {
    fail "后端依赖安装失败。"
    exit 1
  }

  info "启动 Pronoia 服务…"
  LAUNCHD_STARTED=0
  BACKEND_PID=''
  if [ "${PRONOIA_DISABLE_LAUNCHCTL:-0}" != "1" ] && \
     [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
    launchctl remove "$LAUNCH_LABEL" >/dev/null 2>&1 || true
    unlink "$PREVIOUS_LOG_FILE" 2>/dev/null || true
    unlink "$PREVIOUS_ERROR_LOG_FILE" 2>/dev/null || true
    if [ -f "$LOG_FILE" ]; then
      mv "$LOG_FILE" "$PREVIOUS_LOG_FILE"
    fi
    if [ -f "$ERROR_LOG_FILE" ]; then
      mv "$ERROR_LOG_FILE" "$PREVIOUS_ERROR_LOG_FILE"
    fi
    backend_command="cd \"$ROOT_DIR/backend\" && exec \"$PYTHON_BIN\" -m uvicorn app.main:app --host \"$BIND_HOST\" --port \"$APP_PORT\" --workers 1"
    if ! launchctl submit -l "$LAUNCH_LABEL" -o "$LOG_FILE" -e "$ERROR_LOG_FILE" -- /bin/bash -c "$backend_command"; then
      fail "无法向 macOS 后台服务管理器提交 Pronoia。"
      exit 1
    fi
    LAUNCHD_STARTED=1
  else
    printf "\n[%s] launcher start\n" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
    (
      cd "$ROOT_DIR/backend" || exit 1
      exec nohup "$PYTHON_BIN" -m uvicorn app.main:app --host "$BIND_HOST" --port "$APP_PORT" --workers 1
    ) >> "$LOG_FILE" 2>&1 &
    BACKEND_PID=$!
  fi

  if ! wait_for_backend; then
    fail "服务未能就绪。最近日志："
    tail -n 28 "$LOG_FILE" 2>/dev/null || true
    tail -n 28 "$ERROR_LOG_FILE" 2>/dev/null || true
    if [ "$LAUNCHD_STARTED" -eq 1 ]; then
      launchctl remove "$LAUNCH_LABEL" >/dev/null 2>&1 || true
    elif [ -n "$BACKEND_PID" ]; then
      kill "$BACKEND_PID" 2>/dev/null || true
    fi
    exit 1
  fi
  BACKEND_PID="$(lsof -t -iTCP:"$APP_PORT" -sTCP:LISTEN 2>/dev/null | sed -n '1p')"
  if [ -n "$BACKEND_PID" ]; then
    printf "%s\n" "$BACKEND_PID" > "$PID_FILE"
  fi
  ok "Pronoia 已就绪 · $APP_URL"
fi

health_response="$(curl -fsS --max-time 2 "$APP_URL/api/health" 2>/dev/null || true)"
case "$health_response" in
  *'"llm":"missing_api_key"'*)
    info "可在网页的模型连接中填写 API Key 并设为默认模型。"
    ;;
esac

printf "\n%b\n" "${GREEN}${BOLD}启动完成${RESET}"
printf "回测中心和 Arena 已包含在当前工作台中。\n"
printf "地址：%s\n" "$APP_URL"
printf "日志：%s\n" "$LOG_FILE"
printf "停止：%s\n\n" "$STOP_HINT"

if [ "${PRONOIA_NO_OPEN:-0}" = "1" ]; then
  :
elif command -v open >/dev/null 2>&1; then
  open "$(browser_entry_url)"
else
  printf "请在浏览器打开：%s\n" "$(browser_entry_url)"
fi

exit 0
