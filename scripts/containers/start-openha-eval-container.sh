#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

HOST_ROOT="${HOST_ROOT:-$PROJECT_ROOT}"
IMAGE="${IMAGE:-mc-agent-unified-ubuntu2204}"
CONTAINER_NAME="${CONTAINER_NAME:-mc-openha-eval}"
NETWORK_NAME="${NETWORK_NAME:-bridge}"
SERVER_PORT="${SERVER_PORT:-25585}"
RCON_PORT="${RCON_PORT:-25595}"

usage() {
    cat <<'EOF'
用法:
  ./scripts/containers/start-openha-eval-container.sh

说明:
  - 该脚本复用主容器启动脚本 run-unified-container-once.sh。
  - 只做最小挂载补充，便于在容器内运行 eval runner。

可选环境变量:
  HOST_ROOT       项目根目录（默认: 当前仓库根）
  IMAGE           容器镜像（默认: mc-agent-unified-ubuntu2204）
  CONTAINER_NAME  容器名（默认: mc-openha-eval）
  NETWORK_NAME    网络（默认: bridge）
  SERVER_PORT     宿主机映射到容器 25565（默认: 25585）
  RCON_PORT       宿主机映射到容器 25575（默认: 25595）
  GPU_ARGS_STRING 透传给 run-unified-container-once.sh（默认空）

启动后执行:
  podman exec -it <容器名> bash
  /app/scripts/eval/run-openha-task-eval-inside.sh
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

EXTRA_RUN_ARGS=("$@")

IMAGE="$IMAGE" \
CONTAINER_NAME="$CONTAINER_NAME" \
HOST_ROOT="$HOST_ROOT" \
NETWORK_NAME="$NETWORK_NAME" \
SERVER_PORT="$SERVER_PORT" \
RCON_PORT="$RCON_PORT" \
"${SCRIPT_DIR}/run-unified-container-once.sh" \
    -- \
    -v "${HOST_ROOT}/eval:/workspace/eval" \
    -v "${HOST_ROOT}/config:/workspace/config:ro" \
    -v "${HOST_ROOT}/agent:/workspace/agent" \
    -v "${HOST_ROOT}/src:/workspace/src" \
    -v "${HOST_ROOT}/pyproject.toml:/workspace/pyproject.toml:ro" \
    -v "${HOST_ROOT}/README.md:/workspace/README.md:ro" \
    -e PYTHONPATH=/workspace \
    -e RCON_PASSWORD=minecraft \
    -e ENABLE_RCON=true \
    -e SERVER_PORT=25565 \
    -e RCON_PORT=25575 \
    -e MC_VERSION=1.21.1 \
    -e NEOFORGE_VERSION=21.1.217 \
    "${EXTRA_RUN_ARGS[@]}"
