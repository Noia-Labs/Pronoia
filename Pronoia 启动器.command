#!/usr/bin/env bash
# 双击：首次填写 API，之后直接启动 Pronoia。
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/configure_api_and_launch.sh"
