#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

CLIENT_COUNT="${CLIENT_COUNT:-5}"
CLIENT_PREFIX="${CLIENT_PREFIX:-Bot}"
CLIENT_RENDER_MODE="${CLIENT_RENDER_MODE:-cpu}"
CLIENT_DISPLAY_BASE="${CLIENT_DISPLAY_BASE:-20}"
CLIENT_DISPLAY_SCAN_LIMIT="${CLIENT_DISPLAY_SCAN_LIMIT:-256}"
CLIENT_DISPLAY_RESOLUTION="${CLIENT_DISPLAY_RESOLUTION:-800x600x24}"
CLIENT_START_GAP="${CLIENT_START_GAP:-3}"
CLIENT_ENABLE_VNC="${CLIENT_ENABLE_VNC:-false}"
CLIENT_VNC_VIEWONLY="${CLIENT_VNC_VIEWONLY:-false}"

CLIENT_SERVER_HOST="${CLIENT_SERVER_HOST:-127.0.0.1}"
CLIENT_SERVER_PORT="${CLIENT_SERVER_PORT:-}"

SERVER_MEMORY_MIN="${SERVER_MEMORY_MIN:-2G}"
SERVER_MEMORY_MAX="${SERVER_MEMORY_MAX:-8G}"
MC_VERSION="${MC_VERSION:-1.21.1}"
NEOFORGE_VERSION="${NEOFORGE_VERSION:-21.1.217}"
ENABLE_RCON="${ENABLE_RCON:-true}"
RCON_PASSWORD="${RCON_PASSWORD:-minecraft}"
SERVER_RCON_PORT="${SERVER_RCON_PORT:-}"
OP_PLAYER="${OP_PLAYER:-}"
DIFFICULTY="${DIFFICULTY:-}"
MAX_PLAYERS="${MAX_PLAYERS:-}"
SERVER_WAIT_TIMEOUT="${SERVER_WAIT_TIMEOUT:-1800}"

PORT_AUTO_ALLOCATE="${PORT_AUTO_ALLOCATE:-true}"
PORT_REGISTRY_TOOL="${PORT_REGISTRY_TOOL:-$SCRIPT_DIR/../runtime/port_registry.py}"
PORT_REGISTRY_FILE="${PORT_REGISTRY_FILE:-$PROJECT_ROOT/runtime/port-registry.json}"
PORT_RANGE_START="${PORT_RANGE_START:-20000}"
PORT_RANGE_END="${PORT_RANGE_END:-30000}"
DISPLAY_REGISTRY_TOOL="${DISPLAY_REGISTRY_TOOL:-$SCRIPT_DIR/../runtime/display_registry.py}"
DISPLAY_REGISTRY_FILE="${DISPLAY_REGISTRY_FILE:-$PROJECT_ROOT/runtime/display-registry.json}"
DISPLAY_RANGE_START="${DISPLAY_RANGE_START:-20}"
DISPLAY_RANGE_END="${DISPLAY_RANGE_END:-2200}"
ALLOC_OWNERS=()
DISPLAY_ALLOC_OWNERS=()
STARTED_PIDS=()

BASE_LOG_DIR="${BASE_LOG_DIR:-$PROJECT_ROOT/logs}"
BASE_RUNTIME_DIR="${BASE_RUNTIME_DIR:-$PROJECT_ROOT/runtime}"
SERVER_DATA_DIR="${SERVER_DATA_DIR:-$PROJECT_ROOT/server-data}"
CLIENTS_DIR="${CLIENTS_DIR:-$PROJECT_ROOT/workspaces/main}"

usage() {
    cat <<'EOF'
容器内启动脚本：1 个 server + 多个 client

用法:
  ./scripts/launch/start-server-clients-inside.sh

常用环境变量:
  CLIENT_COUNT=5
  CLIENT_PREFIX=Bot
  CLIENT_RENDER_MODE=cpu
  CLIENT_DISPLAY_BASE=20
  CLIENT_DISPLAY_RESOLUTION=800x600x24
  CLIENT_SERVER_HOST=127.0.0.1
  CLIENT_SERVER_PORT=25565
  CLIENT_START_GAP=3
  CLIENT_ENABLE_VNC=false
  CLIENT_VNC_VIEWONLY=false

  SERVER_MEMORY_MIN=2G
  SERVER_MEMORY_MAX=8G
  MC_VERSION=1.21.1
  NEOFORGE_VERSION=21.1.217
  ENABLE_RCON=true
  RCON_PASSWORD=minecraft
  PORT_AUTO_ALLOCATE=true
  PORT_RANGE_START=20000
  PORT_RANGE_END=30000
  PORT_REGISTRY_FILE=<project>/runtime/port-registry.json
  OP_PLAYER=
  DIFFICULTY=               # peaceful/easy/normal/hard（留空保持默认）
  MAX_PLAYERS=              # 留空保持默认 20
  SERVER_WAIT_TIMEOUT=1800

说明:
  - 本脚本会后台启动 server 与多个 client，启动后立即退出。
  - 端口默认自动分配并登记，登记文件: <project>/runtime/port-registry.json
  - 日志目录: <project>/logs/<run_id>/
  - PID 文件: <project>/runtime/<run_id>.pids
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if ! [[ "$CLIENT_COUNT" =~ ^[0-9]+$ ]] || [[ "$CLIENT_COUNT" -lt 1 ]]; then
    echo "错误: CLIENT_COUNT 必须是 >= 1 的整数，当前: $CLIENT_COUNT"
    exit 1
fi

if [[ "$CLIENT_RENDER_MODE" != "cpu" ]]; then
    echo "警告: 你当前选择的是 CLIENT_RENDER_MODE=$CLIENT_RENDER_MODE"
    echo "按你这轮需求推荐 cpu；如确实需要可继续。"
fi

if [[ ! -x "$PORT_REGISTRY_TOOL" ]]; then
    echo "错误: 端口分配工具不存在或不可执行: $PORT_REGISTRY_TOOL"
    exit 1
fi
if [[ ! -f "$DISPLAY_REGISTRY_TOOL" ]]; then
    echo "错误: DISPLAY 分配工具不存在: $DISPLAY_REGISTRY_TOOL"
    exit 1
fi

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
    local service="$1"
    local owner="$2"
    local __port_var="$3"
    local __token_var="$4"
    local out line
    out="$(run_port_registry allocate --count 1 --service "$service" --owner "$owner" --pid "$$")"
    line="$(
        python3 - "$out" <<'PY'
import json
import sys
d = json.loads(sys.argv[1])
alloc = d["allocations"][0]
print(f'{alloc["port"]}|{alloc["token"]}')
PY
    )"
    printf -v "$__port_var" '%s' "${line%%|*}"
    printf -v "$__token_var" '%s' "${line##*|}"
    ALLOC_OWNERS+=("$owner")
}

allocate_one_display() {
    local service="$1"
    local owner="$2"
    local preferred="$3"
    local __display_var="$4"
    local __token_var="$5"
    local out line
    out="$(
        run_display_registry allocate \
            --count 1 \
            --service "$service" \
            --owner "$owner" \
            --pid "$$" \
            --preferred "$preferred" \
            --scan-limit "$CLIENT_DISPLAY_SCAN_LIMIT"
    )"
    line="$(
        python3 - "$out" <<'PY'
import json
import sys
d = json.loads(sys.argv[1])
alloc = d["allocations"][0]
print(f'{alloc["display_index"]}|{alloc["token"]}')
PY
    )"
    printf -v "$__display_var" '%s' "${line%%|*}"
    printf -v "$__token_var" '%s' "${line##*|}"
    DISPLAY_ALLOC_OWNERS+=("$owner")
}

bind_tokens_to_pid() {
    local pid="$1"
    shift
    local args=(bind --pid "$pid")
    local t
    for t in "$@"; do
        [[ -n "$t" ]] && args+=(--token "$t")
    done
    run_port_registry "${args[@]}" >/dev/null
}

start_token_watcher() {
    local pid="$1"
    shift
    local args=("$PORT_REGISTRY_TOOL" --file "$PORT_REGISTRY_FILE" --range-start "$PORT_RANGE_START" --range-end "$PORT_RANGE_END" watch --pid "$pid")
    local t
    for t in "$@"; do
        [[ -n "$t" ]] && args+=(--token "$t")
    done
    nohup python3 "${args[@]}" >/dev/null 2>&1 &
}

RUN_ID="$(date +%Y%m%d_%H%M%S)_$$"
RUN_LOG_DIR="$BASE_LOG_DIR/$RUN_ID"
PID_FILE="$BASE_RUNTIME_DIR/$RUN_ID.pids"
SERVER_LOG="$RUN_LOG_DIR/server.log"
PORT_OWNER_PREFIX="main:${RUN_ID}"
SCRIPT_SUCCESS="false"

kill_process_tree() {
    local root_pid="$1"
    local signal_name="$2"
    local child_pid
    local child_pids
    child_pids="$(ps -o pid= --ppid "$root_pid" 2>/dev/null | tr -d ' ' || true)"
    for child_pid in $child_pids; do
        [[ -n "$child_pid" ]] || continue
        kill_process_tree "$child_pid" "$signal_name"
    done
    kill "-${signal_name}" "$root_pid" 2>/dev/null || true
}

bind_display_tokens_to_pid() {
    local pid="$1"
    shift
    local args=(bind --pid "$pid")
    local t
    for t in "$@"; do
        [[ -n "$t" ]] && args+=(--token "$t")
    done
    run_display_registry "${args[@]}" >/dev/null
}

start_display_token_watcher() {
    local pid="$1"
    shift
    local args=("$DISPLAY_REGISTRY_TOOL" --file "$DISPLAY_REGISTRY_FILE" --range-start "$DISPLAY_RANGE_START" --range-end "$DISPLAY_RANGE_END" watch --pid "$pid")
    local t
    for t in "$@"; do
        [[ -n "$t" ]] && args+=(--token "$t")
    done
    nohup python3 "${args[@]}" >/dev/null 2>&1 &
}

cleanup_on_exit() {
    if [[ "$SCRIPT_SUCCESS" == "true" ]]; then
        return
    fi
    local pid
    for pid in "${STARTED_PIDS[@]}"; do
        [[ -n "$pid" ]] || continue
        if kill -0 "$pid" 2>/dev/null; then
            kill_process_tree "$pid" TERM
        fi
    done
    sleep 0.3
    for pid in "${STARTED_PIDS[@]}"; do
        [[ -n "$pid" ]] || continue
        if kill -0 "$pid" 2>/dev/null; then
            kill_process_tree "$pid" KILL
        fi
    done

    local owner
    for owner in "${ALLOC_OWNERS[@]}"; do
        run_port_registry release --owner "$owner" >/dev/null 2>&1 || true
    done
    for owner in "${DISPLAY_ALLOC_OWNERS[@]}"; do
        run_display_registry release --owner "$owner" >/dev/null 2>&1 || true
    done
}

trap cleanup_on_exit EXIT INT TERM

mkdir -p "$RUN_LOG_DIR" "$BASE_RUNTIME_DIR" "$CLIENTS_DIR" "$SERVER_DATA_DIR"

run_port_registry cleanup >/dev/null || true
run_display_registry cleanup >/dev/null || true

SERVER_PORT_TOKEN=""
RCON_PORT_TOKEN=""
if [[ "${PORT_AUTO_ALLOCATE,,}" == "true" ]]; then
    allocate_one_port "main.server.game" "${PORT_OWNER_PREFIX}:server" CLIENT_SERVER_PORT SERVER_PORT_TOKEN
    allocate_one_port "main.server.rcon" "${PORT_OWNER_PREFIX}:server" SERVER_RCON_PORT RCON_PORT_TOKEN
else
    echo "错误: 当前脚本仅支持 PORT_AUTO_ALLOCATE=true"
    exit 1
fi

echo "=========================================="
echo "启动批次: $RUN_ID"
echo "日志目录: $RUN_LOG_DIR"
echo "端口登记: $PORT_REGISTRY_FILE"
echo "display 登记: $DISPLAY_REGISTRY_FILE"
echo "主服务端口: $CLIENT_SERVER_PORT (RCON: $SERVER_RCON_PORT)"
echo "=========================================="

echo "[1/3] 启动 server..."
(
    cd "$SERVER_DATA_DIR"
    MEMORY_MIN="$SERVER_MEMORY_MIN" \
    MEMORY_MAX="$SERVER_MEMORY_MAX" \
    MC_VERSION="$MC_VERSION" \
    NEOFORGE_VERSION="$NEOFORGE_VERSION" \
    ENABLE_RCON="$ENABLE_RCON" \
    RCON_PASSWORD="$RCON_PASSWORD" \
    SERVER_PORT="$CLIENT_SERVER_PORT" \
    RCON_PORT="$SERVER_RCON_PORT" \
    OP_PLAYER="$OP_PLAYER" \
    DIFFICULTY="$DIFFICULTY" \
    MAX_PLAYERS="$MAX_PLAYERS" \
    "$SCRIPT_DIR/../entrypoints/server-entrypoint.sh"
) >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
STARTED_PIDS+=("$SERVER_PID")

bind_tokens_to_pid "$SERVER_PID" "$SERVER_PORT_TOKEN" "$RCON_PORT_TOKEN"
start_token_watcher "$SERVER_PID" "$SERVER_PORT_TOKEN" "$RCON_PORT_TOKEN"

echo "  server PID: $SERVER_PID"
echo "  server log: $SERVER_LOG"

echo "[2/3] 等待 server 就绪 (timeout=${SERVER_WAIT_TIMEOUT}s)..."
elapsed=0
while [[ "$elapsed" -lt "$SERVER_WAIT_TIMEOUT" ]]; do
    if grep -q "Done (" "$SERVER_LOG" 2>/dev/null; then
        echo "  ✓ 检测到 server 启动完成"
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "  ✗ server 进程已退出，请查看日志: $SERVER_LOG"
        tail -n 80 "$SERVER_LOG" || true
        exit 1
    fi
    sleep 3
    elapsed=$((elapsed + 3))
done

if [[ "$elapsed" -ge "$SERVER_WAIT_TIMEOUT" ]]; then
    echo "  ✗ 等待超时，请查看日志: $SERVER_LOG"
    exit 1
fi

echo "[3/3] 启动 $CLIENT_COUNT 个 client..."
{
    echo "RUN_ID=$RUN_ID"
    echo "SERVER_PID=$SERVER_PID"
    echo "SERVER_PORT=$CLIENT_SERVER_PORT"
    echo "RCON_PORT=$SERVER_RCON_PORT"
    echo "PORT_REGISTRY_FILE=$PORT_REGISTRY_FILE"
} >"$PID_FILE"

for i in $(seq 1 "$CLIENT_COUNT"); do
    client_name="${CLIENT_PREFIX}${i}"
    client_root="${CLIENTS_DIR}/${client_name}"
    client_game_dir="${client_root}/game"
    client_log="${RUN_LOG_DIR}/client-${client_name}.log"
    preferred_display=$((CLIENT_DISPLAY_BASE + i - 1))
    client_owner="${PORT_OWNER_PREFIX}:client:${client_name}"
    agentbridge_port=""
    agentbridge_token=""
    display_index=""
    display_token=""
    vnc_port=""
    novnc_port=""
    vnc_token=""
    novnc_token=""

    mkdir -p "$client_game_dir"
    allocate_one_port "client.agentbridge" "$client_owner" agentbridge_port agentbridge_token
    if ! allocate_one_display "client.display" "$client_owner" "$preferred_display" display_index display_token; then
        echo "ERROR: 无可用 DISPLAY，client=$client_name preferred=:${preferred_display} scan_limit=${CLIENT_DISPLAY_SCAN_LIMIT}"
        exit 1
    fi
    if [[ "$display_index" != "$preferred_display" ]]; then
        echo "  ⚠ $client_name display :${preferred_display} 已占用，改用 :${display_index}"
    fi

    cat > "${client_root}/.mcbots_runtime.json" <<EOF
{
  "bot_name": "${client_name}",
  "workspace_root": "${client_root}",
  "agentbridge": {
    "host": "127.0.0.1",
    "port": ${agentbridge_port}
  },
  "remote_bash": {
    "host": "127.0.0.1",
    "port": 9090
  },
  "x11": {
    "display": ":${display_index}",
    "resolution": "${CLIENT_DISPLAY_RESOLUTION%x*}"
  }
}
EOF

    if [[ "${CLIENT_ENABLE_VNC,,}" == "true" ]]; then
        allocate_one_port "client.vnc" "$client_owner" vnc_port vnc_token
        allocate_one_port "client.novnc" "$client_owner" novnc_port novnc_token
        (
            PLAYER_NAME="$client_name" \
            SERVER_HOST="$CLIENT_SERVER_HOST" \
            SERVER_PORT="$CLIENT_SERVER_PORT" \
            RENDER_MODE="$CLIENT_RENDER_MODE" \
            AGENTBRIDGE_PORT="$agentbridge_port" \
            DISPLAY_INDEX="$display_index" \
            DISPLAY_RESOLUTION="$CLIENT_DISPLAY_RESOLUTION" \
            ENABLE_REMOTE_BASH=false \
            GAME_DIR="$client_game_dir" \
            WORKSPACE_ROOT="$client_root" \
            MCBOTS_PROJECT_ROOT="$PROJECT_ROOT" \
            VNC_PORT="$vnc_port" \
            NOVNC_PORT="$novnc_port" \
            VNC_VIEWONLY="$CLIENT_VNC_VIEWONLY" \
            "$SCRIPT_DIR/../entrypoints/entrypoint-vnc.sh"
        ) >"$client_log" 2>&1 &
    else
        (
            PLAYER_NAME="$client_name" \
            SERVER_HOST="$CLIENT_SERVER_HOST" \
            SERVER_PORT="$CLIENT_SERVER_PORT" \
            RENDER_MODE="$CLIENT_RENDER_MODE" \
            AGENTBRIDGE_PORT="$agentbridge_port" \
            DISPLAY_INDEX="$display_index" \
            DISPLAY_RESOLUTION="$CLIENT_DISPLAY_RESOLUTION" \
            ENABLE_REMOTE_BASH=false \
            GAME_DIR="$client_game_dir" \
            WORKSPACE_ROOT="$client_root" \
            MCBOTS_PROJECT_ROOT="$PROJECT_ROOT" \
            "$SCRIPT_DIR/../entrypoints/entrypoint.sh"
        ) >"$client_log" 2>&1 &
    fi
    client_pid=$!
    STARTED_PIDS+=("$client_pid")
    bind_tokens_to_pid "$client_pid" "$agentbridge_token" "$vnc_token" "$novnc_token"
    start_token_watcher "$client_pid" "$agentbridge_token" "$vnc_token" "$novnc_token"
    bind_display_tokens_to_pid "$client_pid" "$display_token"
    start_display_token_watcher "$client_pid" "$display_token"

    echo "CLIENT_${i}_NAME=$client_name" >>"$PID_FILE"
    echo "CLIENT_${i}_PID=$client_pid" >>"$PID_FILE"
    echo "CLIENT_${i}_LOG=$client_log" >>"$PID_FILE"
    echo "CLIENT_${i}_AGENTBRIDGE_PORT=$agentbridge_port" >>"$PID_FILE"

    if [[ "${CLIENT_ENABLE_VNC,,}" == "true" ]]; then
        echo "CLIENT_${i}_VNC_PORT=$vnc_port" >>"$PID_FILE"
        echo "CLIENT_${i}_NOVNC_PORT=$novnc_port" >>"$PID_FILE"
        echo "  ✓ $client_name 已启动 (PID: $client_pid, DISPLAY=:$display_index, agentbridge=$agentbridge_port, noVNC=http://<host>:$novnc_port/vnc.html)"
    else
        echo "  ✓ $client_name 已启动 (PID: $client_pid, DISPLAY=:$display_index, agentbridge=$agentbridge_port)"
    fi

    if [[ "$i" -lt "$CLIENT_COUNT" ]]; then
        sleep "$CLIENT_START_GAP"
    fi
done

echo
SCRIPT_SUCCESS="true"
echo "全部进程已启动（脚本即将退出）"
echo "  PID 文件: $PID_FILE"
echo "  端口登记: $PORT_REGISTRY_FILE"
echo "  查看 server 日志: tail -f $SERVER_LOG"
echo "  查看 client 日志: ls -1 $RUN_LOG_DIR/client-*.log"
