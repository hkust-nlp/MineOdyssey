#!/usr/bin/env bash
# Batch rollout: run agent in each given map for a capped number of decisions,
# then move on to the next map. Records go to agent_records/<rollout>/.
#
# Usage:
#   bash scripts/launch/batch_rollout.sh [map1 map2 ...]
# Example:
#   bash scripts/launch/batch_rollout.sh testmclevel torchlit
#
# Env vars (all optional):
#   MAX_DECISIONS        cap on LLM decisions per map (default 10)
#   MAX_WALL_SEC         hard kill after this many seconds per map (default 300)
#   MCBOTS_MODEL         LLM model (default moonshotai/kimi-k2.5)
#   MCBOTS_API_KEY       API key (defaults to OpenRouter key from api_models.json)
#   OBSERVE_INTERVAL     agent observation interval seconds (default 5.0)
#   MCBOTS_MAX_IMAGES    cap images in LLM context (default 25)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

MAPS=( "${@:-testmclevel torchlit}" )
if [[ ${#MAPS[@]} -eq 0 ]]; then
    MAPS=(testmclevel torchlit)
fi

MAX_DECISIONS="${MAX_DECISIONS:-10}"
MAX_WALL_SEC="${MAX_WALL_SEC:-300}"
MCBOTS_MODEL="${MCBOTS_MODEL:-kimi-k2.5}"
MCBOTS_BASE_URL="${MCBOTS_BASE_URL:-https://api.moonshot.cn/v1}"
MCBOTS_API_KEY="${MCBOTS_API_KEY:-<YOUR_API_KEY>}"
OBSERVE_INTERVAL="${OBSERVE_INTERVAL:-5.0}"
MCBOTS_MAX_IMAGES="${MCBOTS_MAX_IMAGES:-25}"
MCBOTS_LLM_TIMEOUT_SEC="${MCBOTS_LLM_TIMEOUT_SEC:-300}"
DISPLAY_RESOLUTION="${DISPLAY_RESOLUTION:-1024x768x24}"
if [[ -z "${MCBOTS_MODEL_PARAMS_JSON:-}" ]]; then
    MCBOTS_MODEL_PARAMS_JSON='{"max_tokens": 32000}'
fi

PROMPT=''

log() { echo "[batch $(date +%H:%M:%S)] $*"; }

stop_stack() {
    log "stop stack"
    for name in agent.main neoforge portablemc remote_bash_server Xvfb Xorg; do
        pids=$(pgrep -f "$name" 2>/dev/null || true)
        if [[ -n "$pids" ]]; then
            echo "$pids" | xargs -r kill -TERM 2>/dev/null || true
        fi
    done
    sleep 3
    for name in agent.main neoforge portablemc remote_bash_server Xvfb Xorg; do
        pids=$(pgrep -f "$name" 2>/dev/null || true)
        if [[ -n "$pids" ]]; then
            echo "$pids" | xargs -r kill -KILL 2>/dev/null || true
        fi
    done
    sleep 1
    # clean registries
    cd "$PROJECT_ROOT"
    rm -f runtime/port-registry.json runtime/port-registry.json.lock
    rm -f runtime/display-registry.json runtime/display-registry.json.lock
    rm -f runtime/*.pids runtime/server.json
}

wait_server_ready() {
    local deadline=$((SECONDS + 120))
    while [[ $SECONDS -lt $deadline ]]; do
        if mcrcon -H 127.0.0.1 -P 50001 -p minecraft "list" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

wait_bot_online() {
    local deadline=$((SECONDS + 240))
    while [[ $SECONDS -lt $deadline ]]; do
        if mcrcon -H 127.0.0.1 -P 50001 -p minecraft "list" 2>/dev/null | grep -qE "online:\s*bot"; then
            return 0
        fi
        sleep 3
    done
    return 1
}

run_one_map() {
    local map="$1"
    local run_ts=$(date +%Y%m%d_%H%M%S)

    log "=== MAP: $map ==="
    stop_stack

    log "switch world to $map"
    "$PROJECT_ROOT/custom_maps/switch_map.sh" "$map" >/dev/null
    # server.properties was set to peaceful by switch_map.sh;
    # we keep default difficulty here (per user request to skip stable env)
    sed -i 's/^difficulty=.*/difficulty=normal/' "$PROJECT_ROOT/server-data/server.properties"

    log "start server"
    cd "$PROJECT_ROOT"
    nohup bash -c "PORT_RANGE_START=50000 PORT_RANGE_END=60000 ./scripts/launch/run-server-only-inside.sh --runtime-config runtime/server.json" \
        > "runtime/server-launch.log" 2>&1 &
    SERVER_BG_PID=$!
    sleep 5
    if ! wait_server_ready; then
        log "ERROR: server failed to become ready, skipping $map"
        return 1
    fi
    log "server ready"

    log "start bot"
    nohup bash -c "PORT_RANGE_START=50000 PORT_RANGE_END=60000 SERVER_HOST=127.0.0.1 SERVER_PORT=50000 BOT_NAME=bot RENDER_MODE=gpu DISPLAY_RESOLUTION='$DISPLAY_RESOLUTION' GPU_PCI_BUSID=PCI:1:0:0 ./scripts/launch/start-single-bot-inside.sh" \
        > "runtime/bot-launch.log" 2>&1 &
    BOT_BG_PID=$!
    sleep 5
    if ! wait_bot_online; then
        log "ERROR: bot failed to come online, skipping $map"
        return 1
    fi
    log "bot online"

    # lock daylight to noon (avoid mob spawning at night, stable lighting for VLM)
    mcrcon -H 127.0.0.1 -P 50001 -p minecraft "time set day" >/dev/null 2>&1 || true
    mcrcon -H 127.0.0.1 -P 50001 -p minecraft "gamerule doDaylightCycle false" >/dev/null 2>&1 || true
    log "daylight locked"

    # fix runtime config display (entrypoint always writes :20 but gpu uses :0)
    python3 -c "
import json
p = '$PROJECT_ROOT/workspaces/main/bot/.mcbots_runtime.json'
d = json.load(open(p)); d['x11']['display'] = ':0'; json.dump(d, open(p, 'w'), indent=2)
"

    log "start agent (max_decisions=$MAX_DECISIONS, max_wall=$MAX_WALL_SEC s)"
    cd "$PROJECT_ROOT"
    unset HTTP_PROXY HTTPS_PROXY ALL_PROXY
    nohup bash -c "PYTHONPATH=. \
MCBOTS_REMOTE_BASH_HOST=127.0.0.1 \
MCBOTS_REMOTE_BASH_PORT=50003 \
MCBOTS_DISPLAY=:0 \
MCBOTS_WORKSPACE_ROOT=$PROJECT_ROOT/workspaces/main/bot \
MCBOTS_LOG_FILE_PATH=$PROJECT_ROOT/logs/latest.log \
MCBOTS_PLAYER=bot \
MCBOTS_MODEL='$MCBOTS_MODEL' \
MCBOTS_BASE_URL='$MCBOTS_BASE_URL' \
MCBOTS_API_KEY='$MCBOTS_API_KEY' \
MCBOTS_OBSERVE_INTERVAL=$OBSERVE_INTERVAL \
MCBOTS_DEFAULT_OBSERVE_ENABLED='${MCBOTS_DEFAULT_OBSERVE_ENABLED:-}' \
MCBOTS_ALLOW_MODEL_OBSERVE_TOGGLE='${MCBOTS_ALLOW_MODEL_OBSERVE_TOGGLE:-}' \
MCBOTS_MAX_IMAGES_IN_CONTEXT=$MCBOTS_MAX_IMAGES \
MCBOTS_LLM_TIMEOUT_SEC=$MCBOTS_LLM_TIMEOUT_SEC \
MCBOTS_MAX_LLM_REQUEST_SUCCESSES=$MAX_DECISIONS \
MCBOTS_INITIAL_USER_INPUT='$PROMPT' \
MCBOTS_MODEL_PARAMS_JSON='$MCBOTS_MODEL_PARAMS_JSON' \
MCBOTS_RECORD_VIDEO=false \
MCBOTS_ENABLE_SELF_REWARD='${MCBOTS_ENABLE_SELF_REWARD:-}' \
MCBOTS_SELF_REWARD_INCLUDE_STATE='${MCBOTS_SELF_REWARD_INCLUDE_STATE:-}' \
MCBOTS_SELF_REWARD_SCORE_MODE='${MCBOTS_SELF_REWARD_SCORE_MODE:-ternary}' \
MCBOTS_AUTO_SUMMARIZE_TURN_THRESHOLD='${MCBOTS_AUTO_SUMMARIZE_TURN_THRESHOLD:-}' \
MCBOTS_AUTO_SUMMARIZE_TOKEN_THRESHOLD='${MCBOTS_AUTO_SUMMARIZE_TOKEN_THRESHOLD:-}' \
MCBOTS_MAX_CONVERSATION_ROUNDS='${MCBOTS_MAX_CONVERSATION_ROUNDS:-}' \
MCBOTS_RCON_HOST='${MCBOTS_RCON_HOST:-127.0.0.1}' \
MCBOTS_RCON_PORT='${MCBOTS_RCON_PORT:-50001}' \
MCBOTS_RCON_PASSWORD='${MCBOTS_RCON_PASSWORD:-minecraft}' \
python3 -m agent.main" \
        > "runtime/agent-$map-$run_ts.log" 2>&1 &
    AGENT_BG_PID=$!

    # wait up to MAX_WALL_SEC
    local start_t=$SECONDS
    while [[ $((SECONDS - start_t)) -lt $MAX_WALL_SEC ]]; do
        if ! kill -0 $AGENT_BG_PID 2>/dev/null; then
            log "agent exited naturally (elapsed=$((SECONDS - start_t))s)"
            break
        fi
        sleep 5
    done

    if kill -0 $AGENT_BG_PID 2>/dev/null; then
        log "agent still alive after MAX_WALL_SEC, killing"
        kill -TERM $AGENT_BG_PID 2>/dev/null || true
        sleep 2
        kill -KILL $AGENT_BG_PID 2>/dev/null || true
    fi

    # tag the latest agent_records dir with map name
    local latest_record=$(ls -td "$PROJECT_ROOT/agent_records/"*/ 2>/dev/null | head -1)
    if [[ -n "$latest_record" ]]; then
        local newname="$PROJECT_ROOT/agent_records/rollout_${map}_${run_ts}"
        mv "$latest_record" "$newname"
        log "trajectory saved to: $newname"
        # quick stats
        local decisions=$(grep -cE "Decision triggered" "runtime/agent-$map-$run_ts.log" 2>/dev/null || echo 0)
        local screenshots=$(ls "$newname/screenshots/"*.jpg 2>/dev/null | wc -l)
        log "  decisions=$decisions  screenshots=$screenshots"
    else
        log "WARN: no agent_records dir created"
    fi
}

# ========== main ==========
log "Batch rollout: ${#MAPS[@]} maps: ${MAPS[*]}"
log "model=$MCBOTS_MODEL  max_decisions=$MAX_DECISIONS  max_wall=${MAX_WALL_SEC}s"

for MAP in "${MAPS[@]}"; do
    if run_one_map "$MAP"; then
        log "map $MAP done"
    else
        log "map $MAP failed"
    fi
done

stop_stack
log "All done. Results in $PROJECT_ROOT/agent_records/rollout_*/"
