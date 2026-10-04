#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Auto-detect IPv4 connectivity; if unavailable, tell Java to prefer IPv6.
if ! curl -4 -s --connect-timeout 3 https://maven.neoforged.net -o /dev/null 2>/dev/null; then
    echo "[net] IPv4 unreachable, enabling Java IPv6 mode"
    _JAVA_IPV6_OPTS="-Djava.net.preferIPv4Stack=false -Djava.net.preferIPv6Addresses=true"
    export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:+${JAVA_TOOL_OPTIONS} }${_JAVA_IPV6_OPTS}"
fi

make_task_slug() {
    local value="$1"
    printf '%s' "$value" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-{2,}/-/g'
}

TASK_CONFIG="${TASK_CONFIG:-$PROJECT_ROOT/eval/openha_assets/kill_entity_min.json}"
TASK_NAME="${TASK_NAME:-kill_entity:sheep}"
TASK_SLUG="${TASK_SLUG:-$(make_task_slug "$TASK_NAME")}"
RUN_TS="${RUN_TS:-$(date +%Y%m%d%H%M%S)}"
export MCBOTS_SINGLE_TASK_RESULT_LAYOUT="${MCBOTS_SINGLE_TASK_RESULT_LAYOUT:-single_task_dir}"
MCBOTS_SINGLE_TASK_RESULT_LAYOUT="${MCBOTS_SINGLE_TASK_RESULT_LAYOUT,,}"
BOT_NAME="${BOT_NAME:-bot-${TASK_SLUG}-${RUN_TS}}"
PLAYER_NAME="${PLAYER_NAME:-bot}"
RUNNER_PATH="${RUNNER_PATH:-$PROJECT_ROOT/eval/kill_sheep_eval_runner.py}"
TASK_RUNTIME_DIR="${TASK_RUNTIME_DIR:-$PROJECT_ROOT/eval/runtime/tasks/${BOT_NAME}-p$$}"

SERVER_DATA_DIR="${SERVER_DATA_DIR:-${TASK_RUNTIME_DIR}/server-data}"
SERVER_MODS_DIR="${SERVER_MODS_DIR:-${TASK_RUNTIME_DIR}/server-mods}"
SERVER_WORLD_DIR="${SERVER_WORLD_DIR:-${SERVER_DATA_DIR}/world}"
SERVER_ENTRYPOINT="${SERVER_ENTRYPOINT:-$SCRIPTS_ROOT/entrypoints/server-entrypoint.sh}"
SERVER_LOG_FILE="${SERVER_LOG_FILE:-${TASK_RUNTIME_DIR}/eval_server.log}"
SERVER_PID_FILE="${SERVER_PID_FILE:-${TASK_RUNTIME_DIR}/eval_server.pid}"

TEMPLATE_WORLD_DIR="${TEMPLATE_WORLD_DIR:-$PROJECT_ROOT/eval/templates/template_1_21_1/world}"
OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_ROOT/eval/results}"

MC_VERSION="${MC_VERSION:-1.21.1}"
NEOFORGE_VERSION="${NEOFORGE_VERSION:-21.1.217}"
MEMORY_MIN="${MEMORY_MIN:-2G}"
MEMORY_MAX="${MEMORY_MAX:-8G}"
RCON_PASSWORD="${RCON_PASSWORD:-minecraft}"
RCON_HOST="${RCON_HOST:-127.0.0.1}"
RCON_PORT="${RCON_PORT:-}"
SERVER_PORT="${SERVER_PORT:-}"

TASK_TIMEOUT_SEC="${TASK_TIMEOUT_SEC:-120}"
JUDGE_INTERVAL_SEC="${JUDGE_INTERVAL_SEC:-0.5}"
WAIT_RCON_TIMEOUT_SEC="${WAIT_RCON_TIMEOUT_SEC:-180}"
WAIT_PLAYER_TIMEOUT_SEC="${WAIT_PLAYER_TIMEOUT_SEC:-120}"
EVAL_CLIENT_COORDINATOR_EXTRA_WAIT_SEC="${EVAL_CLIENT_COORDINATOR_EXTRA_WAIT_SEC:-180}"
INIT_WEATHER="${INIT_WEATHER:-keep}"
INIT_MOB_SPAWNING="${INIT_MOB_SPAWNING:-keep}"
INIT_TIME="${INIT_TIME:-}"
INIT_CLEAR_EXISTING_HOSTILES="${INIT_CLEAR_EXISTING_HOSTILES:-false}"
INIT_EQUIP_DISTRACTION_MODE="${INIT_EQUIP_DISTRACTION_MODE:-off}"
INIT_EQUIP_DISTRACTION_LEVEL="${INIT_EQUIP_DISTRACTION_LEVEL:-normal}"
INIT_EQUIP_DISTRACTION_FIXED_JSON="${INIT_EQUIP_DISTRACTION_FIXED_JSON:-}"
INIT_EQUIP_DISTRACTION_RANDOM_HEAD_CANDIDATES="${INIT_EQUIP_DISTRACTION_RANDOM_HEAD_CANDIDATES:-}"
INIT_INVENTORY_DISTRACTION_MODE="${INIT_INVENTORY_DISTRACTION_MODE:-off}"
INIT_INVENTORY_DISTRACTION_LEVEL="${INIT_INVENTORY_DISTRACTION_LEVEL:-normal}"
INIT_WEATHER="${INIT_WEATHER,,}"
INIT_MOB_SPAWNING="${INIT_MOB_SPAWNING,,}"
INIT_CLEAR_EXISTING_HOSTILES="${INIT_CLEAR_EXISTING_HOSTILES,,}"
INIT_EQUIP_DISTRACTION_MODE="${INIT_EQUIP_DISTRACTION_MODE,,}"
INIT_EQUIP_DISTRACTION_LEVEL="${INIT_EQUIP_DISTRACTION_LEVEL,,}"
INIT_INVENTORY_DISTRACTION_MODE="${INIT_INVENTORY_DISTRACTION_MODE,,}"
INIT_INVENTORY_DISTRACTION_LEVEL="${INIT_INVENTORY_DISTRACTION_LEVEL,,}"

SKIP_AGENT="${SKIP_AGENT:-true}"
SKIP_PLAYER_WAIT="${SKIP_PLAYER_WAIT:-false}"
SKIP_WORLD_RESTORE="${SKIP_WORLD_RESTORE:-false}"
NO_SERVER_RESTART="${NO_SERVER_RESTART:-false}"
DRY_RUN="${DRY_RUN:-false}"
STOP_SERVER_ON_EXIT="${STOP_SERVER_ON_EXIT:-true}"
STOP_CLIENT_ON_EXIT="${STOP_CLIENT_ON_EXIT:-true}"

mkdir -p "$(dirname "$SERVER_LOG_FILE")" "$OUTPUT_DIR"

START_EVAL_CLIENT="${START_EVAL_CLIENT:-true}"
EVAL_CLIENT_ID_RAW="${EVAL_CLIENT_ID:-${BOT_NAME}-p$$}"
EVAL_CLIENT_ID="$(make_task_slug "$EVAL_CLIENT_ID_RAW")"
if [[ -z "$EVAL_CLIENT_ID" ]]; then
    EVAL_CLIENT_ID="eval-client-$$"
fi
EVAL_CLIENT_PID_FILE="${EVAL_CLIENT_PID_FILE:-$PROJECT_ROOT/eval/runtime/eval-client-${EVAL_CLIENT_ID}.pid}"
EVAL_CLIENT_LOG_FILE="${EVAL_CLIENT_LOG_FILE:-${TASK_RUNTIME_DIR}/client.log}"
EVAL_WORKSPACE_ROOT="${EVAL_WORKSPACE_ROOT:-$PROJECT_ROOT/workspaces/eval/${BOT_NAME}}"
EVAL_CLIENT_GAME_DIR="${EVAL_CLIENT_GAME_DIR:-${EVAL_WORKSPACE_ROOT}/game}"
SHARED_CLIENT_TEMPLATE_DIR="${SHARED_CLIENT_TEMPLATE_DIR:-$PROJECT_ROOT/eval/templates/client_game}"
EVAL_CLIENT_TEMPLATE_MODE="${EVAL_CLIENT_TEMPLATE_MODE:-required}"
EVAL_CLIENT_TEMPLATE_MODE="${EVAL_CLIENT_TEMPLATE_MODE,,}"
SHARED_SERVER_DATA_TEMPLATE_DIR="${SHARED_SERVER_DATA_TEMPLATE_DIR:-$PROJECT_ROOT/eval/templates/server-data-neoforge-1.21.1}"
EVAL_SERVER_DATA_TEMPLATE_MODE="${EVAL_SERVER_DATA_TEMPLATE_MODE:-required}"
EVAL_SERVER_DATA_TEMPLATE_MODE="${EVAL_SERVER_DATA_TEMPLATE_MODE,,}"
EVAL_CLIENT_RENDER_MODE="${EVAL_CLIENT_RENDER_MODE:-cpu}"
EVAL_CLIENT_DISPLAY_INDEX="${EVAL_CLIENT_DISPLAY_INDEX:-}"
EVAL_CLIENT_DISPLAY_RESOLUTION="${EVAL_CLIENT_DISPLAY_RESOLUTION:-800x600x24}"
EVAL_CLIENT_ENABLE_VNC="${EVAL_CLIENT_ENABLE_VNC:-false}"
EVAL_CLIENT_VNC_VIEWONLY="${EVAL_CLIENT_VNC_VIEWONLY:-false}"
EVAL_CLIENT_ENABLE_REMOTE_BASH="${EVAL_CLIENT_ENABLE_REMOTE_BASH:-true}"
EVAL_AGENTBRIDGE_PORT="${EVAL_AGENTBRIDGE_PORT:-}"
EVAL_REMOTE_BASH_PORT="${EVAL_REMOTE_BASH_PORT:-}"
EVAL_VNC_PORT="${EVAL_VNC_PORT:-}"
EVAL_NOVNC_PORT="${EVAL_NOVNC_PORT:-}"

API_MODELS_CONFIG="${API_MODELS_CONFIG:-$PROJECT_ROOT/config/api_models.json}"
API_MODEL_ALIAS="${API_MODEL_ALIAS:-gemini3flash}"
AUTO_LOAD_API_MODEL="${AUTO_LOAD_API_MODEL:-true}"
WORKSPACE_RUNTIME_CONFIG="${WORKSPACE_RUNTIME_CONFIG:-${EVAL_WORKSPACE_ROOT}/.mcbots_runtime.json}"
AGENT_INSTRUCTION="${AGENT_INSTRUCTION:-}"
TASK_INSTRUCTION_FILE="${TASK_INSTRUCTION_FILE:-$PROJECT_ROOT/eval/openha_assets/instructions.json}"
TASK_INSTRUCTION_MODE="${TASK_INSTRUCTION_MODE:-first}"

PORT_AUTO_ALLOCATE="${PORT_AUTO_ALLOCATE:-true}"
PORT_REGISTRY_TOOL="${PORT_REGISTRY_TOOL:-$SCRIPTS_ROOT/runtime/port_registry.py}"
PORT_REGISTRY_FILE="${PORT_REGISTRY_FILE:-$PROJECT_ROOT/runtime/port-registry.json}"
PORT_RANGE_START="${PORT_RANGE_START:-20000}"
PORT_RANGE_END="${PORT_RANGE_END:-30000}"
DISPLAY_REGISTRY_TOOL="${DISPLAY_REGISTRY_TOOL:-$SCRIPTS_ROOT/runtime/display_registry.py}"
DISPLAY_REGISTRY_FILE="${DISPLAY_REGISTRY_FILE:-$PROJECT_ROOT/runtime/display-registry.json}"
DISPLAY_RANGE_START="${DISPLAY_RANGE_START:-200}"
DISPLAY_RANGE_END="${DISPLAY_RANGE_END:-2200}"
DISPLAY_SCAN_LIMIT="${DISPLAY_SCAN_LIMIT:-1024}"

single_task_layout_enabled() {
    case "${MCBOTS_SINGLE_TASK_RESULT_LAYOUT:-}" in
        single_task_summary|single_task_summary_dir|single_task_dir|summary_dir)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

single_task_run_dir() {
    local run_id="$1"
    local run_root="${MCBOTS_SINGLE_TASK_RUN_ROOT:-single_tasks}"
    if [[ -z "$run_root" ]]; then
        run_root="single_tasks"
    fi
    if [[ "$run_root" == "." || "$run_root" == "./" ]]; then
        printf '%s\n' "$OUTPUT_DIR/$run_id"
        return
    fi
    if [[ "$run_root" = /* ]]; then
        printf '%s\n' "$run_root/$run_id"
        return
    fi
    printf '%s\n' "$OUTPUT_DIR/$run_root/$run_id"
}

normalize_run_ts_for_result() {
    local value="$1"
    if [[ "$value" =~ ^([0-9]{8})_?([0-9]{6})$ ]]; then
        printf '%s_%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    else
        date +%Y%m%d_%H%M%S
    fi
}

if single_task_layout_enabled; then
    if [[ -z "${MCBOTS_SINGLE_TASK_RUN_ID:-}" ]]; then
        MCBOTS_SINGLE_TASK_RUN_ID="$(normalize_run_ts_for_result "$RUN_TS")_${TASK_NAME//:/_}"
    fi
    export MCBOTS_SINGLE_TASK_RUN_ID
fi

is_pid_running() {
    local pid="$1"
    if [[ -z "$pid" ]]; then
        return 1
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    local proc_state
    proc_state="$(ps -o stat= -p "$pid" 2>/dev/null | awk '{print $1}' || true)"
    if [[ "$proc_state" == Z* ]]; then
        return 1
    fi
    return 0
}

kill_process_tree() {
    local root_pid="$1"
    local signal_name="$2"
    local child_pid
    local child_pids

    # `ps --ppid` returns non-zero when there are no children; tolerate that under `set -e`.
    child_pids="$(ps -o pid= --ppid "$root_pid" 2>/dev/null | tr -d ' ' || true)"
    for child_pid in $child_pids; do
        [[ -n "$child_pid" ]] || continue
        kill_process_tree "$child_pid" "$signal_name"
    done

    kill "-${signal_name}" "$root_pid" 2>/dev/null || true
}

stop_stale_remote_bash_processes() {
    local remote_port="${MCBOTS_REMOTE_BASH_PORT:-}"
    if [[ -z "$remote_port" ]]; then
        return 0
    fi
    local stale_pids
    stale_pids="$(
        ps -eo pid,args \
            | awk -v p="$remote_port" '
                $0 ~ /python3 .*remote_bash_server.py/ && $0 ~ (" " p "$") { print $1 }
            '
    )"

    local pid
    for pid in $stale_pids; do
        if is_pid_running "$pid"; then
            echo "Stopping stale remote bash pid=${pid} port=${remote_port}"
            kill_process_tree "$pid" TERM
            sleep 0.2
            if is_pid_running "$pid"; then
                kill_process_tree "$pid" KILL
            fi
        fi
    done
}

PENDING_RELEASE_TOKENS=()
PORT_OWNER="eval:${BOT_NAME}:${RUN_TS}:$$"
SERVER_PORT_TOKEN=""
RCON_PORT_TOKEN=""
EVAL_AGENTBRIDGE_TOKEN=""
EVAL_REMOTE_BASH_TOKEN=""
EVAL_VNC_TOKEN=""
EVAL_NOVNC_TOKEN=""
EVAL_DISPLAY_TOKEN=""
SERVER_TOKEN_BINDER_PID=""
SERVER_TOKEN_WATCH_STARTED="false"
PENDING_DISPLAY_TOKENS=()

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
    local __port_var="$2"
    local __token_var="$3"
    local out line
    out="$(run_port_registry allocate --count 1 --service "$service" --owner "$PORT_OWNER" --pid "$$")"
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
    PENDING_RELEASE_TOKENS+=("${line##*|}")
}

allocate_one_display() {
    local service="$1"
    local preferred="$2"
    local __display_var="$3"
    local __token_var="$4"
    local out line
    out="$(
        run_display_registry allocate \
            --count 1 \
            --service "$service" \
            --owner "$PORT_OWNER" \
            --pid "$$" \
            --preferred "$preferred" \
            --scan-limit "$DISPLAY_SCAN_LIMIT"
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
    PENDING_DISPLAY_TOKENS+=("${line##*|}")
}

drop_pending_token() {
    local token="$1"
    [[ -z "$token" ]] && return
    local kept=()
    local t
    for t in "${PENDING_RELEASE_TOKENS[@]}"; do
        if [[ "$t" != "$token" ]]; then
            kept+=("$t")
        fi
    done
    PENDING_RELEASE_TOKENS=("${kept[@]}")
}

drop_pending_display_token() {
    local token="$1"
    [[ -z "$token" ]] && return
    local kept=()
    local t
    for t in "${PENDING_DISPLAY_TOKENS[@]}"; do
        if [[ "$t" != "$token" ]]; then
            kept+=("$t")
        fi
    done
    PENDING_DISPLAY_TOKENS=("${kept[@]}")
}

release_pending_ports() {
    if [[ "${#PENDING_RELEASE_TOKENS[@]}" -eq 0 ]]; then
        return
    fi
    local snapshot
    if ! snapshot="$(run_port_registry list 2>/dev/null)"; then
        snapshot=""
    fi

    local releasable
    releasable="$(
        python3 - "$$" "$snapshot" "${PENDING_RELEASE_TOKENS[@]}" <<'PY'
import json
import sys

owner_pid = int(sys.argv[1])
raw = sys.argv[2]
tokens = sys.argv[3:]

if not raw.strip():
    raise SystemExit(0)

try:
    data = json.loads(raw)
except Exception:
    raise SystemExit(0)

alloc = data.get("allocations", {})
by_token = {}
for entry in alloc.values():
    if isinstance(entry, dict):
        tok = entry.get("token")
        if isinstance(tok, str) and tok:
            by_token[tok] = entry

for tok in tokens:
    entry = by_token.get(tok)
    if not isinstance(entry, dict):
        continue
    pid = int(entry.get("pid", 0) or 0)
    if pid <= 0 or pid == owner_pid:
        print(tok)
PY
    )"

    local tok
    while IFS= read -r tok; do
        [[ -n "$tok" ]] || continue
        run_port_registry release --owner "$PORT_OWNER" --token "$tok" >/dev/null 2>&1 || true
    done <<<"$releasable"

    PENDING_RELEASE_TOKENS=()
}

release_pending_displays() {
    if [[ "${#PENDING_DISPLAY_TOKENS[@]}" -eq 0 ]]; then
        return
    fi
    local snapshot
    if ! snapshot="$(run_display_registry list 2>/dev/null)"; then
        snapshot=""
    fi

    local releasable
    releasable="$(
        python3 - "$$" "$snapshot" "${PENDING_DISPLAY_TOKENS[@]}" <<'PY'
import json
import sys

owner_pid = int(sys.argv[1])
raw = sys.argv[2]
tokens = sys.argv[3:]

if not raw.strip():
    raise SystemExit(0)

try:
    data = json.loads(raw)
except Exception:
    raise SystemExit(0)

alloc = data.get("allocations", {})
by_token = {}
for entry in alloc.values():
    if isinstance(entry, dict):
        tok = entry.get("token")
        if isinstance(tok, str) and tok:
            by_token[tok] = entry

for tok in tokens:
    entry = by_token.get(tok)
    if not isinstance(entry, dict):
        continue
    pid = int(entry.get("pid", 0) or 0)
    if pid <= 0 or pid == owner_pid:
        print(tok)
PY
    )"

    local tok
    while IFS= read -r tok; do
        [[ -n "$tok" ]] || continue
        run_display_registry release --owner "$PORT_OWNER" --token "$tok" >/dev/null 2>&1 || true
    done <<<"$releasable"

    PENDING_DISPLAY_TOKENS=()
}

bind_tokens_to_pid() {
    local pid="$1"
    shift
    local args=(bind --pid "$pid")
    local tok
    for tok in "$@"; do
        [[ -n "$tok" ]] && args+=(--token "$tok")
    done
    if run_port_registry "${args[@]}" >/dev/null 2>&1; then
        local tok
        for tok in "$@"; do
            drop_pending_token "$tok"
        done
        return 0
    fi
    return 1
}

bind_display_tokens_to_pid() {
    local pid="$1"
    shift
    local args=(bind --pid "$pid")
    local tok
    for tok in "$@"; do
        [[ -n "$tok" ]] && args+=(--token "$tok")
    done
    if run_display_registry "${args[@]}" >/dev/null 2>&1; then
        local tok
        for tok in "$@"; do
            drop_pending_display_token "$tok"
        done
        return 0
    fi
    return 1
}

start_token_watcher() {
    local pid="$1"
    shift
    local args=("$PORT_REGISTRY_TOOL" --file "$PORT_REGISTRY_FILE" --range-start "$PORT_RANGE_START" --range-end "$PORT_RANGE_END" watch --pid "$pid")
    local tok
    for tok in "$@"; do
        [[ -n "$tok" ]] && args+=(--token "$tok")
    done
    nohup python3 "${args[@]}" >/dev/null 2>&1 &
}

start_display_token_watcher() {
    local pid="$1"
    shift
    local args=("$DISPLAY_REGISTRY_TOOL" --file "$DISPLAY_REGISTRY_FILE" --range-start "$DISPLAY_RANGE_START" --range-end "$DISPLAY_RANGE_END" watch --pid "$pid")
    local tok
    for tok in "$@"; do
        [[ -n "$tok" ]] && args+=(--token "$tok")
    done
    nohup python3 "${args[@]}" >/dev/null 2>&1 &
}

read_pid_file() {
    local pid_file="$1"
    if [[ ! -f "$pid_file" ]]; then
        return
    fi
    tr -d '[:space:]' < "$pid_file" 2>/dev/null || true
}

bind_server_tokens_if_possible() {
    local server_pid
    server_pid="$(read_pid_file "$SERVER_PID_FILE")"
    if [[ -z "$server_pid" ]] || ! [[ "$server_pid" =~ ^[0-9]+$ ]]; then
        return 1
    fi
    if ! is_pid_running "$server_pid"; then
        return 1
    fi

    if bind_tokens_to_pid "$server_pid" "$SERVER_PORT_TOKEN" "$RCON_PORT_TOKEN"; then
        if [[ "$SERVER_TOKEN_WATCH_STARTED" != "true" ]]; then
            start_token_watcher "$server_pid" "$SERVER_PORT_TOKEN" "$RCON_PORT_TOKEN"
            SERVER_TOKEN_WATCH_STARTED="true"
        fi
        return 0
    fi
    return 1
}

start_server_token_binder() {
    if [[ -z "$SERVER_PORT_TOKEN" && -z "$RCON_PORT_TOKEN" ]]; then
        return
    fi

    local initial_pid=""
    if [[ "${NO_SERVER_RESTART,,}" != "true" ]]; then
        initial_pid="$(read_pid_file "$SERVER_PID_FILE")"
    fi
    local wait_ready_timeout="${WAIT_RCON_TIMEOUT_SEC%.*}"
    if ! [[ "$wait_ready_timeout" =~ ^[0-9]+$ ]]; then
        wait_ready_timeout=180
    fi

    local coordinator_extra_wait="${EVAL_CLIENT_COORDINATOR_EXTRA_WAIT_SEC%.*}"
    if ! [[ "$coordinator_extra_wait" =~ ^[0-9]+$ ]]; then
        coordinator_extra_wait=180
    fi

    (
        local deadline=$((SECONDS + wait_ready_timeout + coordinator_extra_wait))
        while [[ "$SECONDS" -lt "$deadline" ]]; do
            local current_pid
            current_pid="$(read_pid_file "$SERVER_PID_FILE")"
            if [[ -n "$current_pid" ]] && [[ "$current_pid" =~ ^[0-9]+$ ]] && is_pid_running "$current_pid"; then
                if [[ "${NO_SERVER_RESTART,,}" == "true" || -z "$initial_pid" || "$current_pid" != "$initial_pid" ]]; then
                    if bind_tokens_to_pid "$current_pid" "$SERVER_PORT_TOKEN" "$RCON_PORT_TOKEN"; then
                        start_token_watcher "$current_pid" "$SERVER_PORT_TOKEN" "$RCON_PORT_TOKEN"
                        echo "Bound eval server ports to pid=${current_pid} (server=${SERVER_PORT}, rcon=${RCON_PORT})"
                        exit 0
                    fi
                fi
            fi
            sleep 1
        done
        echo "WARN: failed to bind eval server tokens in time; tokens may be released at script exit"
    ) < /dev/null >/dev/null 2>&1 &
    SERVER_TOKEN_BINDER_PID="$!"
}

auto_assign_ports() {
    if [[ "${PORT_AUTO_ALLOCATE,,}" != "true" ]]; then
        echo "ERROR: 当前脚本仅支持 PORT_AUTO_ALLOCATE=true（所有端口必须统一登记管理）"
        exit 1
    fi

    if [[ ! -x "$PORT_REGISTRY_TOOL" ]]; then
        echo "ERROR: 端口分配工具不存在或不可执行: $PORT_REGISTRY_TOOL"
        exit 1
    fi
    if [[ ! -f "$DISPLAY_REGISTRY_TOOL" ]]; then
        echo "ERROR: DISPLAY 分配工具不存在: $DISPLAY_REGISTRY_TOOL"
        exit 1
    fi

    run_port_registry cleanup >/dev/null || true
    run_display_registry cleanup >/dev/null || true

    allocate_one_port "eval.server.game" SERVER_PORT SERVER_PORT_TOKEN
    allocate_one_port "eval.server.rcon" RCON_PORT RCON_PORT_TOKEN
    allocate_one_port "eval.client.agentbridge" EVAL_AGENTBRIDGE_PORT EVAL_AGENTBRIDGE_TOKEN
    if [[ "${EVAL_CLIENT_ENABLE_REMOTE_BASH,,}" == "true" ]]; then
        allocate_one_port "eval.client.remotebash" EVAL_REMOTE_BASH_PORT EVAL_REMOTE_BASH_TOKEN
    else
        EVAL_REMOTE_BASH_PORT=""
    fi
    if [[ "${EVAL_CLIENT_ENABLE_VNC,,}" == "true" ]]; then
        allocate_one_port "eval.client.vnc" EVAL_VNC_PORT EVAL_VNC_TOKEN
        allocate_one_port "eval.client.novnc" EVAL_NOVNC_PORT EVAL_NOVNC_TOKEN
    fi

    local preferred_display=""
    if [[ -n "$EVAL_CLIENT_DISPLAY_INDEX" ]]; then
        preferred_display="$EVAL_CLIENT_DISPLAY_INDEX"
    else
        preferred_display="$((EVAL_AGENTBRIDGE_PORT - PORT_RANGE_START + 200))"
    fi
    if ! allocate_one_display "eval.client.display" "$preferred_display" EVAL_CLIENT_DISPLAY_INDEX EVAL_DISPLAY_TOKEN; then
        echo "ERROR: failed to allocate DISPLAY index. preferred=${preferred_display}, scan_limit=${DISPLAY_SCAN_LIMIT}"
        exit 1
    fi
    if [[ "$EVAL_CLIENT_DISPLAY_INDEX" != "$preferred_display" ]]; then
        echo "WARN: display :${preferred_display} busy, fallback to :${EVAL_CLIENT_DISPLAY_INDEX}"
    fi
}

mask_secret() {
    local value="$1"
    if [[ -z "$value" ]]; then
        echo "<empty>"
        return
    fi
    local n="${#value}"
    if (( n <= 8 )); then
        echo "***"
        return
    fi
    echo "***${value: -6}"
}

load_api_model_profile() {
    local config_path="$1"
    local alias="$2"

    if [[ ! -f "$config_path" ]]; then
        echo "WARN: API model config not found: ${config_path}"
        return 1
    fi

    local parsed
    if ! parsed="$(
        python3 - "$config_path" "$alias" <<'PY'
import json
import sys

path, alias = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as f:
    cfg_all = json.load(f)
cfg = cfg_all.get(alias)
if not isinstance(cfg, dict):
    sys.exit(2)
print(cfg.get("base_url", ""))
print(cfg.get("api_key", ""))
print(cfg.get("modelname", ""))
model_params = cfg.get("model_params", {})
if isinstance(model_params, dict):
    print(json.dumps(model_params, ensure_ascii=False, separators=(",", ":")))
else:
    print("")
PY
    )"; then
        echo "WARN: failed to load profile '${alias}' from ${config_path}"
        return 1
    fi

    local fields=()
    mapfile -t fields <<<"$parsed"
    local cfg_base_url="${fields[0]:-}"
    local cfg_api_key="${fields[1]:-}"
    local cfg_model="${fields[2]:-}"
    local cfg_model_params_json="${fields[3]:-}"

    if [[ -z "${MCBOTS_BASE_URL:-}" && -n "$cfg_base_url" ]]; then
        export MCBOTS_BASE_URL="$cfg_base_url"
    fi
    if [[ -z "${MCBOTS_MODEL:-}" && -n "$cfg_model" ]]; then
        export MCBOTS_MODEL="$cfg_model"
    fi
    if [[ -z "${MCBOTS_API_KEY:-}" && -z "${OPENROUTER_API_KEY:-}" && -n "$cfg_api_key" ]]; then
        export MCBOTS_API_KEY="$cfg_api_key"
    fi
    if [[ -z "${MCBOTS_MODEL_PARAMS_JSON:-}" && -n "$cfg_model_params_json" && "$cfg_model_params_json" != "{}" ]]; then
        export MCBOTS_MODEL_PARAMS_JSON="$cfg_model_params_json"
    fi
    return 0
}

prepare_agent_runtime_env() {
    if [[ "${SKIP_AGENT,,}" == "true" ]]; then
        return
    fi

    if [[ -z "${AGENT_CMD:-}" ]]; then
        AGENT_CMD="python3 -m agent.main"
    fi

    if [[ "${AUTO_LOAD_API_MODEL,,}" == "true" ]]; then
        load_api_model_profile "$API_MODELS_CONFIG" "$API_MODEL_ALIAS" || true
    fi

    export MCBOTS_PLAYER="${MCBOTS_PLAYER:-$PLAYER_NAME}"
    export MCBOTS_AGENT_CONTAINER_NAME="${MCBOTS_AGENT_CONTAINER_NAME:-$BOT_NAME}"
    export MCBOTS_SERVER_NAME="${MCBOTS_SERVER_NAME:-mc-openha-eval-server}"
    export MCBOTS_REMOTE_BASH_HOST="${MCBOTS_REMOTE_BASH_HOST:-127.0.0.1}"
    export MCBOTS_REMOTE_BASH_PORT="${MCBOTS_REMOTE_BASH_PORT:-$EVAL_REMOTE_BASH_PORT}"
    export MCBOTS_DISPLAY="${MCBOTS_DISPLAY:-:${EVAL_CLIENT_DISPLAY_INDEX}}"
    export MCBOTS_WORKSPACE_ROOT="${MCBOTS_WORKSPACE_ROOT:-$EVAL_WORKSPACE_ROOT}"
    export MCBOTS_RUNTIME_CONFIG="${MCBOTS_RUNTIME_CONFIG:-$WORKSPACE_RUNTIME_CONFIG}"
    export MCBOTS_LOG_FILE_PATH="${MCBOTS_LOG_FILE_PATH:-${EVAL_CLIENT_GAME_DIR}/logs/latest.log}"
    export MCBOTS_ACTION_DOC_PATH="${MCBOTS_ACTION_DOC_PATH:-${EVAL_WORKSPACE_ROOT}/docs/ACTION-SPACE-REFERENCE.md}"
    if single_task_layout_enabled; then
        local single_task_dir
        single_task_dir="$(single_task_run_dir "$MCBOTS_SINGLE_TASK_RUN_ID")"
        export MCBOTS_RECORD_DIR="${MCBOTS_RECORD_DIR:-$single_task_dir/records}"
    fi
    export MCBOTS_RECORD_ROOT="${MCBOTS_RECORD_ROOT:-$OUTPUT_DIR}"
    export MCBOTS_RECORD_VIDEO="${MCBOTS_RECORD_VIDEO:-true}"
    export MCBOTS_VIDEO_FPS="${MCBOTS_VIDEO_FPS:-15}"
    export MCBOTS_VIDEO_CRF="${MCBOTS_VIDEO_CRF:-30}"

    mkdir -p "$(dirname "$WORKSPACE_RUNTIME_CONFIG")"
    local runtime_remote_bash_port_json
    if [[ -n "${MCBOTS_REMOTE_BASH_PORT:-}" ]]; then
        runtime_remote_bash_port_json="${MCBOTS_REMOTE_BASH_PORT}"
    else
        runtime_remote_bash_port_json="null"
    fi
    cat >"$WORKSPACE_RUNTIME_CONFIG" <<EOF
{
  "bot_name": "${BOT_NAME}",
  "workspace_root": "${EVAL_WORKSPACE_ROOT}",
  "agentbridge": {
    "host": "127.0.0.1",
    "port": ${EVAL_AGENTBRIDGE_PORT}
  },
  "remote_bash": {
    "host": "${MCBOTS_REMOTE_BASH_HOST}",
    "port": ${runtime_remote_bash_port_json}
  },
  "x11": {
    "display": "${MCBOTS_DISPLAY}",
    "resolution": "${EVAL_CLIENT_DISPLAY_RESOLUTION%x*}"
  }
}
EOF

    if [[ -z "${MCBOTS_MODEL:-}" ]]; then
        echo "ERROR: missing model config. Set MCBOTS_MODEL or API_MODEL_ALIAS/API_MODELS_CONFIG."
        exit 1
    fi
    if [[ -z "${MCBOTS_API_KEY:-}" && -z "${OPENROUTER_API_KEY:-}" ]]; then
        echo "ERROR: missing API key. Set MCBOTS_API_KEY/OPENROUTER_API_KEY or provide API model config."
        exit 1
    fi

    local shown_key
    shown_key="$(mask_secret "${MCBOTS_API_KEY:-${OPENROUTER_API_KEY:-}}")"
    echo "Agent loop env:"
    echo "  cmd=${AGENT_CMD}"
    echo "  profile=${API_MODEL_ALIAS} config=${API_MODELS_CONFIG}"
    echo "  model=${MCBOTS_MODEL}"
    echo "  model_params=${MCBOTS_MODEL_PARAMS_JSON:-<none>}"
    echo "  base_url=${MCBOTS_BASE_URL:-<default>}"
    echo "  api_key=${shown_key}"
    echo "  player=${MCBOTS_PLAYER} display=${MCBOTS_DISPLAY} workspace=${MCBOTS_WORKSPACE_ROOT}"
    echo "  remote_bash=${MCBOTS_REMOTE_BASH_HOST}:${MCBOTS_REMOTE_BASH_PORT}"
    echo "  runtime_config=${WORKSPACE_RUNTIME_CONFIG}"
    if [[ -n "${MCBOTS_RECORD_DIR:-}" ]]; then
        echo "  record_dir=${MCBOTS_RECORD_DIR}"
    fi
    echo "  record_root=${MCBOTS_RECORD_ROOT}"
    echo "  video_record=${MCBOTS_RECORD_VIDEO} fps=${MCBOTS_VIDEO_FPS} crf=${MCBOTS_VIDEO_CRF} resolution=${MCBOTS_VIDEO_RESOLUTION:-<display-default>}"
}

write_single_task_run_config_if_needed() {
    if ! single_task_layout_enabled; then
        return
    fi
    local run_dir
    run_dir="$(single_task_run_dir "$MCBOTS_SINGLE_TASK_RUN_ID")"
    local run_cfg="$run_dir/run_config.json"
    mkdir -p "$run_dir"
    TASK_NAME="$TASK_NAME" \
    TASK_CONFIG="$TASK_CONFIG" \
    RUNNER_PATH="$RUNNER_PATH" \
    TASK_RUNTIME_DIR="$TASK_RUNTIME_DIR" \
    TEMPLATE_WORLD_DIR="$TEMPLATE_WORLD_DIR" \
    API_MODEL_ALIAS="$API_MODEL_ALIAS" \
    API_MODELS_CONFIG="$API_MODELS_CONFIG" \
    AUTO_LOAD_API_MODEL="$AUTO_LOAD_API_MODEL" \
    AGENT_CMD="${AGENT_CMD:-}" \
    PLAYER_NAME="$PLAYER_NAME" \
    BOT_NAME="$BOT_NAME" \
    SERVER_PORT="$SERVER_PORT" \
    RCON_PORT="$RCON_PORT" \
    EVAL_AGENTBRIDGE_PORT="$EVAL_AGENTBRIDGE_PORT" \
    EVAL_REMOTE_BASH_PORT="$EVAL_REMOTE_BASH_PORT" \
    WAIT_PLAYER_TIMEOUT_SEC="$WAIT_PLAYER_TIMEOUT_SEC" \
    TASK_TIMEOUT_SEC="$TASK_TIMEOUT_SEC" \
    JUDGE_INTERVAL_SEC="$JUDGE_INTERVAL_SEC" \
    DRY_RUN="$DRY_RUN" \
    OUTPUT_DIR="$OUTPUT_DIR" \
    python3 - "$run_cfg" <<'PY'
import json
import os
import datetime
import sys

out_path = sys.argv[1]
payload = {
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "layout": {
        "single_task_result_layout": os.getenv("MCBOTS_SINGLE_TASK_RESULT_LAYOUT", ""),
        "single_task_run_id": os.getenv("MCBOTS_SINGLE_TASK_RUN_ID", ""),
    },
    "task": {
        "task_name": os.getenv("TASK_NAME", ""),
        "task_config": os.getenv("TASK_CONFIG", ""),
        "runner_path": os.getenv("RUNNER_PATH", ""),
        "task_runtime_dir": os.getenv("TASK_RUNTIME_DIR", ""),
        "template_world_dir": os.getenv("TEMPLATE_WORLD_DIR", ""),
    },
    "model": {
        "api_model_alias": os.getenv("API_MODEL_ALIAS", ""),
        "api_models_config": os.getenv("API_MODELS_CONFIG", ""),
        "auto_load_api_model": os.getenv("AUTO_LOAD_API_MODEL", ""),
        "model": os.getenv("MCBOTS_MODEL", ""),
        "base_url": os.getenv("MCBOTS_BASE_URL", ""),
        "model_params_json": os.getenv("MCBOTS_MODEL_PARAMS_JSON", ""),
    },
    "agent": {
        "skip_agent": os.getenv("SKIP_AGENT", ""),
        "agent_cmd": os.getenv("AGENT_CMD", ""),
        "player_name": os.getenv("PLAYER_NAME", ""),
        "bot_name": os.getenv("BOT_NAME", ""),
        "display": os.getenv("MCBOTS_DISPLAY", ""),
        "record_dir": os.getenv("MCBOTS_RECORD_DIR", ""),
        "record_root": os.getenv("MCBOTS_RECORD_ROOT", ""),
        "record_video": os.getenv("MCBOTS_RECORD_VIDEO", ""),
        "video_fps": os.getenv("MCBOTS_VIDEO_FPS", ""),
        "video_crf": os.getenv("MCBOTS_VIDEO_CRF", ""),
    },
    "eval": {
        "output_dir": os.getenv("OUTPUT_DIR", ""),
        "server_port": os.getenv("SERVER_PORT", ""),
        "rcon_port": os.getenv("RCON_PORT", ""),
        "agentbridge_port": os.getenv("EVAL_AGENTBRIDGE_PORT", ""),
        "remote_bash_port": os.getenv("EVAL_REMOTE_BASH_PORT", ""),
        "wait_player_timeout_sec": os.getenv("WAIT_PLAYER_TIMEOUT_SEC", ""),
        "task_timeout_sec": os.getenv("TASK_TIMEOUT_SEC", ""),
        "judge_interval_sec": os.getenv("JUDGE_INTERVAL_SEC", ""),
        "dry_run": os.getenv("DRY_RUN", ""),
    },
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
PY
    echo "Single-task run config: ${run_cfg}"
}

stop_eval_client_if_running() {
    stop_stale_remote_bash_processes

    if [[ ! -f "$EVAL_CLIENT_PID_FILE" ]]; then
        return
    fi

    local existing_pid
    existing_pid="$(cat "$EVAL_CLIENT_PID_FILE" 2>/dev/null || true)"
    if ! is_pid_running "$existing_pid"; then
        rm -f "$EVAL_CLIENT_PID_FILE"
        return
    fi

    echo "Stopping previous eval client pid=${existing_pid}"
    kill_process_tree "$existing_pid" TERM
    for _ in $(seq 1 20); do
        if ! is_pid_running "$existing_pid"; then
            rm -f "$EVAL_CLIENT_PID_FILE"
            stop_stale_remote_bash_processes
            return
        fi
        sleep 0.5
    done
    kill_process_tree "$existing_pid" KILL
    rm -f "$EVAL_CLIENT_PID_FILE"
    stop_stale_remote_bash_processes
}

copy_template_to_task_client_dir() {
    mkdir -p "$TASK_RUNTIME_DIR" "$EVAL_CLIENT_GAME_DIR"

    if [[ "${EVAL_CLIENT_TEMPLATE_MODE,,}" == "off" ]]; then
        echo "Client template seeding disabled (EVAL_CLIENT_TEMPLATE_MODE=off)"
        return
    fi

    if [[ ! -d "$SHARED_CLIENT_TEMPLATE_DIR" ]]; then
        echo "ERROR: shared client template not found: ${SHARED_CLIENT_TEMPLATE_DIR}" >&2
        echo "       Refusing to fallback to main-chain or empty client dir." >&2
        echo "       Prepare an eval-only client template and set SHARED_CLIENT_TEMPLATE_DIR, or set EVAL_CLIENT_TEMPLATE_MODE=off explicitly." >&2
        exit 2
    fi

    if [[ -z "$(ls -A "$SHARED_CLIENT_TEMPLATE_DIR" 2>/dev/null || true)" ]]; then
        echo "ERROR: shared client template is empty: ${SHARED_CLIENT_TEMPLATE_DIR}" >&2
        echo "       Refusing to use an empty template (would trigger downloads/state drift)." >&2
        exit 2
    fi

    if [[ -n "$(ls -A "$EVAL_CLIENT_GAME_DIR" 2>/dev/null || true)" ]]; then
        return
    fi

    echo "Seeding task client dir from template:"
    echo "  template=${SHARED_CLIENT_TEMPLATE_DIR}"
    echo "  target=${EVAL_CLIENT_GAME_DIR}"
    if [[ "${DRY_RUN,,}" == "true" ]]; then
        echo "  [dry-run] skip copy"
        return
    fi
    cp -a --reflink=auto "${SHARED_CLIENT_TEMPLATE_DIR}/." "${EVAL_CLIENT_GAME_DIR}/" 2>/dev/null \
        || cp -a "${SHARED_CLIENT_TEMPLATE_DIR}/." "${EVAL_CLIENT_GAME_DIR}/"
}

preseed_server_data_from_template_if_needed() {
    if [[ "${EVAL_SERVER_DATA_TEMPLATE_MODE,,}" == "off" ]]; then
        echo "Server-data template seeding disabled (EVAL_SERVER_DATA_TEMPLATE_MODE=off)"
        return
    fi

    # When explicitly reusing an already-running server, do not mutate/copy server-data.
    if [[ "${NO_SERVER_RESTART,,}" == "true" ]]; then
        echo "Skip server-data template seeding (NO_SERVER_RESTART=true)"
        return
    fi

    if [[ -n "$(ls -A "$SERVER_DATA_DIR" 2>/dev/null || true)" ]]; then
        echo "Server-data dir already populated, skip template seed: ${SERVER_DATA_DIR}"
        return
    fi

    if [[ ! -d "$SHARED_SERVER_DATA_TEMPLATE_DIR" ]]; then
        echo "ERROR: shared server-data template not found: ${SHARED_SERVER_DATA_TEMPLATE_DIR}" >&2
        echo "       Refusing to fallback to fresh install (would trigger downloads)." >&2
        echo "       Prepare an eval-only installed server-data template or set EVAL_SERVER_DATA_TEMPLATE_MODE=off explicitly." >&2
        exit 2
    fi
    if [[ -z "$(ls -A "$SHARED_SERVER_DATA_TEMPLATE_DIR" 2>/dev/null || true)" ]]; then
        echo "ERROR: shared server-data template is empty: ${SHARED_SERVER_DATA_TEMPLATE_DIR}" >&2
        exit 2
    fi

    mkdir -p "$(dirname "$SERVER_DATA_DIR")"
    echo "Seeding task server-data dir from template:"
    echo "  template=${SHARED_SERVER_DATA_TEMPLATE_DIR}"
    echo "  target=${SERVER_DATA_DIR}"
    if [[ "${DRY_RUN,,}" == "true" ]]; then
        echo "  [dry-run] skip copy"
        return
    fi
    cp -a --reflink=auto "$SHARED_SERVER_DATA_TEMPLATE_DIR" "$SERVER_DATA_DIR" 2>/dev/null \
        || cp -a "$SHARED_SERVER_DATA_TEMPLATE_DIR" "$SERVER_DATA_DIR"

    # Let runner recreate the task-scoped mods symlink against SERVER_MODS_DIR.
    if [[ -e "${SERVER_DATA_DIR}/mods" || -L "${SERVER_DATA_DIR}/mods" ]]; then
        rm -rf "${SERVER_DATA_DIR}/mods"
    fi
}

start_eval_client_if_needed() {
    if [[ "${START_EVAL_CLIENT,,}" != "true" ]]; then
        return
    fi

    stop_eval_client_if_running
    copy_template_to_task_client_dir
    mkdir -p "$(dirname "$EVAL_CLIENT_LOG_FILE")" "$EVAL_CLIENT_GAME_DIR"

    echo "Starting isolated eval client: bot=${BOT_NAME}, player=${PLAYER_NAME}, client_id=${EVAL_CLIENT_ID}, server=127.0.0.1:${SERVER_PORT}"
    local client_entrypoint="$SCRIPTS_ROOT/entrypoints/entrypoint.sh"
    if [[ "${EVAL_CLIENT_ENABLE_VNC,,}" == "true" ]]; then
        client_entrypoint="$SCRIPTS_ROOT/entrypoints/entrypoint-vnc.sh"
    fi
    (
        PLAYER_NAME="$PLAYER_NAME" \
        SERVER_HOST="127.0.0.1" \
        SERVER_PORT="$SERVER_PORT" \
        RENDER_MODE="$EVAL_CLIENT_RENDER_MODE" \
        AGENTBRIDGE_PORT="$EVAL_AGENTBRIDGE_PORT" \
        DISPLAY_INDEX="$EVAL_CLIENT_DISPLAY_INDEX" \
        DISPLAY_RESOLUTION="$EVAL_CLIENT_DISPLAY_RESOLUTION" \
        ENABLE_REMOTE_BASH="$EVAL_CLIENT_ENABLE_REMOTE_BASH" \
        REMOTE_BASH_PORT="$EVAL_REMOTE_BASH_PORT" \
        GAME_DIR="$EVAL_CLIENT_GAME_DIR" \
        WORKSPACE_ROOT="$EVAL_WORKSPACE_ROOT" \
        MCBOTS_PROJECT_ROOT="$PROJECT_ROOT" \
        VNC_PORT="$EVAL_VNC_PORT" \
        NOVNC_PORT="$EVAL_NOVNC_PORT" \
        VNC_VIEWONLY="$EVAL_CLIENT_VNC_VIEWONLY" \
        "$client_entrypoint"
    ) < /dev/null >"$EVAL_CLIENT_LOG_FILE" 2>&1 &

    local client_pid="$!"
    echo "$client_pid" > "$EVAL_CLIENT_PID_FILE"
    sleep 2
    if ! is_pid_running "$client_pid"; then
        echo "Eval client exited immediately. tail log:"
        tail -n 80 "$EVAL_CLIENT_LOG_FILE" || true
        exit 1
    fi
    if ! bind_tokens_to_pid "$client_pid" "$EVAL_AGENTBRIDGE_TOKEN" "$EVAL_REMOTE_BASH_TOKEN" "$EVAL_VNC_TOKEN" "$EVAL_NOVNC_TOKEN"; then
        echo "WARN: failed to bind eval client tokens to pid=${client_pid}; cleanup fallback will release at exit"
    fi
    start_token_watcher "$client_pid" "$EVAL_AGENTBRIDGE_TOKEN" "$EVAL_REMOTE_BASH_TOKEN" "$EVAL_VNC_TOKEN" "$EVAL_NOVNC_TOKEN"
    if ! bind_display_tokens_to_pid "$client_pid" "$EVAL_DISPLAY_TOKEN"; then
        echo "WARN: failed to bind eval display token to pid=${client_pid}; cleanup fallback will release at exit"
    fi
    start_display_token_watcher "$client_pid" "$EVAL_DISPLAY_TOKEN"
    echo "Eval client started pid=${client_pid}, log=${EVAL_CLIENT_LOG_FILE}"
}

CLIENT_START_WAITER_PID=""

start_eval_client_coordinator() {
    if [[ "${START_EVAL_CLIENT,,}" != "true" ]]; then
        return
    fi

    if [[ "${NO_SERVER_RESTART,,}" == "true" ]]; then
        start_eval_client_if_needed
        return
    fi

    local initial_done_count=0
    if [[ -f "$SERVER_LOG_FILE" ]]; then
        initial_done_count="$(grep -c "Done (" "$SERVER_LOG_FILE" 2>/dev/null || true)"
        initial_done_count="${initial_done_count:-0}"
    fi

    local wait_ready_timeout="${WAIT_RCON_TIMEOUT_SEC%.*}"
    if ! [[ "$wait_ready_timeout" =~ ^[0-9]+$ ]]; then
        wait_ready_timeout=180
    fi

    (
        local deadline=$((SECONDS + wait_ready_timeout + 180))
        while [[ "$SECONDS" -lt "$deadline" ]]; do
            local current_done_count=0
            if [[ -f "$SERVER_LOG_FILE" ]]; then
                current_done_count="$(grep -c "Done (" "$SERVER_LOG_FILE" 2>/dev/null || true)"
                current_done_count="${current_done_count:-0}"
            fi
            if [[ "$current_done_count" -gt "$initial_done_count" ]]; then
                start_eval_client_if_needed
                exit 0
            fi
            sleep 2
        done
        echo "WARN: server ready signal not observed in time, eval client not auto-started"
    ) < /dev/null >/dev/null 2>&1 &
    CLIENT_START_WAITER_PID="$!"
    echo "Eval client coordinator started pid=${CLIENT_START_WAITER_PID} (will start client after server ready)"
}

cleanup_waiter() {
    if [[ -z "$CLIENT_START_WAITER_PID" ]]; then
        return
    fi
    if is_pid_running "$CLIENT_START_WAITER_PID"; then
        kill "$CLIENT_START_WAITER_PID" 2>/dev/null || true
        wait "$CLIENT_START_WAITER_PID" 2>/dev/null || true
    fi
}

cleanup_server_token_binder() {
    if [[ -z "$SERVER_TOKEN_BINDER_PID" ]]; then
        return
    fi
    if is_pid_running "$SERVER_TOKEN_BINDER_PID"; then
        kill "$SERVER_TOKEN_BINDER_PID" 2>/dev/null || true
        wait "$SERVER_TOKEN_BINDER_PID" 2>/dev/null || true
    fi
}

stop_eval_server_if_running() {
    local server_pid
    server_pid="$(read_pid_file "$SERVER_PID_FILE")"
    if [[ -z "$server_pid" ]] || ! [[ "$server_pid" =~ ^[0-9]+$ ]]; then
        return
    fi
    if ! is_pid_running "$server_pid"; then
        rm -f "$SERVER_PID_FILE"
        return
    fi

    echo "Stopping eval server pid=${server_pid}"
    kill_process_tree "$server_pid" TERM
    for _ in $(seq 1 20); do
        if ! is_pid_running "$server_pid"; then
            rm -f "$SERVER_PID_FILE"
            return
        fi
        sleep 0.5
    done
    kill_process_tree "$server_pid" KILL
    rm -f "$SERVER_PID_FILE"
}

cleanup_all() {
    cleanup_waiter
    cleanup_server_token_binder
    if [[ "${STOP_CLIENT_ON_EXIT,,}" == "true" ]]; then
        stop_eval_client_if_running
    fi
    if [[ "${STOP_SERVER_ON_EXIT,,}" == "true" && "${NO_SERVER_RESTART,,}" != "true" ]]; then
        stop_eval_server_if_running
    fi
    bind_server_tokens_if_possible || true
    release_pending_ports
    release_pending_displays
    run_port_registry cleanup >/dev/null 2>&1 || true
    run_display_registry cleanup >/dev/null 2>&1 || true
}

trap cleanup_all EXIT INT TERM

auto_assign_ports
export MCBOTS_REMOTE_BASH_PORT="${MCBOTS_REMOTE_BASH_PORT:-$EVAL_REMOTE_BASH_PORT}"
echo "Auto port allocation:"
echo "  registry=${PORT_REGISTRY_FILE}"
echo "  display_registry=${DISPLAY_REGISTRY_FILE}"
echo "  server=${SERVER_PORT}"
echo "  rcon=${RCON_PORT}"
echo "  agentbridge=${EVAL_AGENTBRIDGE_PORT}"
if [[ "${EVAL_CLIENT_ENABLE_REMOTE_BASH,,}" == "true" ]]; then
    echo "  remotebash=${EVAL_REMOTE_BASH_PORT}"
fi
if [[ "${EVAL_CLIENT_ENABLE_VNC,,}" == "true" ]]; then
    echo "  vnc=${EVAL_VNC_PORT}"
    echo "  novnc=${EVAL_NOVNC_PORT}"
fi
echo "  display=:${EVAL_CLIENT_DISPLAY_INDEX}"
echo "  init_weather=${INIT_WEATHER} init_mob_spawning=${INIT_MOB_SPAWNING} init_time=${INIT_TIME:-<keep>} clear_existing_hostiles=${INIT_CLEAR_EXISTING_HOSTILES}"
echo "  equip_distraction_mode=${INIT_EQUIP_DISTRACTION_MODE} level=${INIT_EQUIP_DISTRACTION_LEVEL} random_head_candidates=${INIT_EQUIP_DISTRACTION_RANDOM_HEAD_CANDIDATES:-<openha-default>}"
echo "  inventory_distraction_mode=${INIT_INVENTORY_DISTRACTION_MODE} level=${INIT_INVENTORY_DISTRACTION_LEVEL}"

prepare_agent_runtime_env
write_single_task_run_config_if_needed
preseed_server_data_from_template_if_needed
if [[ "${START_EVAL_CLIENT,,}" == "true" ]]; then
    stop_eval_client_if_running
fi
start_eval_client_coordinator
start_server_token_binder

CMD=(
    python3 "$RUNNER_PATH"
    --task-config "$TASK_CONFIG"
    --task-name "$TASK_NAME"
    --template-world-dir "$TEMPLATE_WORLD_DIR"
    --server-world-dir "$SERVER_WORLD_DIR"
    --server-data-dir "$SERVER_DATA_DIR"
    --server-mods-dir "$SERVER_MODS_DIR"
    --server-entrypoint "$SERVER_ENTRYPOINT"
    --server-log-file "$SERVER_LOG_FILE"
    --server-pid-file "$SERVER_PID_FILE"
    --mc-version "$MC_VERSION"
    --neoforge-version "$NEOFORGE_VERSION"
    --server-memory-min "$MEMORY_MIN"
    --server-memory-max "$MEMORY_MAX"
    --server-port "$SERVER_PORT"
    --player-name "$PLAYER_NAME"
    --rcon-host "$RCON_HOST"
    --rcon-port "$RCON_PORT"
    --rcon-password "$RCON_PASSWORD"
    --task-timeout-sec "$TASK_TIMEOUT_SEC"
    --judge-interval-sec "$JUDGE_INTERVAL_SEC"
    --wait-rcon-timeout-sec "$WAIT_RCON_TIMEOUT_SEC"
    --wait-player-timeout-sec "$WAIT_PLAYER_TIMEOUT_SEC"
    --init-weather "$INIT_WEATHER"
    --init-mob-spawning "$INIT_MOB_SPAWNING"
    --init-clear-existing-hostiles "$INIT_CLEAR_EXISTING_HOSTILES"
    --init-equip-distraction-mode "$INIT_EQUIP_DISTRACTION_MODE"
    --init-equip-distraction-level "$INIT_EQUIP_DISTRACTION_LEVEL"
    --init-inventory-distraction-mode "$INIT_INVENTORY_DISTRACTION_MODE"
    --init-inventory-distraction-level "$INIT_INVENTORY_DISTRACTION_LEVEL"
    --task-instruction-file "$TASK_INSTRUCTION_FILE"
    --task-instruction-mode "$TASK_INSTRUCTION_MODE"
    --output-dir "$OUTPUT_DIR"
)

if [[ -n "${INIT_TIME:-}" ]]; then
    CMD+=(--init-time "$INIT_TIME")
fi
if [[ -n "${INIT_EQUIP_DISTRACTION_FIXED_JSON:-}" ]]; then
    CMD+=(--init-equip-distraction-fixed-json "$INIT_EQUIP_DISTRACTION_FIXED_JSON")
fi
if [[ -n "${INIT_EQUIP_DISTRACTION_RANDOM_HEAD_CANDIDATES:-}" ]]; then
    CMD+=(--init-equip-distraction-random-head-candidates "$INIT_EQUIP_DISTRACTION_RANDOM_HEAD_CANDIDATES")
fi
if [[ -n "${OPENHA_INIT_ACTION_REPLAY:-}" ]]; then
    CMD+=(--openha-init-action-replay "$OPENHA_INIT_ACTION_REPLAY")
fi
if [[ -n "${OPENHA_INIT_ACTION_STEP_SLEEP_SEC:-}" ]]; then
    CMD+=(--openha-init-action-step-sleep-sec "$OPENHA_INIT_ACTION_STEP_SLEEP_SEC")
fi
if [[ -n "${OPENHA_INIT_ACTION_CAMERA_PIXELS_PER_UNIT:-}" ]]; then
    CMD+=(--openha-init-action-camera-pixels-per-unit "$OPENHA_INIT_ACTION_CAMERA_PIXELS_PER_UNIT")
fi

if [[ "${SKIP_AGENT,,}" == "true" ]]; then
    CMD+=(--skip-agent)
fi
if [[ "${SKIP_PLAYER_WAIT,,}" == "true" ]]; then
    CMD+=(--skip-player-wait)
fi
if [[ "${SKIP_WORLD_RESTORE,,}" == "true" ]]; then
    CMD+=(--skip-world-restore)
fi
if [[ "${NO_SERVER_RESTART,,}" == "true" ]]; then
    CMD+=(--no-server-restart)
fi
if [[ "${DRY_RUN,,}" == "true" ]]; then
    CMD+=(--dry-run)
fi

if [[ -n "${AGENT_CMD:-}" ]]; then
    CMD+=(--agent-cmd "$AGENT_CMD")
fi
if [[ -n "${AGENT_INSTRUCTION:-}" ]]; then
    CMD+=(--agent-instruction "$AGENT_INSTRUCTION")
fi

echo "Running inside container:"
printf '  %q' "${CMD[@]}"
echo

set +e
"${CMD[@]}"
runner_rc=$?
set -e

bind_server_tokens_if_possible || true

exit "$runner_rc"
