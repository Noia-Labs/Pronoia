#!/usr/bin/env bash
# 一键开启可信局域网团队分享。
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/launch_pronoia_share.sh" "$@"
