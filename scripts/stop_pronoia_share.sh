#!/usr/bin/env bash
# 停止团队分享实例；不会删除数据库、行情或回测结果。

set -e
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SHARE_ENV_FILE="${PRONOIA_SHARE_ENV_FILE:-$ROOT_DIR/.env.share}"
SHARE_PORT=8000

if [ -f "$SHARE_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$SHARE_ENV_FILE"
  set +a
  SHARE_PORT="${PRONOIA_SHARE_PORT:-${PRONOIA_HOST_PORT:-8000}}"
fi

PRONOIA_PORT="$SHARE_PORT" \
PRONOIA_FRONTEND_PORT='' \
  "$ROOT_DIR/scripts/stop_pronoia.sh"

printf "团队分享已停止。运行 %s/start.sh 可恢复仅本机访问。\n" "$ROOT_DIR"
