#!/bin/bash
# run_team_forward.sh — 跑 Team 全管线生成 research_cache + trajectory
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

set -a
source "$ROOT/.env"
set +a

export PRONOIA_BT_FAST=1   # 3 专家白名单 + 跳过 hypothesis/verify
export PYTHONPATH="$ROOT/backend:$ROOT:${PYTHONPATH:-}"

PY="${PYTHON:-python3}"

cd "$ROOT/backend"

exec "$PY" "$ROOT/backtesting/rlvr/scripts/team_research_batch.py" \
    --events "$ROOT/pronoia_run/forward_test/events_forward.jsonl" \
    --out "$ROOT/pronoia_run/forward_test/research_cache_team.jsonl" \
    --ckpt-dir "$ROOT/pronoia_run/team_traj_forward" \
    --concurrency 8 \
    --timeout 1800 \
    "$@"
