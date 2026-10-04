#!/usr/bin/env bash
# Launch one materialized 1.21.11 navigation task.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
RUN_DIR=""
SERVER_PID=""
CLIENT_PID=""
MONITOR_PID=""
AGENT_PID=""
FORCELOAD_ACTIVE="false"
START_BLOCK_X=""
START_BLOCK_Z=""
PHASE="initialization"
RESULTS_DIR=""
MESSAGES_FINALIZED="false"

usage() {
    echo "Usage: $0 --run-dir PATH" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --run-dir)
            RUN_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

[[ -n "$RUN_DIR" ]] || { usage; exit 2; }
RUN_DIR="$(cd "$RUN_DIR" && pwd)"
RUN_JSON="$RUN_DIR/control/run.json"
[[ -f "$RUN_JSON" ]] || { echo "Missing $RUN_JSON" >&2; exit 2; }
LOG_DIR="$RUN_DIR/logs"
mkdir -p "$LOG_DIR"
AGENT_WORKSPACE="$(mktemp -d "$RUN_DIR/agent-workspace.XXXXXX")"
printf '%s\n' "$AGENT_WORKSPACE" >"$RUN_DIR/control/agent-workspace-path.txt"
AGENT_ACTION_BIN="$RUN_DIR/control/agent-action-bin"
mkdir -p "$AGENT_ACTION_BIN"
install -m 0755 \
    "$PROJECT_ROOT/scripts/runtime/mcapi" \
    "$PROJECT_ROOT/scripts/runtime/xdo" \
    "$AGENT_ACTION_BIN/"
install -m 0644 \
    "$PROJECT_ROOT/scripts/runtime/mc_runtime.py" \
    "$AGENT_ACTION_BIN/"

json_value() {
    uv run python - "$RUN_JSON" "$1" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    value = value[part]
print(value)
PY
}

kill_tree() {
    local pid="${1:-}"
    [[ -n "$pid" ]] || return 0
    kill -0 "$pid" 2>/dev/null || return 0
    local child
    for child in $(ps -o pid= --ppid "$pid" 2>/dev/null); do
        kill_tree "$child"
    done
    kill -TERM "$pid" 2>/dev/null || true
}

stop_agent_process() {
    local pid="${AGENT_PID:-}"
    [[ -n "$pid" ]] || return 0
    if kill -0 "$pid" 2>/dev/null; then
        for _ in $(seq 1 8); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.25
        done
    fi
    if kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.25
        done
    fi
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
}

finalize_agent_messages() {
    [[ "$MESSAGES_FINALIZED" != "true" ]] || return 0
    [[ -n "$RESULTS_DIR" ]] || return 0
    local record_dir="$RESULTS_DIR/agent-record"
    if [[ ! -f "$record_dir/messages.jsonl" ]]; then
        MESSAGES_FINALIZED="true"
        return 0
    fi
    uv run python "$PROJECT_ROOT/scripts/analysis/finalize_agent_messages.py" \
        --record-dir "$record_dir" \
        >"$LOG_DIR/messages-finalize.json" \
        2>"$LOG_DIR/messages-finalize.log"
    MESSAGES_FINALIZED="true"
}

write_fallback_result() {
    local exit_status="$1"
    local reason="$2"
    [[ -n "$RESULTS_DIR" && -f "$RUN_JSON" ]] || return 0
    [[ ! -f "$RESULTS_DIR/supervisor.json" ]] || return 0
    uv run python - "$RUN_JSON" "$RESULTS_DIR" "$reason" "$exit_status" <<'PY' || true
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from eval.navigation.metrics import NavigationMetricAccumulator
from eval.navigation.schema import atomic_write_json

run_path = Path(sys.argv[1])
results = Path(sys.argv[2])
reason = sys.argv[3]
exit_status = int(sys.argv[4])
run = json.loads(run_path.read_text(encoding="utf-8"))
task = run["task"]
metrics = NavigationMetricAccumulator(
    start=task["start"]["position"],
    target=task["target"]["position"],
    required_waypoints=task["required_waypoints"],
    reference_length_blocks=run["reference_length_blocks"],
).finalize(completed=False, duration_sec=0.0)
ended_at = (
    datetime.now(timezone.utc)
    .replace(microsecond=0)
    .isoformat()
    .replace("+00:00", "Z")
)
interrupted = reason == "external_interrupt"
completion = {
    "schema_version": 1,
    "terminal": True,
    "success": False,
    "terminal_reason": reason,
    "claim_count": 0,
    "oracle_arrived": False,
    "oracle_arrived_at_elapsed_sec": None,
    "last_sample": None,
    "arrival": {},
    "metrics": metrics,
    "task_id": run["task_id"],
    "map_id": run["map_id"],
    "mode": run["mode"],
    "state_error_count": 0,
    "infrastructure_error": not interrupted,
    "ended_at_utc": ended_at,
}
atomic_write_json(results / "completion.json", completion)
atomic_write_json(results / "metrics.json", metrics)
atomic_write_json(
    results / "supervisor.json",
    {
        "schema_version": 1,
        "task_id": run["task_id"],
        "terminal_reason": reason,
        "infrastructure_error": not interrupted,
        "success": False,
        "wrapper_exit_status": exit_status,
        "ended_at_utc": ended_at,
    },
)
agent_status_path = run_path.parent / "agent-status.json"
agent_status = (
    json.loads(agent_status_path.read_text(encoding="utf-8"))
    if agent_status_path.is_file()
    else None
)
atomic_write_json(
    results / "agent-result.json",
    {
        "schema_version": 1,
        "mode": run["mode"],
        "agent_started": agent_status is not None,
        "decision_count": (
            int(agent_status.get("llm_request_success", 0))
            if isinstance(agent_status, dict)
            else 0
        ),
        "agent_status": agent_status,
        "recorded_at_utc": ended_at,
    },
)
for name in ("positions.jsonl", "claims.jsonl"):
    (results / name).touch(exist_ok=True)
PY
}

cleanup() {
    local exit_status=$?
    trap - EXIT INT TERM
    local reason="infrastructure_${PHASE}_failed"
    if [[ "$exit_status" -eq 130 || "$exit_status" -eq 143 ]]; then
        reason="external_interrupt"
    fi
    stop_agent_process
    finalize_agent_messages || true
    write_fallback_result "$exit_status" "$reason"
    if [[ "$FORCELOAD_ACTIVE" == "true" && -n "$START_BLOCK_X" && -n "$START_BLOCK_Z" ]]; then
        mcrcon -H 127.0.0.1 -P "$RCON_PORT" -p minecraft \
            "forceload remove $START_BLOCK_X $START_BLOCK_Z" >/dev/null 2>&1 || true
    fi
    kill_tree "$MONITOR_PID"
    kill_tree "$CLIENT_PID"
    kill_tree "$SERVER_PID"
}
trap cleanup EXIT INT TERM

write_terminal_request() {
    local reason="$1"
    uv run python - "$RUN_DIR/control/terminal-request.json" "$reason" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path
path = Path(sys.argv[1])
fd, name = tempfile.mkstemp(prefix=".terminal.", suffix=".tmp", dir=path.parent)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump({"schema_version": 1, "reason": sys.argv[2]}, handle)
    handle.write("\n")
Path(name).replace(path)
PY
}

TASK_ID="$(json_value task_id)"
MODE="$(json_value mode)"
RESULTS_DIR="$(json_value results_dir)"
COORDINATE_LOCK_ENABLED="$(json_value eval_setting.coordinate_lock_enabled)"
case "$COORDINATE_LOCK_ENABLED" in
    True|true) COORDINATE_LOCK_ENABLED="true" ;;
    False|false) COORDINATE_LOCK_ENABLED="false" ;;
    *) echo "Invalid coordinate-lock setting" >&2; exit 2 ;;
esac
SERVER_PORT="$(json_value ports.server)"
RCON_PORT="$(json_value ports.rcon)"
AGENTBRIDGE_PORT="$(json_value ports.agentbridge)"
REMOTE_BASH_PORT="$(json_value ports.remote_bash)"
MC_VERSION="$(json_value runtime_versions.minecraft)"
NEOFORGE_VERSION="$(json_value runtime_versions.neoforge)"
PLAYER_NAME="client"
MODEL_ID="$(uv run python - "$RUN_JSON" <<'PY'
import json
import sys
run = json.load(open(sys.argv[1], encoding="utf-8"))
print(run["model_parameters"].get("model_id", ""))
PY
)"
API_PROTOCOL="$(uv run python - "$RUN_JSON" <<'PY'
import json
import sys
run = json.load(open(sys.argv[1], encoding="utf-8"))
print(run["model_parameters"].get("api_protocol", "chat_completions"))
PY
)"
ACTION_PROTOCOL="$(uv run python - "$RUN_JSON" <<'PY'
import json
import sys
run = json.load(open(sys.argv[1], encoding="utf-8"))
print(run["model_parameters"].get("action_protocol", "tool_calls"))
PY
)"
MODEL_PARAMS_JSON="$(uv run python - "$RUN_JSON" <<'PY'
import json
import sys
run = json.load(open(sys.argv[1], encoding="utf-8"))
parameters = dict(run["model_parameters"])
parameters.pop("model_id", None)
parameters.pop("api_protocol", None)
parameters.pop("action_protocol", None)
print(json.dumps(parameters, ensure_ascii=False, separators=(",", ":")))
PY
)"
START_X="$(json_value task.start.position.x)"
START_Y="$(json_value task.start.position.y)"
START_Z="$(json_value task.start.position.z)"
START_YAW="$(json_value task.start.yaw)"
START_PITCH="$(json_value task.start.pitch)"
read -r START_BLOCK_X START_BLOCK_Y START_BLOCK_Z < <(
    uv run python - "$START_X" "$START_Y" "$START_Z" <<'PY'
import math
import sys
print(*(math.floor(float(value)) for value in sys.argv[1:4]))
PY
)

PHASE="server_start"
(
    cd "$RUN_DIR/server"
    MC_VERSION="$MC_VERSION" \
    NEOFORGE_VERSION="$NEOFORGE_VERSION" \
    sh run.sh nogui
) >"$LOG_DIR/server.log" 2>&1 &
SERVER_PID=$!

uv run python - "$SERVER_PORT" "$SERVER_PID" <<'PY'
import os
import socket
import sys
import time
port, pid = int(sys.argv[1]), int(sys.argv[2])
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
    except OSError:
        raise SystemExit("server exited before opening its port")
    with socket.socket() as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(0)
    time.sleep(0.5)
raise SystemExit("timed out waiting for navigation server")
PY

PHASE="start_chunk_preload"
command -v mcrcon >/dev/null 2>&1 || {
    echo "mcrcon is required for navigation setup" >&2
    exit 3
}
for _ in $(seq 1 120); do
    RCON_READY="$(
        mcrcon -H 127.0.0.1 -P "$RCON_PORT" -p minecraft list 2>/dev/null || true
    )"
    [[ -n "$RCON_READY" ]] && break
    sleep 0.5
done
[[ -n "${RCON_READY:-}" ]] || {
    echo "RCON did not become ready before start chunk preload" >&2
    exit 3
}
FORCELOAD_QUERY="$(
    mcrcon -H 127.0.0.1 -P "$RCON_PORT" -p minecraft \
        "forceload query $START_BLOCK_X $START_BLOCK_Z"
)"
printf '%s\n' "$FORCELOAD_QUERY" >"$LOG_DIR/start-chunk-preload.log"
if [[ "$FORCELOAD_QUERY" == *"not marked"* ]]; then
    mcrcon -H 127.0.0.1 -P "$RCON_PORT" -p minecraft \
        "forceload add $START_BLOCK_X $START_BLOCK_Z" >>"$LOG_DIR/start-chunk-preload.log"
    FORCELOAD_ACTIVE="true"
fi
for _ in $(seq 1 30); do
    LOADED_CHECK="$(
        mcrcon -H 127.0.0.1 -P "$RCON_PORT" -p minecraft \
            "execute if loaded $START_BLOCK_X $START_BLOCK_Y $START_BLOCK_Z run time query daytime" \
            2>/dev/null || true
    )"
    if [[ "$LOADED_CHECK" == *"The time is"* ]]; then
        printf '%s\n' "$LOADED_CHECK" >>"$LOG_DIR/start-chunk-preload.log"
        break
    fi
    sleep 0.25
done
[[ "${LOADED_CHECK:-}" == *"The time is"* ]] || {
    echo "Start chunk did not become loaded" >&2
    exit 3
}
mcrcon -H 127.0.0.1 -P "$RCON_PORT" -p minecraft \
    "gamerule minecraft:respawn_radius 0" \
    "setworldspawn $START_BLOCK_X $START_BLOCK_Y $START_BLOCK_Z $START_YAW $START_PITCH" \
    >"$LOG_DIR/start-spawn-setup.log"

PHASE="client_start"
PATH="$PROJECT_ROOT/scripts/runtime:$PATH" \
MCBOTS_RUNTIME_CONFIG="$RUN_DIR/client/.mcbots_runtime.json" \
MCBOTS_WORKSPACE_ROOT="$RUN_DIR/client" \
BASE_RUNTIME_DIR="$RUN_DIR/control/client-launch" \
BASE_LOG_DIR="$LOG_DIR/client-launch" \
PORT_REGISTRY_FILE="$RUN_DIR/control/port-registry.json" \
DISPLAY_REGISTRY_FILE="$RUN_DIR/control/display-registry.json" \
CLIENTS_DIR="$RUN_DIR" \
CLIENT_GAME_TEMPLATE="$RUN_DIR/client/game" \
BOT_NAME="client" \
SERVER_HOST="127.0.0.1" \
SERVER_PORT="$SERVER_PORT" \
AGENTBRIDGE_PORT="$AGENTBRIDGE_PORT" \
AGENTBRIDGE_SUPPRESS_UNSECURE_SERVER_TOAST="true" \
AGENTBRIDGE_COORDINATE_LOCK="$COORDINATE_LOCK_ENABLED" \
REMOTE_BASH_PORT="$REMOTE_BASH_PORT" \
REMOTE_BASH_WORKDIR="$AGENT_WORKSPACE" \
REMOTE_BASH_SANDBOX="true" \
REMOTE_BASH_SANDBOX_DISABLE_NETWORK="${MCBOTS_NAV_REMOTE_BASH_NETWORK_DISABLED:-false}" \
REMOTE_BASH_SANDBOX_ACTION_BIN="$AGENT_ACTION_BIN" \
REMOTE_BASH_SANDBOX_RUNTIME_CONFIG="$RUN_DIR/client/.mcbots_runtime.json" \
ENABLE_REMOTE_BASH="true" \
ENABLE_VNC="$(json_value vnc)" \
VNC_PORT="${MCBOTS_NAV_VNC_PORT:-}" \
NOVNC_PORT="${MCBOTS_NAV_NOVNC_PORT:-}" \
CLIENT_FOREGROUND="false" \
COPY_GAME_MODS_TEMPLATE="false" \
MC_VERSION="$MC_VERSION" \
NEOFORGE_VERSION="$NEOFORGE_VERSION" \
MCBOTS_PORTABLEMC_SHIM="$PROJECT_ROOT/scripts/runtime/portablemc-neoforge-root-shim.py" \
"$PROJECT_ROOT/scripts/launch/start-single-bot-inside.sh" client \
    >"$LOG_DIR/client-launcher.log" 2>&1

PID_FILE="$(find "$RUN_DIR/control/client-launch" -name '*.pids' -type f | head -n 1)"
[[ -n "$PID_FILE" ]] || { echo "Client launcher did not write a PID file" >&2; exit 3; }
CLIENT_PID="$(sed -n 's/^CLIENT_PID=//p' "$PID_FILE")"
CLIENT_DISPLAY=":$(sed -n 's/^DISPLAY_INDEX=//p' "$PID_FILE")"
[[ "$CLIENT_DISPLAY" != ":" ]] || { echo "Client launcher did not report DISPLAY_INDEX" >&2; exit 3; }
if [[ "$(json_value vnc)" == "True" ]]; then
    VNC_URL="$(sed -n 's/.*noVNC=\\([^)]*\\)).*/\\1/p' "$LOG_DIR/client-launcher.log" | tail -n 1)"
    if [[ -n "$VNC_URL" ]]; then
        echo "Navigation review noVNC: ${VNC_URL/<host>/127.0.0.1}"
    fi
fi

PHASE="runtime_readback"
uv run python - \
    "$AGENTBRIDGE_PORT" \
    "$RESULTS_DIR/runtime-readback.json" \
    "$MC_VERSION" \
    "$NEOFORGE_VERSION" \
    "$COORDINATE_LOCK_ENABLED" <<'PY'
import json
import sys
import time
import urllib.request
port = int(sys.argv[1])
output = sys.argv[2]
expected_minecraft = sys.argv[3]
expected_neoforge = sys.argv[4]
expected_coordinate_lock = sys.argv[5] == "true"
deadline = time.monotonic() + 240
last = None
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as response:
            last = json.load(response)
        if last.get("success"):
            break
    except Exception:
        pass
    time.sleep(1)
else:
    raise SystemExit(f"AgentBridge health timeout: {last}")
if (
    last.get("minecraft_version") != expected_minecraft
    or last.get("neoforge_version") != expected_neoforge
):
    raise SystemExit(f"runtime version mismatch: {last}")
if last.get("coordinate_lock_enabled") is not expected_coordinate_lock:
    raise SystemExit(f"AgentBridge coordinate lock state mismatch: {last}")
if expected_coordinate_lock and last.get("coordinate_lock_policy_version") != "xaero-coordinate-filter-v2":
    raise SystemExit(f"AgentBridge coordinate lock policy mismatch: {last}")
with open(output, "w", encoding="utf-8") as handle:
    json.dump(last, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY

action_cli_readback() {
uv run python - \
    "$REMOTE_BASH_PORT" \
    "$RUN_DIR/client/.mcbots_runtime.json" \
    "$RESULTS_DIR/action-cli-readback.json" \
    "$(json_value eval_setting.third_person)" <<'PY'
import json
import sys
import urllib.request

remote_bash_port = int(sys.argv[1])
runtime_config_path = sys.argv[2]
output_path = sys.argv[3]
third_person = sys.argv[4].strip().lower() == "true"
runtime_config = json.load(open(runtime_config_path, encoding="utf-8"))

def execute(command):
    request = urllib.request.Request(
        f"http://127.0.0.1:{remote_bash_port}/exec",
        data=json.dumps({"command": command, "timeout": 10}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.load(response)
    if payload.get("exit_code") != 0:
        raise SystemExit(f"action CLI smoke failed for {command!r}: {payload}")
    return payload

mcapi = execute("mcapi state")
try:
    mcapi_state = json.loads(mcapi.get("stdout") or "")
except json.JSONDecodeError as exc:
    raise SystemExit(f"mcapi state returned invalid JSON: {mcapi}") from exc
if mcapi_state.get("success") is not True:
    raise SystemExit(f"mcapi state did not succeed: {mcapi_state}")
xdo = execute("xdo getmouselocation --shell")
if third_person:
    execute("xdo key F5")
sandbox = execute(
    'test "$PWD" = /workspace '
    '&& test -z "$(find /workspace -mindepth 1 -maxdepth 1 -print -quit)" '
    '&& test ! -e /workspace/mcbots '
    '&& test ! -e /path/to/Projects/mcbots '
    '&& echo sandbox-ok'
)
capabilities = execute("awk '/^CapEff:/ {print $2}' /proc/self/status")
if sandbox.get("stdout", "").strip() != "sandbox-ok":
    raise SystemExit(f"Remote Bash sandbox isolation failed: {sandbox}")
if capabilities.get("stdout", "").strip() != "0000000000000000":
    raise SystemExit(f"Remote Bash retained effective capabilities: {capabilities}")
readback = {
    "schema_version": 1,
    "agentbridge": runtime_config["agentbridge"],
    "remote_bash": runtime_config["remote_bash"],
    "display": runtime_config["x11"]["display"],
    "mcapi_state_success": True,
    "xdo_success": True,
    "xdo_stdout": xdo.get("stdout", "").strip(),
    "third_person": third_person,
    "sandbox_workspace": "/workspace",
    "sandbox_project_hidden": True,
    "sandbox_workspace_empty": True,
    "sandbox_effective_capabilities": capabilities.get("stdout", "").strip(),
}
with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(readback, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

PHASE="rcon_setup"
for _ in $(seq 1 120); do
    PLAYER_LIST="$(mcrcon -H 127.0.0.1 -P "$RCON_PORT" -p minecraft list 2>/dev/null || true)"
    if [[ "$PLAYER_LIST" == *"$PLAYER_NAME"* ]]; then
        break
    fi
    sleep 1
done
[[ "${PLAYER_LIST:-}" == *"$PLAYER_NAME"* ]] || {
    echo "Player did not join before RCON setup" >&2
    exit 3
}

PLAYER_SCALE="$(json_value eval_setting.player_scale)"
TIME_VALUE="$(json_value runtime.time.value)"
TIME_FREEZE="$(json_value runtime.time.freeze)"
WEATHER_VALUE="$(json_value runtime.weather.value)"
WEATHER_FREEZE="$(json_value runtime.weather.freeze)"
DAYLIGHT_CYCLE="true"
WEATHER_CYCLE="true"
[[ "${TIME_FREEZE,,}" == "true" ]] && DAYLIGHT_CYCLE="false"
[[ "${WEATHER_FREEZE,,}" == "true" ]] && WEATHER_CYCLE="false"

PHASE="rcon_setup"
RCON_SETUP_COMMANDS=(
    "gamemode adventure $PLAYER_NAME"
    "clear $PLAYER_NAME"
    "effect clear $PLAYER_NAME"
    "difficulty peaceful"
    "gamerule spawn_mobs false"
    "gamerule advance_time $DAYLIGHT_CYCLE"
    "gamerule advance_weather $WEATHER_CYCLE"
    "time set $TIME_VALUE"
    "weather $WEATHER_VALUE"
    "tp $PLAYER_NAME $START_X $START_Y $START_Z $START_YAW $START_PITCH"
    "attribute $PLAYER_NAME minecraft:scale base set $PLAYER_SCALE"
    "item replace entity $PLAYER_NAME hotbar.1 with minecraft:torch 1"
    "effect give $PLAYER_NAME minecraft:instant_health 1 10 true"
)
mcrcon -H 127.0.0.1 -P "$RCON_PORT" -p minecraft \
    "${RCON_SETUP_COMMANDS[@]}" >"$LOG_DIR/rcon-setup.log"

PHASE="start_readiness"
uv run python - \
    "$AGENTBRIDGE_PORT" \
    "$START_X" "$START_Y" "$START_Z" \
    "$RUN_DIR/control/start-readiness.json" <<'PY'
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from eval.navigation.schema import atomic_write_json
from eval.navigation.startup import StartReadinessPolicy, StartReadinessTracker

port = int(sys.argv[1])
expected = tuple(float(value) for value in sys.argv[2:5])
output = Path(sys.argv[5])
policy = StartReadinessPolicy(
    expected_x=expected[0], expected_y=expected[1], expected_z=expected[2]
)
tracker = StartReadinessTracker(policy)
deadline = time.monotonic() + 30
samples = []
last_error = None
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/state", timeout=2
        ) as response:
            state = json.load(response)
        sample = tracker.observe(state)
        samples.append(sample)
        if sample["ready"]:
            atomic_write_json(
                output,
                {
                    "schema_version": 1,
                    "ready": True,
                    "expected_position": dict(zip(("x", "y", "z"), expected)),
                    "policy": {
                        "horizontal_tolerance": policy.horizontal_tolerance,
                        "vertical_tolerance": policy.vertical_tolerance,
                        "stable_epsilon": policy.stable_epsilon,
                        "consecutive_samples": policy.consecutive_samples,
                    },
                    "samples": samples,
                    "confirmed_at_utc": datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
            raise SystemExit(0)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        last_error = str(error)
    time.sleep(0.5)
atomic_write_json(
    output,
    {
        "schema_version": 1,
        "ready": False,
        "expected_position": dict(zip(("x", "y", "z"), expected)),
        "samples": samples,
        "last_error": last_error,
    },
)
raise SystemExit(
    f"Player did not settle on the ground near the task start: {last_error or samples[-1:] or 'no state'}"
)
PY

if [[ "$(json_value eval_setting.guideline)" == "True" ]]; then
    PHASE="ground_navigation_guide"
    uv run python "$SCRIPT_DIR/set_ground_navigation_route.py" \
        --instance "$RUN_DIR/client/game" \
        --run-json "$RUN_JSON" \
        --arrival-radius "$(json_value arrival.radius_3d)" \
        --wait-seconds 30 \
        >"$LOG_DIR/ground-navigation-guide.json"
fi

mcrcon -H 127.0.0.1 -P "$RCON_PORT" -p minecraft \
    "forceload remove $START_BLOCK_X $START_BLOCK_Z" >"$LOG_DIR/start-chunk-release.log"
FORCELOAD_ACTIVE="false"

# Let transient join/tutorial toasts clear before the agent captures its first frame.
sleep 6

PHASE="action_cli_readback"
if ! action_cli_readback >"$LOG_DIR/action-cli-readback.log" 2>&1; then
    cat "$LOG_DIR/action-cli-readback.log" >&2
    exit 1
fi

PHASE="goal_monitor"
uv run python "$SCRIPT_DIR/monitor-navigation-goal-inside.py" \
    --run-dir "$RUN_DIR" >"$LOG_DIR/monitor.log" 2>&1 &
MONITOR_PID=$!

if [[ "$MODE" != "review" ]]; then
    PHASE="agent_start"
    PROMPT="$(json_value task.prompt)"
    MCBOTS_RUNTIME_CONFIG="$RUN_DIR/client/.mcbots_runtime.json" \
    MCBOTS_WORKSPACE_ROOT="$RUN_DIR/client/game" \
    MCBOTS_RECORD_DIR="$RESULTS_DIR/agent-record" \
    MCBOTS_DISPLAY="$CLIENT_DISPLAY" \
    MCBOTS_LOG_FILE_PATH="$RUN_DIR/client/game/logs/latest.log" \
    MCBOTS_PLAYER="$PLAYER_NAME" \
    MCBOTS_REMOTE_BASH_HOST="127.0.0.1" \
    MCBOTS_REMOTE_BASH_PORT="$REMOTE_BASH_PORT" \
    MCBOTS_INITIAL_USER_INPUT="$PROMPT" \
    MCBOTS_SYSTEM_PROMPT_PROFILE="navigation" \
    MCBOTS_NAV_CLAIM_REQUEST_DIR="$RUN_DIR/control/claim-requests" \
    MCBOTS_NAV_CLAIM_RESPONSE_DIR="$RUN_DIR/control/claim-responses" \
    MCBOTS_NAV_EVENT_PATH="$RUN_DIR/control/evaluator-events.jsonl" \
    MCBOTS_NAV_AGENT_STATUS_PATH="$RUN_DIR/control/agent-status.json" \
    MCBOTS_MAX_LLM_REQUEST_SUCCESSES="$(json_value limits.max_assistant_steps)" \
    MCBOTS_OBSERVE_INTERVAL="$(json_value agent.observe_interval_sec)" \
    MCBOTS_DEFAULT_OBSERVE_ENABLED="$(json_value agent.default_periodic_observe)" \
    MCBOTS_ALLOW_MODEL_OBSERVE_TOGGLE="$(json_value agent.allow_model_observe_toggle)" \
    MCBOTS_EXEC_TIMEOUT="$(json_value agent.bash_timeout_sec)" \
    MCBOTS_LLM_TIMEOUT_SEC="$(json_value agent.llm_timeout_sec)" \
    MCBOTS_LLM_MAX_RETRIES="$(json_value agent.llm_max_retries)" \
    MCBOTS_MAX_CONSECUTIVE_LLM_FAILURES="$(json_value agent.max_consecutive_llm_failures)" \
    MCBOTS_ENABLE_PANORAMA="$(json_value agent.six_view_enabled)" \
    MCBOTS_NAVIGATION_HINTS_ENABLED="$(json_value eval_setting.navigation_hints_enabled)" \
    MCBOTS_MAX_IMAGES_IN_CONTEXT="$(json_value agent.max_images_in_context)" \
    MCBOTS_AUTO_SUMMARIZE_TURN_THRESHOLD="$(json_value agent.auto_summarize_turn_threshold)" \
    MCBOTS_AUTO_SUMMARIZE_TOKEN_THRESHOLD="$(json_value agent.auto_summarize_token_threshold)" \
    MCBOTS_MAX_CONVERSATION_ROUNDS="$(json_value agent.max_conversation_rounds)" \
    MCBOTS_MODEL="$MODEL_ID" \
    MCBOTS_API_PROTOCOL="$API_PROTOCOL" \
    MCBOTS_ACTION_PROTOCOL="$ACTION_PROTOCOL" \
    MCBOTS_MODEL_PARAMS_JSON="$MODEL_PARAMS_JSON" \
    MCBOTS_RECORD_VIDEO="$(json_value record_video)" \
    MCBOTS_EVAL_MODE="true" \
    uv run python -m agent.main >"$LOG_DIR/agent.log" 2>&1 &
    AGENT_PID=$!
fi

PHASE="task_supervision"
while kill -0 "$MONITOR_PID" 2>/dev/null; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        write_terminal_request "server_crash"
    elif ! kill -0 "$CLIENT_PID" 2>/dev/null; then
        write_terminal_request "client_exit"
    elif [[ "$MODE" != "review" ]] && ! kill -0 "$AGENT_PID" 2>/dev/null; then
        if [[ -f "$RUN_DIR/control/agent-status.json" ]]; then
            AGENT_STOP_REASON="$(uv run python - "$RUN_DIR/control/agent-status.json" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("stop_reason", "agent_stopped"))
PY
)"
            case "$AGENT_STOP_REASON" in
                max_llm_request_successes|max_llm_request_failures)
                    write_terminal_request "step_limit"
                    ;;
                max_consecutive_llm_failures)
                    write_terminal_request "llm_failure_limit"
                    ;;
                claim_done_accepted|claim_attempts_exhausted)
                    ;;
                exception:*)
                    write_terminal_request "agent_crash"
                    ;;
                *)
                    write_terminal_request "agent_stopped_without_completion"
                    ;;
            esac
        else
            write_terminal_request "agent_crash"
        fi
    fi
    sleep 1
done

set +e
wait "$MONITOR_PID"
MONITOR_STATUS=$?
set -e

stop_agent_process

PHASE="message_finalization"
finalize_agent_messages

PHASE="result_collection"
uv run python - \
    "$RESULTS_DIR/agent-result.json" \
    "$MODE" \
    "${AGENT_PID:-}" \
    "$RUN_DIR/control/agent-status.json" \
    "$LOG_DIR/agent.log" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

status_path = Path(sys.argv[4])
agent_log = Path(sys.argv[5])
status = None
if status_path.is_file():
    status = json.loads(status_path.read_text(encoding="utf-8"))
decision_count = (
    int(status.get("llm_request_success", 0))
    if isinstance(status, dict)
    else (
        agent_log.read_text(encoding="utf-8", errors="replace").count("💬 Assistant:")
        if agent_log.is_file()
        else 0
    )
)
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(
        {
            "schema_version": 1,
            "mode": sys.argv[2],
            "agent_started": bool(sys.argv[3]),
            "decision_count": decision_count,
            "agent_status": status,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        handle,
        indent=2,
    )
    handle.write("\n")
PY

exit "$MONITOR_STATUS"
