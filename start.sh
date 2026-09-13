#!/usr/bin/env bash
# Pronoia 日常启动入口。也可双击桌面的「Pronoia 启动器.command」。
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/launch_pronoia.sh" "$@"
