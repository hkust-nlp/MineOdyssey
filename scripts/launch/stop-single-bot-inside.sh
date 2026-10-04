#!/usr/bin/env bash
# Stop a single bot started by start-single-bot-inside.sh.
#
# Usage:
#   stop-single-bot-inside.sh <RUN_ID>     # match the suffix shown at launch
#   stop-single-bot-inside.sh <PID_FILE>   # or full path to .pids
#   stop-single-bot-inside.sh --bot Bot1   # convenience: stop newest matching RUN_ID for this bot

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
BASE_RUNTIME_DIR="${BASE_RUNTIME_DIR:-$PROJECT_ROOT/runtime}"

usage() {
    cat <<EOF
Usage: $0 (<RUN_ID> | <pid_file> | --bot <BOT_NAME>)

Stops the wrapper subshell, entrypoint, portablemc, Java client, Xvfb,
remote_bash_server, x11vnc, websockify — i.e. the whole tree spawned by
start-single-bot-inside.sh — using ps --ppid to walk the process tree.

Examples:
  $0 20260518_134937_390087
  $0 /workspace/mcbots/runtime/20260518_134937_390087.pids
  $0 --bot Bot1
EOF
}

if [[ $# -lt 1 ]]; then
    usage >&2
    exit 2
fi

PID_FILE=""
if [[ "$1" == "--bot" ]]; then
    if [[ $# -lt 2 ]]; then
        echo "ERROR: --bot needs a BOT_NAME" >&2
        exit 2
    fi
    BOT_NAME="$2"
    PID_FILE="$(ls -1t "$BASE_RUNTIME_DIR"/*.pids 2>/dev/null \
        | while read -r f; do grep -q "^CLIENT_NAME=$BOT_NAME$" "$f" 2>/dev/null && echo "$f" && break; done)"
    if [[ -z "$PID_FILE" ]]; then
        echo "ERROR: no .pids found in $BASE_RUNTIME_DIR for bot $BOT_NAME" >&2
        exit 1
    fi
elif [[ -f "$1" ]]; then
    PID_FILE="$1"
else
    PID_FILE="$BASE_RUNTIME_DIR/$1.pids"
    if [[ ! -f "$PID_FILE" ]]; then
        echo "ERROR: pid file not found: $PID_FILE" >&2
        exit 1
    fi
fi

# shellcheck disable=SC1090
source "$PID_FILE"

if [[ -z "${CLIENT_PID:-}" ]]; then
    echo "ERROR: $PID_FILE has no CLIENT_PID" >&2
    exit 1
fi

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

echo "Stopping bot tree rooted at PID $CLIENT_PID (RUN_ID=${RUN_ID:-?}, CLIENT_NAME=${CLIENT_NAME:-?})"
if kill -0 "$CLIENT_PID" 2>/dev/null; then
    kill_process_tree "$CLIENT_PID" TERM
    for i in $(seq 1 15); do
        kill -0 "$CLIENT_PID" 2>/dev/null || { echo "  ✓ exited after ${i}s"; break; }
        sleep 1
    done
    if kill -0 "$CLIENT_PID" 2>/dev/null; then
        echo "  ⚠ still alive after 15s, sending SIGKILL"
        kill_process_tree "$CLIENT_PID" KILL
    fi
else
    echo "  (wrapper PID $CLIENT_PID already gone — sweeping any leftover descendants by ppid walk)"
fi

# Even if the wrapper was already gone, the Java/Xvfb/etc. processes are now
# orphans (reparented to init), so ps --ppid CLIENT_PID can no longer find
# them. Fall back to pattern-matching against this run's known artifacts.
sweep_by_pattern() {
    local pattern="$1"
    local pids
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    for p in $pids; do
        kill -TERM "$p" 2>/dev/null || true
    done
    sleep 1
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    for p in $pids; do
        kill -KILL "$p" 2>/dev/null || true
    done
}

if [[ -n "${WORKSPACE_ROOT:-}" ]]; then
    # Targets things rooted in this bot's workspace (java --gameDir, portablemc, etc.)
    sweep_by_pattern "$WORKSPACE_ROOT"
fi
if [[ -n "${DISPLAY_INDEX:-}" ]]; then
    # Xvfb / x11vnc bound to this display
    sweep_by_pattern "Xvfb :$DISPLAY_INDEX"
    sweep_by_pattern "x11vnc.*:$DISPLAY_INDEX"
fi
if [[ -n "${REMOTE_BASH_PORT:-}" ]]; then
    sweep_by_pattern "remote_bash_server.py $REMOTE_BASH_PORT"
fi

echo "done"
