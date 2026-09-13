#!/usr/bin/env bash
# 双击：重新填写模型 API，然后启动 Pronoia。
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/configure_api_and_launch.sh" --reconfigure
