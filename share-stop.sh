#!/usr/bin/env bash
# 一键停止团队分享；数据不会被删除。
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/stop_pronoia_share.sh" "$@"
