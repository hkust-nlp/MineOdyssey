#!/usr/bin/env bash
##############################################
# 宿主机直接启动 Minecraft 服务端（无容器）
# 支持端口自动分配、IPv6 检测、RCON
#
# 用法:
#   bash scripts/launch/run-server-only-inside.sh
#
# 常用环境变量:
#   SERVER_PORT / RCON_PORT     手动指定端口（留空则自动分配）
#   PORT_AUTO_ALLOCATE=true     是否自动分配端口（默认 true）
#   SERVER_MEMORY_MIN=2G        JVM 最小内存
#   SERVER_MEMORY_MAX=8G        JVM 最大内存
#   ENABLE_RCON=true            启用 RCON
#   RCON_PASSWORD=minecraft     RCON 密码
#   OP_PLAYER=                  管理员玩家名（可选）
#   DIFFICULTY=                 难度: peaceful/easy/normal/hard（留空保持默认）
#   MAX_PLAYERS=                最大玩家数（留空保持默认 20）
#   SERVER_WAIT_TIMEOUT=1800    等待服务端启动超时（秒）
#   SERVER_FOREGROUND=false     设为 true 前台运行（tail -f 日志）
#   SERVER_DATA_DIR             MC 实例目录（默认 $PROJECT_ROOT/server-data）；
#                               指向不同目录可维护多个独立实例（各自的 mods/、
#                               server.properties、world/）。目录不存在会自动创建。
#                               有 world/ 时载入，否则按 LEVEL_SEED 生成新世界。
#   LEVEL_SEED                  新世界种子（留空则随机）；world/ 已存在时忽略。
#   NEOFORGE_SHARED_DIR         共享 NeoForge 安装目录（默认空=每实例独立安装）。
#                               设置后：libraries/、versions/、run.sh 软链到此目录，
#                               每实例可省约 200MB。首次发现该目录未初始化时会
#                               自动安装一次（带文件锁防并发）。
#
# 持久化 / 游戏标签（stage-and-sync，适合集群慢盘如 HDFS）:
#   MCBOTS_PERSISTENT_ROOT      持久化根路径；设置后启用：
#                               - 启动时从 $MCBOTS_PERSISTENT_ROOT/games/$GAME_ID/
#                                 rsync 到本地 $PROJECT_ROOT/staging/games/$GAME_ID/
#                               - 运行中每 $MCBOTS_SYNC_INTERVAL_SEC 秒增量同步回去
#                               - 启动完成后脚本默认返回（后台模式），元信息写入
#                                 $STAGING_ROOT/.game.info 供 stop-server.sh 读取
#   GAME_ID                     游戏标签（默认自动生成 game_YYYYMMDD_HHMMSS，
#                               用来区分多局游戏、在另一台机器上恢复指定游戏）
#   MCBOTS_SYNC_INTERVAL_SEC    周期同步间隔秒数（默认 300）
#
# 干净停止：
#   bash scripts/launch/stop-server.sh <GAME_ID>
#   （会触发最终 rsync --delete，把 staging 完整回写到持久化路径）
#
# 可选参数:
#   --runtime-config <path>     服务端就绪后写入 JSON 配置（含端口等），
#                               供同机客户端自动读取
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
SERVER_MEMORY_MIN="${SERVER_MEMORY_MIN:-2G}"
SERVER_MEMORY_MAX="${SERVER_MEMORY_MAX:-8G}"
MC_VERSION="${MC_VERSION:-1.21.1}"
NEOFORGE_VERSION="${NEOFORGE_VERSION:-21.1.217}"
ENABLE_RCON="${ENABLE_RCON:-true}"
RCON_PASSWORD="${RCON_PASSWORD:-minecraft}"
OP_PLAYER="${OP_PLAYER:-}"
DIFFICULTY="${DIFFICULTY:-}"
MAX_PLAYERS="${MAX_PLAYERS:-}"
SERVER_WAIT_TIMEOUT="${SERVER_WAIT_TIMEOUT:-1800}"
SERVER_FOREGROUND="${SERVER_FOREGROUND:-false}"
LEVEL_SEED="${LEVEL_SEED:-}"
NEOFORGE_SHARED_DIR="${NEOFORGE_SHARED_DIR:-}"

PORT_AUTO_ALLOCATE="${PORT_AUTO_ALLOCATE:-true}"
PORT_REGISTRY_TOOL="${PORT_REGISTRY_TOOL:-$SCRIPT_DIR/../runtime/port_registry.py}"
PORT_REGISTRY_FILE="${PORT_REGISTRY_FILE:-$PROJECT_ROOT/runtime/port-registry.json}"
PORT_RANGE_START="${PORT_RANGE_START:-20000}"
PORT_RANGE_END="${PORT_RANGE_END:-30000}"

BASE_LOG_DIR="${BASE_LOG_DIR:-$PROJECT_ROOT/logs}"
BASE_RUNTIME_DIR="${BASE_RUNTIME_DIR:-$PROJECT_ROOT/runtime}"
SERVER_DATA_DIR="${SERVER_DATA_DIR:-$PROJECT_ROOT/server-data}"

# ── 持久化 / 游戏标签 ──
MCBOTS_PERSISTENT_ROOT="${MCBOTS_PERSISTENT_ROOT:-}"
GAME_ID_RAW="${GAME_ID:-game_$(date +%Y%m%d_%H%M%S)}"
MCBOTS_SYNC_INTERVAL_SEC="${MCBOTS_SYNC_INTERVAL_SEC:-300}"

# 规范化 GAME_ID：路径安全字符之外全部替换为 _
GAME_ID="$(echo "$GAME_ID_RAW" | tr -c 'A-Za-z0-9_-' '_')"

STAGING_ROOT=""
PERSISTENT_DIR=""
GAME_INFO_FILE=""
if [[ -n "$MCBOTS_PERSISTENT_ROOT" ]]; then
    STAGING_ROOT="$PROJECT_ROOT/staging/games/${GAME_ID}"
    PERSISTENT_DIR="$MCBOTS_PERSISTENT_ROOT/games/${GAME_ID}"
    # 覆盖 SERVER_DATA_DIR 指向 staging，Minecraft 就在快盘上跑
    SERVER_DATA_DIR="$STAGING_ROOT/server-data"
    GAME_INFO_FILE="$STAGING_ROOT/.game.info"
fi

ALLOC_OWNERS=()
SERVER_PID=""
SYNC_DAEMON_PID=""
SCRIPT_SUCCESS="false"

# ── 解析参数 ──
RUNTIME_CONFIG_FILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            sed -n '2,/^####/{ /^#/s/^# \?//p }' "$0"
            exit 0
            ;;
        --runtime-config)
            RUNTIME_CONFIG_FILE="$2"
            shift 2
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 1
            ;;
    esac
done

# ── 端口分配工具 ──
run_port_registry() {
    python3 "$PORT_REGISTRY_TOOL" \
        --file "$PORT_REGISTRY_FILE" \
        --range-start "$PORT_RANGE_START" \
        --range-end "$PORT_RANGE_END" \
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

    # 停 Minecraft：给它最多 30s 保存世界
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill_process_tree "$SERVER_PID" TERM
        for _ in $(seq 1 30); do
            kill -0 "$SERVER_PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$SERVER_PID" 2>/dev/null; then
            kill_process_tree "$SERVER_PID" KILL
        fi
    fi

    # 停同步守护进程（手动 rsync 在下面做，不依赖它自己的 trap）
    if [[ -n "${SYNC_DAEMON_PID:-}" ]] && kill -0 "$SYNC_DAEMON_PID" 2>/dev/null; then
        kill -KILL "$SYNC_DAEMON_PID" 2>/dev/null || true
    fi

    # 最终同步：staging → persistent (--delete 确保镜像一致)
    if [[ -n "${PERSISTENT_DIR:-}" ]] && [[ -n "${STAGING_ROOT:-}" ]] && [[ -d "$STAGING_ROOT" ]]; then
        mkdir -p "$PERSISTENT_DIR"
        rsync -a --delete "${STAGING_ROOT}/" "${PERSISTENT_DIR}/" 2>/dev/null || true
    fi

    # 清理标记文件
    if [[ -n "${PERSISTENT_DIR:-}" ]]; then
        rm -f "${PERSISTENT_DIR}/.owner"
    fi
    if [[ -n "${GAME_INFO_FILE:-}" ]]; then
        rm -f "$GAME_INFO_FILE"
    fi

    for owner in "${ALLOC_OWNERS[@]}"; do
        run_port_registry release --owner "$owner" >/dev/null 2>&1 || true
    done
}
trap cleanup_on_exit EXIT INT TERM

# ── 初始化目录 ──
RUN_ID="$(date +%Y%m%d_%H%M%S)_$$"
RUN_LOG_DIR="$BASE_LOG_DIR/$RUN_ID"
PID_FILE="$BASE_RUNTIME_DIR/$RUN_ID.pids"
SERVER_LOG="$RUN_LOG_DIR/server.log"
PORT_OWNER_PREFIX="server-only:${RUN_ID}"

# ── 加载阶段：从持久化路径同步到本地 staging（仅在开启持久化时）──
if [[ -n "$MCBOTS_PERSISTENT_ROOT" ]]; then
    # .owner 警告（非阻塞）—— 提示上次非正常退出或别的节点可能仍占用
    if [[ -f "$PERSISTENT_DIR/.owner" ]]; then
        echo "⚠ 警告: 持久化目录已有 .owner 标记 (上次未正常停止或其他节点正在使用):"
        sed 's/^/    /' "$PERSISTENT_DIR/.owner" 2>/dev/null || true
        echo "  继续启动 (warn-only 策略)"
    fi

    mkdir -p "$STAGING_ROOT" "$PERSISTENT_DIR"

    if [[ -d "$PERSISTENT_DIR/server-data/world" ]]; then
        echo "→ 从持久化路径恢复游戏 '${GAME_ID}' ($PERSISTENT_DIR → $STAGING_ROOT)"
        rsync -a "${PERSISTENT_DIR}/" "${STAGING_ROOT}/"
    else
        echo "→ 开启新游戏 '${GAME_ID}' (持久化至 $PERSISTENT_DIR)"
    fi

    # 上次残留的 session.lock 会阻止启动，清掉
    if [[ -f "$SERVER_DATA_DIR/world/session.lock" ]]; then
        rm -f "$SERVER_DATA_DIR/world/session.lock"
        echo "  已清理残留的 world/session.lock"
    fi

    # 写 .owner 标记到持久化目录（跨节点可见）
    {
        echo "host=$(hostname)"
        echo "pid=$$"
        echo "started=$(date -Iseconds)"
    } > "$PERSISTENT_DIR/.owner"
fi

mkdir -p "$RUN_LOG_DIR" "$BASE_RUNTIME_DIR" "$SERVER_DATA_DIR"
run_port_registry cleanup >/dev/null || true

# ── 分配端口 ──
SERVER_PORT="${SERVER_PORT:-}"
RCON_PORT="${RCON_PORT:-}"
SERVER_PORT_TOKEN=""
RCON_PORT_TOKEN=""

if [[ "${PORT_AUTO_ALLOCATE,,}" == "true" ]]; then
    if [[ -z "$SERVER_PORT" ]]; then
        allocate_one_port "server.game" "${PORT_OWNER_PREFIX}:server" SERVER_PORT SERVER_PORT_TOKEN
    fi
    if [[ "$ENABLE_RCON" == "true" ]] && [[ -z "$RCON_PORT" ]]; then
        allocate_one_port "server.rcon" "${PORT_OWNER_PREFIX}:server" RCON_PORT RCON_PORT_TOKEN
    fi
else
    SERVER_PORT="${SERVER_PORT:-25565}"
    RCON_PORT="${RCON_PORT:-25575}"
fi

echo "=========================================="
echo "  Minecraft 服务端 (inside mode)"
echo "=========================================="
echo "  启动批次: $RUN_ID"
echo "  游戏标签: $GAME_ID"
if [[ "$GAME_ID" != "$GAME_ID_RAW" ]]; then
    echo "    (原始输入 '$GAME_ID_RAW' 已规范化)"
fi
if [[ -n "$MCBOTS_PERSISTENT_ROOT" ]]; then
    echo "  Staging: $STAGING_ROOT"
    echo "  持久化: $PERSISTENT_DIR"
    echo "  同步间隔: ${MCBOTS_SYNC_INTERVAL_SEC}s"
else
    echo "  持久化: 未启用 (未设置 MCBOTS_PERSISTENT_ROOT)"
fi
echo "  日志目录: $RUN_LOG_DIR"
echo "  SERVER_DATA: $SERVER_DATA_DIR"
echo "  游戏端口: $SERVER_PORT"
if [[ "$ENABLE_RCON" == "true" ]]; then
    echo "  RCON 端口: $RCON_PORT"
fi
echo "  内存: ${SERVER_MEMORY_MIN} ~ ${SERVER_MEMORY_MAX}"
echo "=========================================="

# ── 启动 server ──
echo "[1/2] 启动 server..."
(
    cd "$SERVER_DATA_DIR"
    MEMORY_MIN="$SERVER_MEMORY_MIN" \
    MEMORY_MAX="$SERVER_MEMORY_MAX" \
    MC_VERSION="$MC_VERSION" \
    NEOFORGE_VERSION="$NEOFORGE_VERSION" \
    ENABLE_RCON="$ENABLE_RCON" \
    RCON_PASSWORD="$RCON_PASSWORD" \
    SERVER_PORT="$SERVER_PORT" \
    RCON_PORT="${RCON_PORT:-25575}" \
    OP_PLAYER="$OP_PLAYER" \
    DIFFICULTY="$DIFFICULTY" \
    MAX_PLAYERS="$MAX_PLAYERS" \
    LEVEL_SEED="$LEVEL_SEED" \
    NEOFORGE_SHARED_DIR="$NEOFORGE_SHARED_DIR" \
    "$SCRIPT_DIR/../entrypoints/server-entrypoint.sh"
) >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

if [[ -n "$SERVER_PORT_TOKEN" ]]; then
    bind_tokens_to_pid "$SERVER_PID" "$SERVER_PORT_TOKEN" "$RCON_PORT_TOKEN"
    start_token_watcher "$SERVER_PID" "$SERVER_PORT_TOKEN" "$RCON_PORT_TOKEN"
fi

echo "  server PID: $SERVER_PID"
echo "  server log: $SERVER_LOG"

# ── 等待就绪 ──
echo "[2/2] 等待 server 就绪 (timeout=${SERVER_WAIT_TIMEOUT}s)..."
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

# ── 启动后台同步守护进程（仅在持久化启用时）──
if [[ -n "$MCBOTS_PERSISTENT_ROOT" ]]; then
    SYNC_LOG="$RUN_LOG_DIR/sync.log"
    : > "$SYNC_LOG"

    (
        # 守护进程自身的 trap 只负责安静退出；最终 --delete 同步由 stop-server.sh
        # 或 cleanup_on_exit 统一做，避免双方竞争同一个 rsync
        trap 'echo "[$(date -Iseconds)] daemon received signal, exiting"; exit 0' TERM INT
        while true; do
            sleep "$MCBOTS_SYNC_INTERVAL_SEC" & wait $!
            echo "[$(date -Iseconds)] periodic sync"
            rsync -a "${STAGING_ROOT}/" "${PERSISTENT_DIR}/" || true
        done
    ) </dev/null >>"$SYNC_LOG" 2>&1 &
    SYNC_DAEMON_PID=$!
    disown "$SYNC_DAEMON_PID" 2>/dev/null || true
    echo "  ✓ 同步守护进程已启动: PID $SYNC_DAEMON_PID (每 ${MCBOTS_SYNC_INTERVAL_SEC}s)"
    echo "  同步日志: $SYNC_LOG"

    # 写入 .game.info —— stop-server.sh 读这里找 PIDs 和路径
    {
        echo "GAME_ID=$GAME_ID"
        echo "RUN_ID=$RUN_ID"
        echo "SERVER_PID=$SERVER_PID"
        echo "SYNC_DAEMON_PID=$SYNC_DAEMON_PID"
        echo "STAGING_ROOT=$STAGING_ROOT"
        echo "PERSISTENT_DIR=$PERSISTENT_DIR"
        echo "SERVER_PORT=$SERVER_PORT"
        echo "RCON_PORT=${RCON_PORT:-}"
        echo "RCON_PASSWORD=${RCON_PASSWORD:-}"
        echo "ENABLE_RCON=${ENABLE_RCON:-false}"
        echo "RUN_LOG_DIR=$RUN_LOG_DIR"
        echo "SYNC_LOG=$SYNC_LOG"
        echo "HOSTNAME=$(hostname)"
        echo "STARTED_AT=$(date -Iseconds)"
    } > "$GAME_INFO_FILE"
    echo "  ✓ 元信息已写入: $GAME_INFO_FILE"
fi

# ── 写入 PID 文件 ──
{
    echo "RUN_ID=$RUN_ID"
    echo "SERVER_PID=$SERVER_PID"
    echo "SERVER_PORT=$SERVER_PORT"
    echo "RCON_PORT=${RCON_PORT:-}"
    echo "PORT_REGISTRY_FILE=$PORT_REGISTRY_FILE"
} >"$PID_FILE"

# ── 写入 runtime config（可选）──
if [[ -n "$RUNTIME_CONFIG_FILE" ]]; then
    mkdir -p "$(dirname "$RUNTIME_CONFIG_FILE")"
    cat > "$RUNTIME_CONFIG_FILE" <<EOF
{
  "server": {
    "host": "127.0.0.1",
    "port": ${SERVER_PORT}
  },
  "rcon": {
    "host": "127.0.0.1",
    "port": ${RCON_PORT:-0},
    "password": "${RCON_PASSWORD}"
  },
  "run_id": "${RUN_ID}",
  "server_pid": ${SERVER_PID}
}
EOF
    echo "  ✓ runtime config 已写入: $RUNTIME_CONFIG_FILE"
fi

SCRIPT_SUCCESS="true"

echo ""
echo "=========================================="
echo "  ✓ 服务端已就绪"
echo "=========================================="
echo "  PID 文件: $PID_FILE"
echo "  查看日志: tail -f $SERVER_LOG"
if [[ "$ENABLE_RCON" == "true" ]]; then
    echo "  RCON 连接: mcrcon -H 127.0.0.1 -P $RCON_PORT -p $RCON_PASSWORD"
fi
if [[ -n "$MCBOTS_PERSISTENT_ROOT" ]]; then
    echo ""
    echo "  干净停止 (会做最终 rsync --delete 回持久化路径):"
    echo "    bash scripts/launch/stop-server.sh ${GAME_ID}"
    echo ""
    echo "  在另一节点恢复此游戏:"
    echo "    GAME_ID=${GAME_ID} MCBOTS_PERSISTENT_ROOT=${MCBOTS_PERSISTENT_ROOT} \\"
    echo "      bash scripts/launch/run-server-only-inside.sh"
fi

if [[ "${SERVER_FOREGROUND,,}" == "true" ]]; then
    echo ""
    echo "前台模式，按 Ctrl+C 退出..."
    SCRIPT_SUCCESS="false"   # 让 cleanup 可以杀进程
    tail -f "$SERVER_LOG" &
    wait "$SERVER_PID" || true
fi
