# we for not just use TRAIL_START_TIME as GAME_ID as this is unique. 
# If we want to run multiple games on the same machine, we need to use a preset GAME_ID.
# and for each run, the runtime-config will be refreshed by $ARNOLD_TRIAL_START_TIME.
MCBOTS_PERSISTENT_ROOT=/path/to/mcbots/shared/games \
GAME_ID=${ARNOLD_TRIAL_START_TIME} \
SERVER_MEMORY_MIN=32G \
SERVER_MEMORY_MAX=100G \
DIFFICULTY=peaceful \
MAX_PLAYERS=1024 \
SERVER_FOREGROUND=true \
bash scripts/launch/run-server-only-inside.sh \
--runtime-config "/path/to/mcbots/shared/server-runtime-${ARNOLD_TRIAL_START_TIME}.json"