#!/usr/bin/env bash
# 生成不含密钥、本地数据库、行情与回测结果的程序员分享包。

set -e
set -u
set -o pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_OUTPUT="$ROOT_DIR/release/Pronoia-team-share.zip"
OUTPUT_FILE="${1:-${PRONOIA_SHARE_BUNDLE_OUTPUT:-$DEFAULT_OUTPUT}}"
OUTPUT_DIR="$(cd "$(dirname "$OUTPUT_FILE")" 2>/dev/null && pwd || true)"

if [ -z "$OUTPUT_DIR" ]; then
  mkdir -p "$(dirname "$OUTPUT_FILE")"
  OUTPUT_DIR="$(cd "$(dirname "$OUTPUT_FILE")" && pwd)"
fi
OUTPUT_FILE="$OUTPUT_DIR/$(basename "$OUTPUT_FILE")"

if ! command -v rsync >/dev/null 2>&1; then
  printf '需要 rsync 才能生成安全分享包。\n' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf '需要 Python 3 才能安全脱敏并生成分享包。\n' >&2
  exit 1
fi
if [ ! -f "$ROOT_DIR/scripts/sanitize_share_bundle.py" ]; then
  printf '缺少分享包脱敏工具，无法生成安全分享包。\n' >&2
  exit 1
fi

if [ ! -x "$ROOT_DIR/scripts/frontend_source_fingerprint.sh" ]; then
  printf '缺少前端源码指纹工具，无法验证分享构建。\n' >&2
  exit 1
fi
current_frontend_fingerprint="$("$ROOT_DIR/scripts/frontend_source_fingerprint.sh" "$ROOT_DIR")"
built_frontend_fingerprint="$(sed -n '1p' "$ROOT_DIR/frontend/dist/.pronoia-source.sha256" 2>/dev/null || true)"
if [ ! -f "$ROOT_DIR/frontend/dist/index.html" ] || \
   [ "$current_frontend_fingerprint" != "$built_frontend_fingerprint" ]; then
  if ! command -v npm >/dev/null 2>&1; then
    printf '前端源码有更新，但未找到 npm；请安装 Node.js 18+ 后重新生成分享包。\n' >&2
    exit 1
  fi
  printf '检测到前端源码更新，正在生成分享版界面…\n'
  (cd "$ROOT_DIR/frontend" && npm run build)
  current_frontend_fingerprint="$("$ROOT_DIR/scripts/frontend_source_fingerprint.sh" "$ROOT_DIR")"
  printf '%s\n' "$current_frontend_fingerprint" \
    > "$ROOT_DIR/frontend/dist/.pronoia-source.sha256"
fi

TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pronoia-share.XXXXXX")"
trap 'rm -rf "$TEMP_ROOT"' EXIT INT TERM
PACKAGE_NAME='Pronoia-team-share'
STAGE_DIR="$TEMP_ROOT/$PACKAGE_NAME"
mkdir -p "$STAGE_DIR"

rsync -a \
  --exclude '/.git/' \
  --exclude '/.git' \
  --exclude '/.github/' \
  --exclude '/.env' \
  --exclude '/.env.*' \
  --exclude '**/.env' \
  --exclude '**/.env.*' \
  --exclude '**/*.env' \
  --exclude '.*.model-secrets/' \
  --exclude '*.pem' \
  --exclude '*.key' \
  --exclude '*.p12' \
  --exclude '*.pfx' \
  --exclude '/.DS_Store' \
  --exclude '**/.DS_Store' \
  --exclude '/.run/' \
  --exclude '/.pronoia-launcher/' \
  --exclude '/.pronoia-state/' \
  --exclude '/.pytest_cache/' \
  --exclude '/.trae-html-share-packages/' \
  --exclude '/data/' \
  --exclude '/backtesting/' \
  --exclude '/pronoia_run/' \
  --exclude '/work/' \
  --exclude '/archive/' \
  --exclude '/test-report/' \
  --exclude '/tech-report/' \
  --exclude '/htmlcov/' \
  --exclude '/.coverage*' \
  --exclude '/release/' \
  --exclude '/frontend/node_modules' \
  --exclude '/frontend/node_modules/' \
  --exclude '/frontend/build/' \
  --exclude '/frontend/scripts/' \
  --exclude '/backend/.venv/' \
  --exclude '/backend/venv/' \
  --exclude '/backend/fever.db*' \
  --exclude '/backend/tests/_e2e_ckpt/' \
  --exclude '/docs/20260729_design.md' \
  --exclude '/docs/20260816_CODE_WIKI.md' \
  --exclude '/docs/20260817_plan.md' \
  --exclude '/scripts/generate_datasets.py' \
  --exclude '**/__pycache__/' \
  --exclude '**/.pytest_cache/' \
  --exclude '*.pyc' \
  --exclude '*.pyo' \
  --exclude '*.log' \
  --exclude '*.zip' \
  "$ROOT_DIR/" "$STAGE_DIR/"

# Examples are deliberately restored after the broad .env.* exclusion.
cp "$ROOT_DIR/.env.example" "$STAGE_DIR/.env.example"
cp "$ROOT_DIR/.env.share.example" "$STAGE_DIR/.env.share.example"
if [ -f "$STAGE_DIR/README.md" ]; then
  mv "$STAGE_DIR/README.md" "$STAGE_DIR/docs/LEGACY_PROJECT_README.md"
fi
cp "$STAGE_DIR/SHARE_README.md" "$STAGE_DIR/README.md"
cp "$STAGE_DIR/SHARE_README.md" "$STAGE_DIR/00_START_HERE.md"

if find "$STAGE_DIR" -type l -print -quit | grep -q .; then
  printf '安全检查失败：分享包中发现符号链接。\n' >&2
  exit 1
fi

python3 "$ROOT_DIR/scripts/sanitize_share_bundle.py" \
  "$STAGE_DIR" \
  --source-env "$ROOT_DIR/.env" \
  --source-env "$ROOT_DIR/.env.share"

# The provider presets are sanitized only in the staged copy. Rebuild there so
# the compiled frontend cannot retain an address from the sender's normal app.
if ! command -v npm >/dev/null 2>&1 || [ ! -d "$ROOT_DIR/frontend/node_modules" ]; then
  printf '需要当前项目的 npm 与 frontend/node_modules 才能重建脱敏后的前端。\n' >&2
  exit 1
fi
ln -s "$ROOT_DIR/frontend/node_modules" "$STAGE_DIR/frontend/node_modules"
(cd "$STAGE_DIR/frontend" && npm run build)
rm -f "$STAGE_DIR/frontend/node_modules"
"$STAGE_DIR/scripts/frontend_source_fingerprint.sh" "$STAGE_DIR" \
  > "$STAGE_DIR/frontend/dist/.pronoia-source.sha256"

if [ ! -s "$STAGE_DIR/frontend/dist/.pronoia-source.sha256" ]; then
  printf '安全检查失败：分享包缺少已验证的前端构建指纹。\n' >&2
  exit 1
fi

if find "$STAGE_DIR" -type f \( \
  \( -name '.env' -o -name '.env.*' \) \
    ! -name '.env.example' ! -name '.env.share.example' -o \
  -name '*.env' -o \
  -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' -o \
  -name '*.sqlite' -o -name '*.sqlite-*' -o \
  -name '*.sqlite3' -o -name '*.sqlite3-*' -o \
  -name '*.pem' -o -name '*.key' -o -name '*.p12' -o -name '*.pfx' -o \
  -path '*/.*.model-secrets/*' \
\) -print -quit | grep -q .; then
  printf '安全检查失败：分享包中发现运行时密钥或数据库文件。\n' >&2
  exit 1
fi

if find "$STAGE_DIR" -type f -size +95M -print -quit | grep -q .; then
  printf '安全检查失败：分享包中存在超过 95 MiB 的文件。\n' >&2
  exit 1
fi

# If local secret values exist, verify that none was copied. Values are never
# printed, and short placeholders are ignored to avoid noisy false positives.
for source_env in "$ROOT_DIR/.env" "$ROOT_DIR/.env.share"; do
  [ -f "$source_env" ] || continue
  while IFS='=' read -r key value; do
    case "$key" in
      *_KEY|*_TOKEN|*_SECRET|*_PASSWORD|*_URL|*_ENDPOINT)
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"
        if [ "${#value}" -ge 8 ] && grep -RIlF -- "$value" "$STAGE_DIR" >/dev/null 2>&1; then
          printf '安全检查失败：分享包中发现本机凭据内容。\n' >&2
          exit 1
        fi
        ;;
    esac
  done < "$source_env"
done

current_user_name="$(id -un 2>/dev/null || true)"
if [ -n "$current_user_name" ] && \
   grep -RIlF -- "/Users/$current_user_name/" "$STAGE_DIR" >/dev/null 2>&1; then
  printf '安全检查失败：分享包中发现本机用户绝对路径。\n' >&2
  exit 1
fi

if grep -ERIl --exclude='build_share_bundle.sh' -- "/Users/[^/[:space:]<>]+/|/home/[^/[:space:]<>]+/|[A-Za-z]:\\\\Users\\\\[^\\\\[:space:]<>]+\\\\" \
  "$STAGE_DIR" >/dev/null 2>&1; then
  printf '安全检查失败：分享包中发现用户目录绝对路径。\n' >&2
  exit 1
fi

if find "$STAGE_DIR" -type l -print -quit | grep -q .; then
  printf '安全检查失败：分享包构建后发现符号链接。\n' >&2
  exit 1
fi

TEMP_ZIP="$TEMP_ROOT/${PACKAGE_NAME}.zip"
# Python sets the ZIP UTF-8 filename flag, which keeps the Chinese launchers
# readable in Finder, Windows Explorer and cross-platform unzip tools.
python3 - "$TEMP_ROOT" "$PACKAGE_NAME" "$TEMP_ZIP" <<'PY'
from pathlib import Path
import sys
import zipfile

root = Path(sys.argv[1])
package = sys.argv[2]
output = Path(sys.argv[3])
with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted((root / package).rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(root).as_posix())
PY
mv "$TEMP_ZIP" "$OUTPUT_FILE"
chmod 600 "$OUTPUT_FILE"

if command -v shasum >/dev/null 2>&1; then
  (
    cd "$OUTPUT_DIR"
    shasum -a 256 "$(basename "$OUTPUT_FILE")" > "$(basename "$OUTPUT_FILE").sha256"
  )
elif command -v openssl >/dev/null 2>&1; then
  (
    cd "$OUTPUT_DIR"
    openssl dgst -sha256 "$(basename "$OUTPUT_FILE")" > "$(basename "$OUTPUT_FILE").sha256"
  )
fi

printf '分享包已生成：%s\n' "$OUTPUT_FILE"
printf '内容：源码、启动器、Docker 配置与文档；不含发送者 API 地址、密钥、数据库、行情和历史回测结果。\n'
