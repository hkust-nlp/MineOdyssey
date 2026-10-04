#!/usr/bin/env bash
##############################################
# 宿主机直接启动单个 Minecraft 客户端（无容器）
# 支持端口/DISPLAY 自动分配、CPU 软渲染、IPv6 检测
#
# 用法:
#   bash scripts/launch/start-single-bot-inside.sh [BOT_NAME]
#
# 常用环境变量:
#   SERVER_HOST=127.0.0.1       服务端地址
#   SERVER_PORT=25565            服务端游戏端口
#   RENDER_MODE=cpu              渲染模式（cpu / gpu）
#   DISPLAY_INDEX=               手动指定 DISPLAY 号（留空则自动分配）
#   DISPLAY_RESOLUTION=800x600x24
#   AGENTBRIDGE_PORT=            手动指定 agentbridge 端口（留空则自动分配）
#   ENABLE_VNC=false             启用 VNC
#   VNC_PORT=                    手动指定 VNC 端口（留空则自动分配）
#   NOVNC_PORT=                  手动指定 noVNC 端口（留空则自动分配）
#   VNC_VIEWONLY=false           VNC 只读模式
#   COPY_GAME_MODS_TEMPLATE=true Copy shared game/mods into the runtime
#   PORT_AUTO_ALLOCATE=true      是否自动分配端口
#   CLIENT_FOREGROUND=false      设为 true 前台等待
##############################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# ── IPv6 自动检测 ──
if ! curl -4 -s --connect-timeout 3 https://maven.neoforged.net -o /dev/null 2>/dev/null; then
    _JAVA_IPV6_OPTS="-Djava.net.preferIPv4Stack=false -Djava.net.preferIPv6Addresses=true"
    if [[ " ${JAVA_TOOL_OPTIONS:-} " != *" ${_JAVA_IPV6_OPTS} "* ]]; then
        export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:+${JAVA_TOOL_OPTIONS} }${_JAVA_IPV6_OPTS}"
        echo "[net] IPv4 unreachable, enabling Java IPv6 mode"
    fi
fi

# ── 参数 ──
BOT_NAME="${1:-${BOT_NAME:-Bot1}}"
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
# IPv6 地址自动加 [] （如果包含 : 且尚未被 [] 包裹）
if [[ "$SERVER_HOST" == *:* && "$SERVER_HOST" != \[* ]]; then
    SERVER_HOST="[${SERVER_HOST}]"
fi
SERVER_PORT="${SERVER_PORT:-25565}"
RENDER_MODE="${RENDER_MODE:-cpu}"
DISPLAY_RESOLUTION="${DISPLAY_RESOLUTION:-800x600x24}"
DISPLAY_INDEX="${DISPLAY_INDEX:-}"
AGENTBRIDGE_PORT="${AGENTBRIDGE_PORT:-}"
ENABLE_REMOTE_BASH="${ENABLE_REMOTE_BASH:-true}"
REMOTE_BASH_PORT="${REMOTE_BASH_PORT:-}"
ENABLE_VNC="${ENABLE_VNC:-false}"
VNC_PORT="${VNC_PORT:-}"
NOVNC_PORT="${NOVNC_PORT:-}"
VNC_VIEWONLY="${VNC_VIEWONLY:-false}"
CLIENT_FOREGROUND="${CLIENT_FOREGROUND:-false}"
COPY_GAME_MODS_TEMPLATE="${COPY_GAME_MODS_TEMPLATE:-true}"

PORT_AUTO_ALLOCATE="${PORT_AUTO_ALLOCATE:-true}"
PORT_REGISTRY_TOOL="${PORT_REGISTRY_TOOL:-$SCRIPT_DIR/../runtime/port_registry.py}"
PORT_REGISTRY_FILE="${PORT_REGISTRY_FILE:-$PROJECT_ROOT/runtime/port-registry.json}"
PORT_RANGE_START="${PORT_RANGE_START:-20000}"
PORT_RANGE_END="${PORT_RANGE_END:-30000}"
DISPLAY_REGISTRY_TOOL="${DISPLAY_REGISTRY_TOOL:-$SCRIPT_DIR/../runtime/display_registry.py}"
DISPLAY_REGISTRY_FILE="${DISPLAY_REGISTRY_FILE:-$PROJECT_ROOT/runtime/display-registry.json}"
DISPLAY_RANGE_START="${DISPLAY_RANGE_START:-20}"
DISPLAY_RANGE_END="${DISPLAY_RANGE_END:-2200}"
DISPLAY_SCAN_LIMIT="${DISPLAY_SCAN_LIMIT:-256}"
DISPLAY_PREFERRED="${DISPLAY_PREFERRED:-20}"

BASE_LOG_DIR="${BASE_LOG_DIR:-$PROJECT_ROOT/logs}"
BASE_RUNTIME_DIR="${BASE_RUNTIME_DIR:-$PROJECT_ROOT/runtime}"
CLIENTS_DIR="${CLIENTS_DIR:-$PROJECT_ROOT/workspaces/main}"

ALLOC_OWNERS=()
DISPLAY_ALLOC_OWNERS=()
CLIENT_PID=""
SCRIPT_SUCCESS="false"

# ── 帮助 ──
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n '2,/^##*$/{ /^#/s/^# \?//p }' "$0"
    exit 0
fi

# ── 端口分配工具 ──
run_port_registry() {
    python3 "$PORT_REGISTRY_TOOL" \
        --file "$PORT_REGISTRY_FILE" \
        --range-start "$PORT_RANGE_START" \
        --range-end "$PORT_RANGE_END" \
        "$@"
}

run_display_registry() {
    python3 "$DISPLAY_REGISTRY_TOOL" \
        --file "$DISPLAY_REGISTRY_FILE" \
        --range-start "$DISPLAY_RANGE_START" \
        --range-end "$DISPLAY_RANGE_END" \
        "$@"
}

allocate_one_port() {
    local service="$1" owner="$2" __port_var="$3" __token_var="$4"
    local out line
    out="$(run_port_registry allocate --count 1 --service "$service" --owner "$owner" --pid "$$")"
    line="$(python3 -c "import json,sys; a=json.loads(sys.argv[1])['allocations'][0]; print(f'{a[\"port\"]}|{a[\"token\"]}')" "$out")"
    printf -v "$__port_var" '%s' "${line%%|*}"
    printf -v "$__token_var" '%s' "${line##*|}"
    ALLOC_OWNERS+=("$owner")
}

allocate_one_display() {
    local service="$1" owner="$2" preferred="$3" __display_var="$4" __token_var="$5"
    local out line
    out="$(run_display_registry allocate \
        --count 1 --service "$service" --owner "$owner" --pid "$$" \
        --preferred "$preferred" --scan-limit "$DISPLAY_SCAN_LIMIT")"
    line="$(python3 -c "import json,sys; a=json.loads(sys.argv[1])['allocations'][0]; print(f'{a[\"display_index\"]}|{a[\"token\"]}')" "$out")"
    printf -v "$__display_var" '%s' "${line%%|*}"
    printf -v "$__token_var" '%s' "${line##*|}"
    DISPLAY_ALLOC_OWNERS+=("$owner")
}

bind_tokens_to_pid() {
    local pid="$1"; shift
    local args=(bind --pid "$pid")
    for t in "$@"; do [[ -n "$t" ]] && args+=(--token "$t"); done
    run_port_registry "${args[@]}" >/dev/null
}

start_token_watcher() {
    local pid="$1"; shift
    local args=("$PORT_REGISTRY_TOOL" --file "$PORT_REGISTRY_FILE" --range-start "$PORT_RANGE_START" --range-end "$PORT_RANGE_END" watch --pid "$pid")
    for t in "$@"; do [[ -n "$t" ]] && args+=(--token "$t"); done
    nohup python3 "${args[@]}" >/dev/null 2>&1 &
}

bind_display_tokens_to_pid() {
    local pid="$1"; shift
    local args=(bind --pid "$pid")
    for t in "$@"; do [[ -n "$t" ]] && args+=(--token "$t"); done
    run_display_registry "${args[@]}" >/dev/null
}

start_display_token_watcher() {
    local pid="$1"; shift
    local args=("$DISPLAY_REGISTRY_TOOL" --file "$DISPLAY_REGISTRY_FILE" --range-start "$DISPLAY_RANGE_START" --range-end "$DISPLAY_RANGE_END" watch --pid "$pid")
    for t in "$@"; do [[ -n "$t" ]] && args+=(--token "$t"); done
    nohup python3 "${args[@]}" >/dev/null 2>&1 &
}

# ── 进程树 kill ──
kill_process_tree() {
    local root_pid="$1" signal_name="$2"
    local child_pids
    child_pids="$(ps -o pid= --ppid "$root_pid" 2>/dev/null | tr -d ' ' || true)"
    for child_pid in $child_pids; do
        [[ -n "$child_pid" ]] || continue
        kill_process_tree "$child_pid" "$signal_name"
    done
    kill "-${signal_name}" "$root_pid" 2>/dev/null || true
}

# ── 清理 ──
cleanup_on_exit() {
    if [[ "$SCRIPT_SUCCESS" == "true" ]]; then return; fi
    if [[ -n "$CLIENT_PID" ]] && kill -0 "$CLIENT_PID" 2>/dev/null; then
        kill_process_tree "$CLIENT_PID" TERM
        sleep 0.3
        kill -0 "$CLIENT_PID" 2>/dev/null && kill_process_tree "$CLIENT_PID" KILL
    fi
    for owner in "${ALLOC_OWNERS[@]}"; do
        run_port_registry release --owner "$owner" >/dev/null 2>&1 || true
    done
    for owner in "${DISPLAY_ALLOC_OWNERS[@]}"; do
        run_display_registry release --owner "$owner" >/dev/null 2>&1 || true
    done
}
trap cleanup_on_exit EXIT INT TERM

# ── 初始化目录 ──
RUN_ID="$(date +%Y%m%d_%H%M%S)_$$"
RUN_LOG_DIR="$BASE_LOG_DIR/$RUN_ID"
PID_FILE="$BASE_RUNTIME_DIR/$RUN_ID.pids"
CLIENT_LOG="$RUN_LOG_DIR/client-${BOT_NAME}.log"
PORT_OWNER_PREFIX="bot:${RUN_ID}"
CLIENT_OWNER="${PORT_OWNER_PREFIX}:client:${BOT_NAME}"

CLIENT_ROOT="${CLIENTS_DIR}/${BOT_NAME}"
CLIENT_GAME_DIR="${CLIENT_ROOT}/game"
CLIENT_GAME_TEMPLATE="${CLIENT_GAME_TEMPLATE:-$PROJECT_ROOT/game}"

mkdir -p "$RUN_LOG_DIR" "$BASE_RUNTIME_DIR" "$CLIENT_ROOT"

# Seed game dir from shared game directory if empty
if [[ -z "$(ls -A "$CLIENT_GAME_DIR" 2>/dev/null || true)" ]]; then
    if [[ -d "$CLIENT_GAME_TEMPLATE" && -n "$(ls -A "$CLIENT_GAME_TEMPLATE" 2>/dev/null || true)" ]]; then
        echo "  Seeding game dir from: $CLIENT_GAME_TEMPLATE -> $CLIENT_GAME_DIR"
        mkdir -p "$CLIENT_GAME_DIR"
        cp -a --reflink=auto "${CLIENT_GAME_TEMPLATE}/." "${CLIENT_GAME_DIR}/" 2>/dev/null \
            || cp -a "${CLIENT_GAME_TEMPLATE}/." "${CLIENT_GAME_DIR}/"
    else
        echo "  ⚠ No shared game dir at $CLIENT_GAME_TEMPLATE, creating empty game dir"
        mkdir -p "$CLIENT_GAME_DIR"
    fi
fi

# Seed mod configs from tracked templates into this task's client game dir.
# The repository may be mounted read-only during fleet evaluation.
for tmpl in "$PROJECT_ROOT"/config/minihud.json; do
    [ -f "$tmpl" ] || continue
    target="$CLIENT_GAME_DIR/config/$(basename "$tmpl")"
    if [ ! -f "$target" ]; then
        mkdir -p "$(dirname "$target")"
        cp "$tmpl" "$target"
        echo "  Seeded mod config: $(basename "$tmpl")"
    fi
done
run_port_registry cleanup >/dev/null || true
run_display_registry cleanup >/dev/null || true

# ── 分配端口和 DISPLAY ──
agentbridge_token=""
remote_bash_token=""
display_token=""
vnc_port="$VNC_PORT"
novnc_port="$NOVNC_PORT"
vnc_token=""
novnc_token=""

if [[ "${PORT_AUTO_ALLOCATE,,}" == "true" ]]; then
    if [[ -z "$AGENTBRIDGE_PORT" ]]; then
        allocate_one_port "client.agentbridge" "$CLIENT_OWNER" AGENTBRIDGE_PORT agentbridge_token
    fi
    if [[ "${ENABLE_REMOTE_BASH,,}" == "true" ]] && [[ -z "$REMOTE_BASH_PORT" ]]; then
        allocate_one_port "client.remote_bash" "$CLIENT_OWNER" REMOTE_BASH_PORT remote_bash_token
    fi
else
    AGENTBRIDGE_PORT="${AGENTBRIDGE_PORT:-8080}"
    REMOTE_BASH_PORT="${REMOTE_BASH_PORT:-9090}"
fi

if [[ -z "$DISPLAY_INDEX" ]]; then
    allocate_one_display "client.display" "$CLIENT_OWNER" "$DISPLAY_PREFERRED" DISPLAY_INDEX display_token
fi

if [[ "${ENABLE_VNC,,}" == "true" ]]; then
    if [[ "${PORT_AUTO_ALLOCATE,,}" == "true" ]]; then
        if [[ -z "$vnc_port" ]]; then
            allocate_one_port "client.vnc" "$CLIENT_OWNER" vnc_port vnc_token
        fi
        if [[ -z "$novnc_port" ]]; then
            allocate_one_port "client.novnc" "$CLIENT_OWNER" novnc_port novnc_token
        fi
    else
        vnc_port="${vnc_port:-5900}"
        novnc_port="${novnc_port:-6080}"
    fi
fi

# ── 写入 runtime 配置 ──
cat > "${CLIENT_ROOT}/.mcbots_runtime.json" <<EOF
{
  "bot_name": "${BOT_NAME}",
  "workspace_root": "${CLIENT_ROOT}",
  "agentbridge": {
    "host": "127.0.0.1",
    "port": ${AGENTBRIDGE_PORT}
  },
  "remote_bash": {
    "host": "127.0.0.1",
    "port": ${REMOTE_BASH_PORT:-0}
  },
  "x11": {
    "display": ":${DISPLAY_INDEX}",
    "resolution": "${DISPLAY_RESOLUTION%x*}"
  }
}
EOF

echo "=========================================="
echo "  Minecraft 客户端 (inside mode)"
echo "=========================================="
echo "  玩家名: $BOT_NAME"
echo "  服务器: $SERVER_HOST:$SERVER_PORT"
echo "  渲染模式: $RENDER_MODE"
echo "  DISPLAY: :$DISPLAY_INDEX"
echo "  AgentBridge: $AGENTBRIDGE_PORT"
if [[ "${ENABLE_REMOTE_BASH,,}" == "true" ]]; then
    echo "  RemoteBash: ${REMOTE_BASH_PORT}"
fi
echo "  启动批次: $RUN_ID"
echo "  日志: $CLIENT_LOG"
echo "=========================================="

# ── 启动 client ──
echo "启动 client..."
if [[ "${ENABLE_VNC,,}" == "true" ]]; then
    (
        PLAYER_NAME="$BOT_NAME" \
        SERVER_HOST="$SERVER_HOST" \
        SERVER_PORT="$SERVER_PORT" \
        RENDER_MODE="$RENDER_MODE" \
        AGENTBRIDGE_PORT="$AGENTBRIDGE_PORT" \
        DISPLAY_INDEX="$DISPLAY_INDEX" \
        DISPLAY_RESOLUTION="$DISPLAY_RESOLUTION" \
        ENABLE_REMOTE_BASH="$ENABLE_REMOTE_BASH" \
        REMOTE_BASH_PORT="${REMOTE_BASH_PORT:-9090}" \
        GAME_DIR="$CLIENT_GAME_DIR" \
        COPY_GAME_MODS_TEMPLATE="$COPY_GAME_MODS_TEMPLATE" \
        WORKSPACE_ROOT="$CLIENT_ROOT" \
        MCBOTS_PROJECT_ROOT="$PROJECT_ROOT" \
        VNC_PORT="$vnc_port" \
        NOVNC_PORT="$novnc_port" \
        VNC_VIEWONLY="$VNC_VIEWONLY" \
        "$SCRIPT_DIR/../entrypoints/entrypoint-vnc.sh"
    ) >"$CLIENT_LOG" 2>&1 &
else
    (
        PLAYER_NAME="$BOT_NAME" \
        SERVER_HOST="$SERVER_HOST" \
        SERVER_PORT="$SERVER_PORT" \
        RENDER_MODE="$RENDER_MODE" \
        AGENTBRIDGE_PORT="$AGENTBRIDGE_PORT" \
        DISPLAY_INDEX="$DISPLAY_INDEX" \
        DISPLAY_RESOLUTION="$DISPLAY_RESOLUTION" \
        ENABLE_REMOTE_BASH="$ENABLE_REMOTE_BASH" \
        REMOTE_BASH_PORT="${REMOTE_BASH_PORT:-9090}" \
        GAME_DIR="$CLIENT_GAME_DIR" \
        COPY_GAME_MODS_TEMPLATE="$COPY_GAME_MODS_TEMPLATE" \
        WORKSPACE_ROOT="$CLIENT_ROOT" \
        MCBOTS_PROJECT_ROOT="$PROJECT_ROOT" \
        "$SCRIPT_DIR/../entrypoints/entrypoint.sh"
    ) >"$CLIENT_LOG" 2>&1 &
fi
CLIENT_PID=$!

# ── 绑定 token 和 watcher ──
if [[ -n "$agentbridge_token" || -n "$remote_bash_token" ]]; then
    bind_tokens_to_pid "$CLIENT_PID" "$agentbridge_token" "$remote_bash_token" "$vnc_token" "$novnc_token"
    start_token_watcher "$CLIENT_PID" "$agentbridge_token" "$remote_bash_token" "$vnc_token" "$novnc_token"
fi
if [[ -n "$display_token" ]]; then
    bind_display_tokens_to_pid "$CLIENT_PID" "$display_token"
    start_display_token_watcher "$CLIENT_PID" "$display_token"
fi

# ── 写入 PID 文件 ──
{
    echo "RUN_ID=$RUN_ID"
    echo "CLIENT_NAME=$BOT_NAME"
    echo "CLIENT_PID=$CLIENT_PID"
    echo "CLIENT_LOG=$CLIENT_LOG"
    echo "SERVER_HOST=$SERVER_HOST"
    echo "SERVER_PORT=$SERVER_PORT"
    echo "AGENTBRIDGE_PORT=$AGENTBRIDGE_PORT"
    echo "REMOTE_BASH_PORT=${REMOTE_BASH_PORT:-}"
    echo "VNC_PORT=${vnc_port:-}"
    echo "NOVNC_PORT=${novnc_port:-}"
    echo "DISPLAY_INDEX=$DISPLAY_INDEX"
    echo "PORT_REGISTRY_FILE=$PORT_REGISTRY_FILE"
} >"$PID_FILE"

SCRIPT_SUCCESS="true"

if [[ "${ENABLE_VNC,,}" == "true" ]]; then
    echo "  ✓ $BOT_NAME 已启动 (PID: $CLIENT_PID, DISPLAY=:$DISPLAY_INDEX, agentbridge=$AGENTBRIDGE_PORT, noVNC=http://<host>:$novnc_port/vnc.html)"
else
    echo "  ✓ $BOT_NAME 已启动 (PID: $CLIENT_PID, DISPLAY=:$DISPLAY_INDEX, agentbridge=$AGENTBRIDGE_PORT)"
fi

echo ""
echo "  PID 文件: $PID_FILE"
echo "  查看日志: tail -f $CLIENT_LOG"

if [[ "${CLIENT_FOREGROUND,,}" == "true" ]]; then
    echo ""
    echo "前台模式，按 Ctrl+C 退出..."
    SCRIPT_SUCCESS="false"
    tail -f "$CLIENT_LOG" &
    wait "$CLIENT_PID" || true
fi
