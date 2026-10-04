#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Bot to attach the agent to (default Bot1). Usage: fast-launch-agent.sh [BOT_NAME]
BOT_NAME="${1:-Bot1}"

# Load secrets / provider config from project-local .env (gitignored).
# Expected keys: MCBOTS_BASE_URL, MCBOTS_API_KEY, MCBOTS_MODEL.
if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  echo "ERROR: $PROJECT_ROOT/.env not found. Create it with MCBOTS_BASE_URL / MCBOTS_API_KEY / MCBOTS_MODEL." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source "$PROJECT_ROOT/.env"
set +a

# Read ports / display / workspace from the bot's runtime config
# (written by start-single-bot-inside.sh). No fallback — error if missing.
RUNTIME_CONFIG="$PROJECT_ROOT/workspaces/main/$BOT_NAME/.mcbots_runtime.json"
if [[ ! -f "$RUNTIME_CONFIG" ]]; then
  echo "ERROR: runtime config not found: $RUNTIME_CONFIG" >&2
  echo "  Is $BOT_NAME started? (scripts/launch/start-single-bot-inside.sh $BOT_NAME)" >&2
  exit 1
fi

{
  read -r WORKSPACE_ROOT
  read -r REMOTE_BASH_HOST
  read -r REMOTE_BASH_PORT
  read -r DISPLAY_ID
} < <(python3 -c "
import json
c = json.load(open('$RUNTIME_CONFIG'))
print(c['workspace_root'])
print(c['remote_bash']['host'])
print(c['remote_bash']['port'])
print(c['x11']['display'])
")

# Minecraft player/entity name controlled by this agent.
export MCBOTS_PLAYER="$BOT_NAME"
# Remote bash service host from the bot runtime config.
export MCBOTS_REMOTE_BASH_HOST="$REMOTE_BASH_HOST"
# Remote bash service port from the bot runtime config.
export MCBOTS_REMOTE_BASH_PORT="$REMOTE_BASH_PORT"
# X11 display used by the bot client.
export MCBOTS_DISPLAY="$DISPLAY_ID"
# Per-bot workspace root for files, logs, and helper APIs.
export MCBOTS_WORKSPACE_ROOT="$WORKSPACE_ROOT"
# Runtime config path produced by the bot launch script.
export MCBOTS_RUNTIME_CONFIG="$RUNTIME_CONFIG"
# Minecraft latest.log path watched for chat/system messages.
export MCBOTS_LOG_FILE_PATH="$WORKSPACE_ROOT/game/logs/latest.log"

# Trigger summary when active context image count exceeds this value; <=0 disables.
export MCBOTS_MAX_IMAGES_IN_CONTEXT=20
# Trigger summary when active non-system message count exceeds this value.
export MCBOTS_MAX_CONVERSATION_ROUNDS=40
# Trigger summary when previous request total_tokens exceeds this value; <=0 disables.
export MCBOTS_AUTO_SUMMARIZE_TOKEN_THRESHOLD=100000
# Trigger summary when assistant turns in current window reach this value; <=0 disables.
export MCBOTS_AUTO_SUMMARIZE_TURN_THRESHOLD=20

# Keep one screenshot at least this often when frame filtering is active.
export MCBOTS_FRAME_KEEPALIVE_SEC=30
# Debug-only random episode reward for ROLL notifications; keep 0 for real runs.
export MCBOTS_DEBUG_RANDOM_REWARD=0
# OpenAI-compatible client timeout per LLM request, in seconds.
export MCBOTS_LLM_TIMEOUT_SEC=300
# OpenAI-compatible client retry count per request.
export MCBOTS_LLM_MAX_RETRIES=2
# Log a heartbeat when an LLM request stays pending this many seconds; <=0 disables.
export MCBOTS_LLM_WATCHDOG_INTERVAL_SEC=30
# Extra request body passed to chat completions; enables retained reasoning on official API.
export MCBOTS_MODEL_PARAMS_JSON='{"thinking":{"type":"enabled","keep":"all"},"chat_template_kwargs": {"enable_thinking": True}}'

# Start in streaming screenshots (true) or event_only screenshots (false).
export MCBOTS_DEFAULT_OBSERVE_ENABLED=false
# Expose start_observe/stop_observe actions to the model.
export MCBOTS_ALLOW_MODEL_OBSERVE_TOGGLE=false
# Automatically respawn the bot after death.
export MCBOTS_AUTO_RESPAWN=false
# Auto-respawn polling interval, in seconds.
export MCBOTS_AUTO_RESPAWN_INTERVAL=2.0
# Include Baritone docs/actions in the agent system prompt.
export MCBOTS_ENABLE_BARITONE=0
# Enable out-of-band self-reward grading at window boundaries.
export MCBOTS_ENABLE_SELF_REWARD=1
# Include structured game state snapshots in self-reward grading.
export MCBOTS_SELF_REWARD_INCLUDE_STATE=1
# Reuse the agent system prompt for grading (true=KV cache friendly; false=use independent GRADER_SYSTEM_PROMPT).
export MCBOTS_SELF_REWARD_SHARE_SYSTEM_PROMPT=true
# In event_only mode, send a 6-view panorama mosaic (up/feet/main/left/back/right) instead of a single frame
# whenever the bot is stationary and no GUI is open; also adds a note about it to the agent system prompt.
export MCBOTS_ENABLE_PANORAMA=true

export MCBOTS_INITIAL_USER_INPUT='build a house using logs!'

UV_PROJECT_ENVIRONMENT=/tmp/mcbots-uv-env uv run -m agent.main
