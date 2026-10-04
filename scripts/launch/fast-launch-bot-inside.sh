#!/usr/bin/env bash
# 快速启动单个 bot，复用 fast-launch-server-inside.sh 写入的 server runtime 配置。
#
# 用法:
#   bash scripts/launch/fast-launch-bot-inside.sh [BOT_NAME]
#
# 可控环境变量:
#   CLIENT_FOREGROUND=true|false   (默认 true)
#
# 前提:
#   - fast-launch-server-inside.sh 已在某台机器上启动（共享存储路径一致）
#   - 环境变量 ARNOLD_MCSERVER_0_HOST / ARNOLD_TRIAL_START_TIME 已注入
set -euo pipefail

BOT_NAME="${1:-Bot1}"
CLIENT_FOREGROUND="${CLIENT_FOREGROUND:-true}"

: "${ARNOLD_MCSERVER_0_HOST:?ARNOLD_MCSERVER_0_HOST not set}"
: "${ARNOLD_TRIAL_START_TIME:?ARNOLD_TRIAL_START_TIME not set}"

SERVER_RUNTIME_CONFIG="/path/to/mcbots/shared/server-runtime-${ARNOLD_TRIAL_START_TIME}.json"
MAX_WAIT_TIME=900

echo "等待 server runtime 配置: $SERVER_RUNTIME_CONFIG"
start_time=$(date +%s)
while [ ! -f "$SERVER_RUNTIME_CONFIG" ]; do
    sleep 1
    elapsed=$(( $(date +%s) - start_time ))
    if [ "$elapsed" -gt "$MAX_WAIT_TIME" ]; then
        echo "错误: ${MAX_WAIT_TIME}s 内未发现 server runtime 配置文件，退出" >&2
        exit 1
    fi
done
elapsed=$(( $(date +%s) - start_time ))
echo "  ✓ 已发现 server runtime 配置 (耗时 ${elapsed}s)"

MC_SERVER_HOST="$ARNOLD_MCSERVER_0_HOST"
MC_SERVER_PORT=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['server']['port'])" "$SERVER_RUNTIME_CONFIG")

echo "  MC_SERVER_HOST=${MC_SERVER_HOST}"
echo "  MC_SERVER_PORT=${MC_SERVER_PORT}"
echo "  BOT_NAME=${BOT_NAME}"
echo "  CLIENT_FOREGROUND=${CLIENT_FOREGROUND}"

SERVER_HOST="$MC_SERVER_HOST" \
SERVER_PORT="$MC_SERVER_PORT" \
CLIENT_FOREGROUND="$CLIENT_FOREGROUND" \
    bash scripts/launch/start-single-bot-inside.sh "$BOT_NAME"
