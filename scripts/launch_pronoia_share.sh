#!/usr/bin/env bash
# Pronoia 团队分享模式：可信局域网访问 + 全站账号密码保护。

set -e
set -u
set -o pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SHARE_ENV_FILE="${PRONOIA_SHARE_ENV_FILE:-$ROOT_DIR/.env.share}"

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

generate_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 16
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(16))'
  else
    return 1
  fi
}

create_share_env() {
  local password temp_file
  password="$(generate_password)" || {
    fail "无法生成安全密码；请安装 openssl 或 Python 3。"
    return 1
  }
  temp_file="${SHARE_ENV_FILE}.tmp.$$"
  {
    printf '%s\n' '# Pronoia 团队分享凭据（自动生成，请勿提交或转发到公共群组）'
    printf '%s\n' 'PRONOIA_SHARE_USER=pronoia'
    printf 'PRONOIA_SHARE_PASSWORD=%s\n' "$password"
    printf 'PRONOIA_SHARE_PORT=%s\n' "${PRONOIA_SHARE_PORT:-${PRONOIA_HOST_PORT:-8000}}"
  } > "$temp_file"
  chmod 600 "$temp_file"
  mv "$temp_file" "$SHARE_ENV_FILE"
  ok "已生成团队分享账号（保存在 .env.share，不会写入运行日志）"
}

detect_lan_ip() {
  local interface candidate
  if command -v route >/dev/null 2>&1 && command -v ipconfig >/dev/null 2>&1; then
    interface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
    if [ -n "$interface" ]; then
      candidate="$(ipconfig getifaddr "$interface" 2>/dev/null || true)"
      if [ -n "$candidate" ]; then
        printf '%s' "$candidate"
        return 0
      fi
    fi
    for interface in en0 en1; do
      candidate="$(ipconfig getifaddr "$interface" 2>/dev/null || true)"
      if [ -n "$candidate" ]; then
        printf '%s' "$candidate"
        return 0
      fi
    done
  fi
  if command -v hostname >/dev/null 2>&1; then
    candidate="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    if [ -n "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  fi
  return 1
}

printf "\n%b\n" "${BOLD}Pronoia · 团队分享模式${RESET}"
printf "%s\n\n" "────────────────────────────────"

if [ "${PRONOIA_SHARE_SKIP_API_SETUP:-0}" != "1" ]; then
  "$SCRIPT_DIR/configure_api_and_launch.sh" --setup-only
fi

if [ ! -f "$SHARE_ENV_FILE" ]; then
  create_share_env
fi
chmod 600 "$SHARE_ENV_FILE" 2>/dev/null || true

# 该文件由本机所有者维护；自动生成的值只含 shell-safe 字符。
set -a
# shellcheck disable=SC1090
. "$SHARE_ENV_FILE"
set +a

if [ -z "${PRONOIA_SHARE_USER:-}" ] || [ -z "${PRONOIA_SHARE_PASSWORD:-}" ]; then
  fail ".env.share 中必须同时填写 PRONOIA_SHARE_USER 和 PRONOIA_SHARE_PASSWORD。"
  printf "可删除该文件后重新运行，让启动器自动生成安全凭据。\n"
  exit 1
fi
case "$PRONOIA_SHARE_USER" in
  *:*)
    fail "分享用户名不能包含冒号。"
    exit 1
    ;;
esac
if [ "${#PRONOIA_SHARE_PASSWORD}" -lt 12 ]; then
  fail "分享密码至少需要 12 个字符。"
  exit 1
fi

SHARE_PORT="${PRONOIA_SHARE_PORT:-${PRONOIA_HOST_PORT:-8000}}"
case "$SHARE_PORT" in
  ''|*[!0-9]*)
    fail "分享端口必须是数字。"
    exit 1
    ;;
esac
if [ "$SHARE_PORT" -lt 1 ] || [ "$SHARE_PORT" -gt 65535 ]; then
  fail "分享端口必须在 1–65535 之间。"
  exit 1
fi

info "正在切换本机服务到团队分享模式…"
PRONOIA_PORT="$SHARE_PORT" \
PRONOIA_FRONTEND_PORT='' \
  "$ROOT_DIR/scripts/stop_pronoia.sh" >/dev/null 2>&1 || true

export PRONOIA_SHARE_MODE=1
export PRONOIA_BIND_HOST=0.0.0.0
export PRONOIA_APP_HOST=127.0.0.1
export PRONOIA_PORT="$SHARE_PORT"
export PRONOIA_FRONTEND_PORT=''
export PRONOIA_DISABLE_LAUNCHCTL=1
export PRONOIA_NO_OPEN=1
export PRONOIA_WORKSPACE_TITLE='Pronoia · 团队分享工作台'
export PRONOIA_STOP_HINT='运行项目中的 share-stop.sh'

"$ROOT_DIR/scripts/launch_pronoia.sh"

LOCAL_URL="http://127.0.0.1:${SHARE_PORT}"
LAN_IP="$(detect_lan_ip || true)"

printf "\n%b\n" "${GREEN}${BOLD}团队分享已开启${RESET}"
printf "本机地址：%s\n" "$LOCAL_URL"
if [ -n "$LAN_IP" ]; then
  printf "同事地址：http://%s:%s\n" "$LAN_IP" "$SHARE_PORT"
else
  printf "同事地址：http://<这台 Mac 的局域网 IP>:%s\n" "$SHARE_PORT"
fi
printf "用户名：%s\n" "$PRONOIA_SHARE_USER"
printf "密码：保存在 %s（不会输出到日志）\n" "$SHARE_ENV_FILE"
printf "停止分享：%s/share-stop.sh\n" "$ROOT_DIR"
printf "恢复本机模式：停止分享后运行 %s/start.sh\n" "$ROOT_DIR"
printf "\n注意：这是可信局域网内测模式；通过公网分享时必须使用 HTTPS/访问网关。\n\n"

if command -v open >/dev/null 2>&1 && [ "${PRONOIA_SHARE_NO_OPEN:-0}" != "1" ]; then
  open "$LOCAL_URL"
fi
