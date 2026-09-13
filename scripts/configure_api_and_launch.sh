#!/usr/bin/env bash
# Pronoia 首次运行向导：安全配置模型 API，然后启动本机工作台。

set -u
set -o pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${PRONOIA_SETUP_ENV_FILE:-$ROOT_DIR/.env}"
FORCE_SETUP="${PRONOIA_FORCE_API_SETUP:-0}"
SETUP_ONLY="${PRONOIA_SETUP_ONLY:-0}"
TEMP_FILE=''

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

cleanup() {
  if [ -n "$TEMP_FILE" ] && [ -f "$TEMP_FILE" ]; then
    rm -f "$TEMP_FILE"
  fi
}
trap cleanup EXIT INT TERM

usage() {
  printf '%s\n' '用法：'
  printf '%s\n' '  scripts/configure_api_and_launch.sh [--reconfigure] [--setup-only]'
  printf '%s\n' ''
  printf '%s\n' '选项：'
  printf '%s\n' '  --reconfigure  忽略现有配置并重新填写 API'
  printf '%s\n' '  --setup-only   只保存配置，不启动服务（用于部署或验收）'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --reconfigure)
      FORCE_SETUP=1
      ;;
    --setup-only)
      SETUP_ONLY=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "未知参数：$1"
      usage >&2
      exit 2
      ;;
  esac
  shift
done

trim_value() {
  printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

strip_outer_quotes() {
  local value
  value="$(trim_value "$1")"
  case "$value" in
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
  esac
  printf '%s' "$value"
}

read_env_value() {
  local key line
  key="$1"
  [ -f "$ENV_FILE" ] || return 1
  line="$(awk -v wanted="$key" '
    {
      candidate = $0
      sub(/^[[:space:]]*/, "", candidate)
      sub(/^export[[:space:]]+/, "", candidate)
      if (candidate ~ "^" wanted "[[:space:]]*=") {
        sub(/^[^=]*=/, "", candidate)
        value = candidate
        found = 1
      }
    }
    END { if (found) printf "%s", value }
  ' "$ENV_FILE")"
  [ -n "$line" ] || return 1
  strip_outer_quotes "$line"
}

has_unsafe_scalar_content() {
  local value
  value="$1"
  case "$value" in
    *$'\n'*|*$'\r'*|*'${'*) return 0 ;;
  esac
  if printf '%s' "$value" | LC_ALL=C grep -q '[[:cntrl:]]'; then
    return 0
  fi
  return 1
}

valid_port() {
  local port
  port="$1"
  case "$port" in
    ''|*[!0-9]*) return 1 ;;
  esac
  awk -v port="$port" 'BEGIN { exit !(port >= 1 && port <= 65535) }'
}

valid_api_url() {
  local url rest authority host port
  url="$1"
  [ -n "$url" ] || return 1
  has_unsafe_scalar_content "$url" && return 1
  case "$url" in
    *[[:space:]]*|*\?*|*\#*|*@*) return 1 ;;
  esac

  case "$url" in
    https://*) rest="${url#https://}" ;;
    http://localhost|http://localhost/*|http://localhost:*|http://127.0.0.1|http://127.0.0.1/*|http://127.0.0.1:*)
      rest="${url#http://}"
      ;;
    *) return 1 ;;
  esac

  authority="${rest%%/*}"
  [ -n "$authority" ] || return 1
  if ! printf '%s' "$authority" | LC_ALL=C grep -Eq '^[A-Za-z0-9.-]+(:[0-9]+)?$'; then
    return 1
  fi

  host="${authority%%:*}"
  case "$host" in
    ''|.*|-*|*.) return 1 ;;
  esac
  case "$authority" in
    *:*)
      port="${authority##*:}"
      valid_port "$port" || return 1
      ;;
  esac

  case "$url" in
    http://*)
      case "$host" in
        localhost|127.0.0.1) ;;
        *) return 1 ;;
      esac
      ;;
  esac
  return 0
}

valid_api_key() {
  local value lowered
  value="$1"
  [ -n "$value" ] || return 1
  has_unsafe_scalar_content "$value" && return 1
  lowered="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  case "$lowered" in
    your-api-key-here|your_api_key_here|changeme|replace-me|replace_me|'<your-api-key>') return 1 ;;
  esac
  return 0
}

valid_model() {
  local value
  value="$1"
  [ -n "$value" ] || return 1
  has_unsafe_scalar_content "$value" && return 1
  case "$value" in
    *[[:space:]]*) return 1 ;;
  esac
  return 0
}

configuration_is_valid() {
  local current_url current_key current_model
  current_url="$(read_env_value ARK_API_URL 2>/dev/null || true)"
  current_key="$(read_env_value ARK_API_KEY 2>/dev/null || true)"
  current_model="$(read_env_value ARK_MODEL 2>/dev/null || true)"
  valid_api_url "$current_url" && valid_api_key "$current_key" && valid_model "$current_model"
}

dotenv_escape() {
  # python-dotenv 的单引号值支持 \\ 与 \'；${...} 会插值，已在校验阶段拒绝。
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/\\\\'/g"
}

write_configuration() {
  local api_url api_key model env_dir escaped_url escaped_key escaped_model
  api_url="$1"
  api_key="$2"
  model="$3"
  env_dir="$(dirname "$ENV_FILE")"

  if [ -L "$ENV_FILE" ]; then
    fail "为避免覆盖意外目标，配置文件不能是符号链接。"
    return 1
  fi
  mkdir -p "$env_dir"
  TEMP_FILE="$(mktemp "${ENV_FILE}.tmp.XXXXXX")" || {
    fail "无法创建临时配置文件。"
    return 1
  }
  chmod 600 "$TEMP_FILE"

  if [ -f "$ENV_FILE" ]; then
    awk '
      {
        candidate = $0
        sub(/^[[:space:]]*/, "", candidate)
        sub(/^export[[:space:]]+/, "", candidate)
        if (candidate ~ /^(ARK_API_URL|ARK_API_KEY|ARK_MODEL|MAAS_API_URL|MAAS_API_KEY|MAAS_MODEL)[[:space:]]*=/) next
        # 清理新旧启动器写入的托管注释，不依赖任何服务商品牌名称。
        if (candidate ~ /^# Pronoia .*API.*\(managed by launcher\)$/) next
        print
      }
    ' "$ENV_FILE" > "$TEMP_FILE"
  fi

  escaped_url="$(dotenv_escape "$api_url")"
  escaped_key="$(dotenv_escape "$api_key")"
  escaped_model="$(dotenv_escape "$model")"
  {
    printf '\n%s\n' '# Pronoia model API configuration (managed by launcher)'
    printf "ARK_API_URL='%s'\n" "$escaped_url"
    printf "ARK_API_KEY='%s'\n" "$escaped_key"
    printf "ARK_MODEL='%s'\n" "$escaped_model"
  } >> "$TEMP_FILE"

  chmod 600 "$TEMP_FILE"
  mv -f "$TEMP_FILE" "$ENV_FILE"
  TEMP_FILE=''
  chmod 600 "$ENV_FILE"
}

collect_interactive_configuration() {
  local default_url default_model answer api_url api_key model
  default_url="$(read_env_value ARK_API_URL 2>/dev/null || true)"
  default_model="$(read_env_value ARK_MODEL 2>/dev/null || true)"
  valid_api_url "$default_url" || default_url=''
  valid_model "$default_model" || default_model='deepseek-v4-flash'

  printf '\n%b\n' "${BOLD}Pronoia · 首次模型 API 配置${RESET}"
  printf '%s\n' '────────────────────────────────'
  printf '%s\n' '可填写 DeepSeek、火山方舟等模型服务的兼容接口；配置仅保存在这台电脑，不会打进分享包。'

  while :; do
    printf '\n模型 API URL [%s]：' "$default_url"
    IFS= read -r answer || return 1
    api_url="$(trim_value "${answer:-$default_url}")"
    if valid_api_url "$api_url"; then
      break
    fi
    fail "URL 无效：请使用 HTTPS；仅 localhost / 127.0.0.1 可使用 HTTP。"
  done

  while :; do
    printf 'API Key（输入时隐藏）：'
    IFS= read -r -s api_key || return 1
    printf '\n'
    if valid_api_key "$api_key"; then
      break
    fi
    fail "API Key 不能为空、不能使用示例占位值，也不能包含控制字符或 \${...}。"
  done

  while :; do
    printf '模型名称 [%s]：' "$default_model"
    IFS= read -r answer || return 1
    model="$(trim_value "${answer:-$default_model}")"
    if valid_model "$model"; then
      break
    fi
    fail "模型名称不能为空，也不能包含空白、控制字符或 \${...}。"
  done

  write_configuration "$api_url" "$api_key" "$model"
}

noninteractive_requested=0
if [ "${PRONOIA_SETUP_API_URL+x}" = x ] || \
   [ "${PRONOIA_SETUP_API_KEY+x}" = x ] || \
   [ "${PRONOIA_SETUP_MODEL+x}" = x ]; then
  noninteractive_requested=1
  FORCE_SETUP=1
fi

if [ "$FORCE_SETUP" != "1" ] && configuration_is_valid; then
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  if [ "$SETUP_ONLY" = "1" ]; then
    ok "API 配置已就绪。"
  else
    ok "API 配置已就绪，正在启动 Pronoia…"
  fi
else
  if [ "$noninteractive_requested" -eq 1 ]; then
    if [ "${PRONOIA_SETUP_API_URL+x}" != x ] || \
       [ "${PRONOIA_SETUP_API_KEY+x}" != x ] || \
       [ "${PRONOIA_SETUP_MODEL+x}" != x ]; then
      fail "非交互配置必须同时提供 API URL、API Key 和模型名称。"
      exit 2
    fi
    setup_url="$(trim_value "$PRONOIA_SETUP_API_URL")"
    setup_key="$PRONOIA_SETUP_API_KEY"
    setup_model="$(trim_value "$PRONOIA_SETUP_MODEL")"
    if ! valid_api_url "$setup_url"; then
      fail "API URL 无效：请使用 HTTPS；仅 localhost / 127.0.0.1 可使用 HTTP。"
      exit 2
    fi
    if ! valid_api_key "$setup_key"; then
      fail "API Key 无效（具体内容不会输出）。"
      exit 2
    fi
    if ! valid_model "$setup_model"; then
      fail "模型名称无效。"
      exit 2
    fi
    write_configuration "$setup_url" "$setup_key" "$setup_model" || exit 1
  else
    if [ ! -t 0 ]; then
      fail "尚未配置有效 API。请双击“Pronoia 启动器.command”完成首次设置。"
      exit 2
    fi
    collect_interactive_configuration || {
      fail "API 配置未完成。"
      exit 1
    }
  fi
  ok "API 配置已安全保存到 .env（权限 600，Key 不会写入日志）。"
fi

if [ "$SETUP_ONLY" = "1" ]; then
  ok "配置检查完成；已按设置跳过启动。"
  exit 0
fi

exec "$ROOT_DIR/start.sh"
