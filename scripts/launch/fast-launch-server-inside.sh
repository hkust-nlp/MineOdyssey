SERVER_MEMORY_MIN=32G \
SERVER_MEMORY_MAX=100G \
DIFFICULTY=peaceful \
MAX_PLAYERS=1024 \
SERVER_FOREGROUND=true \
bash scripts/launch/run-server-only-inside.sh \
--runtime-config "/path/to/mcbots/shared/server-runtime-${ARNOLD_TRIAL_START_TIME}.json"