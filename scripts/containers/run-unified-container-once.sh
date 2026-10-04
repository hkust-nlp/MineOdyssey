#!/usr/bin/env bash

set -euo pipefail

IMAGE="${IMAGE:-mc-agent-unified-ubuntu2204}"
CONTAINER_NAME="${CONTAINER_NAME:-mc-unified}"
HOST_ROOT="${HOST_ROOT:-$(pwd)}"
CONTAINER_ROOT="${CONTAINER_ROOT:-/workspace/mcbots}"
NETWORK_NAME="${NETWORK_NAME:-}"
SHARED_PORT_RANGE="${SHARED_PORT_RANGE:-20000-30000}"
CONTAINER_CPUS="${CONTAINER_CPUS:-}"
CONTAINER_MEMORY="${CONTAINER_MEMORY:-}"
CONTAINER_MEMORY_SWAP="${CONTAINER_MEMORY_SWAP:-}"
CONTAINER_PIDS_LIMIT="${CONTAINER_PIDS_LIMIT:-32768}"
CONTAINER_SHM_SIZE="${CONTAINER_SHM_SIZE:-2g}"
CONTAINER_MASK_DOT_DIRS="${CONTAINER_MASK_DOT_DIRS-auto}"
CONTAINER_DOT_DIR_TMPFS_OPTIONS="${CONTAINER_DOT_DIR_TMPFS_OPTIONS:-rw,exec,nosuid,nodev,size=64m}"

# 占位接口：后续可把 GPU 设备/库挂载参数放进来（空字符串表示 CPU-only）
# 示例:
#   GPU_ARGS_STRING="--device /dev/nvidia0 --device /dev/nvidiactl"
GPU_ARGS_STRING="${GPU_ARGS_STRING:-}"

usage() {
    cat <<'EOF'
用法:
  ./scripts/containers/run-unified-container-once.sh [--port-range START-END] [--] <额外 podman run 参数>

选项:
  --port-range, -p RANGE   覆盖 SHARED_PORT_RANGE（格式: START-END，如 30000-40000）

常用环境变量:
  IMAGE             镜像名（默认: mc-agent-unified-ubuntu2204）
  CONTAINER_NAME    容器名（默认: mc-unified）
  HOST_ROOT         宿主机项目根目录（默认: 当前目录）
  CONTAINER_ROOT    容器内项目目录（默认: /workspace/mcbots）
  NETWORK_NAME      可选，显式指定 --network（默认空，交给运行时默认网络）
  SHARED_PORT_RANGE 宿主机与容器共享端口范围（默认: 20000-30000，亦可用 --port-range 覆盖）
  CONTAINER_CPUS    可选，CPU 配额（例如 8）
  CONTAINER_MEMORY  可选，内存上限（例如 32g）
  CONTAINER_MEMORY_SWAP 可选，内存+swap 上限（例如 40g）
  CONTAINER_PIDS_LIMIT 进程数上限（默认: 32768）
  CONTAINER_SHM_SIZE /dev/shm 大小（默认: 2g）
  CONTAINER_MASK_DOT_DIRS 顶层隐藏目录遮罩（默认: auto，即遮住所有顶层 .开头目录；设为空可禁用）
  CONTAINER_DOT_DIR_TMPFS_OPTIONS 隐藏目录 tmpfs 选项（默认: rw,exec,nosuid,nodev,size=64m）
  GPU_ARGS_STRING   GPU 参数占位（默认空）

说明:
  - 该脚本只负责启动容器，不在容器内启动 server/client。
  - 只挂载一次项目根目录: HOST_ROOT -> CONTAINER_ROOT
  - 默认会用 tmpfs 遮住容器内的顶层隐藏目录（如 .git/.venv），避免容器写到宿主机元数据。
  - 会共享 SHARED_PORT_RANGE 端口段，便于主链路/eval 动态端口直接从宿主机访问。
  - 启动后进入容器执行:
      ./scripts/launch/start-server-clients-inside.sh
EOF
}

EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -p|--port-range)
            if [[ $# -lt 2 || -z "${2:-}" ]]; then
                echo "错误: $1 需要一个参数，格式 START-END"
                exit 1
            fi
            SHARED_PORT_RANGE="$2"
            shift 2
            ;;
        --port-range=*)
            SHARED_PORT_RANGE="${1#*=}"
            shift
            ;;
        --)
            shift
            EXTRA_ARGS=("$@")
            break
            ;;
        *)
            echo "错误: 未知参数: $1"
            echo "使用 -h 查看帮助。"
            exit 1
            ;;
    esac
done

if [[ ! "$SHARED_PORT_RANGE" =~ ^[0-9]+-[0-9]+$ ]]; then
    echo "错误: SHARED_PORT_RANGE 格式非法: '$SHARED_PORT_RANGE'（应为 START-END）"
    exit 1
fi

GPU_ARGS=()
if [[ -n "$GPU_ARGS_STRING" ]]; then
    # shellcheck disable=SC2206
    GPU_ARGS=($GPU_ARGS_STRING)
fi

mkdir -p "$HOST_ROOT/runtime" "$HOST_ROOT/logs" "$HOST_ROOT/server-data" "$HOST_ROOT/server-mods" "$HOST_ROOT/clients"

DOT_DIR_MASKS=()

add_dot_dir_mask() {
    local name="$1"
    local existing

    name="${name%/}"
    name="${name##*/}"
    if [[ -z "$name" || "$name" == "." || "$name" == ".." ]]; then
        return
    fi
    if [[ "$name" != .* ]]; then
        name=".$name"
    fi
    for existing in "${DOT_DIR_MASKS[@]}"; do
        if [[ "$existing" == "$name" ]]; then
            return
        fi
    done
    DOT_DIR_MASKS+=("$name")
}

if [[ "$CONTAINER_MASK_DOT_DIRS" == "auto" ]]; then
    add_dot_dir_mask ".git"
    add_dot_dir_mask ".venv"
    shopt -s nullglob
    for dot_dir in "$HOST_ROOT"/.[!.]* "$HOST_ROOT"/..?*; do
        [[ -d "$dot_dir" ]] || continue
        add_dot_dir_mask "$dot_dir"
    done
    shopt -u nullglob
elif [[ -n "$CONTAINER_MASK_DOT_DIRS" ]]; then
    # shellcheck disable=SC2206
    CONFIGURED_DOT_DIR_MASKS=($CONTAINER_MASK_DOT_DIRS)
    for dot_dir in "${CONFIGURED_DOT_DIR_MASKS[@]}"; do
        add_dot_dir_mask "$dot_dir"
    done
fi

if podman container exists "$CONTAINER_NAME" 2>/dev/null; then
    echo "错误: 容器已存在: $CONTAINER_NAME"
    echo "请先删除或更换 CONTAINER_NAME。"
    exit 1
fi

echo "启动统一容器: $CONTAINER_NAME"
echo "  镜像: $IMAGE"
echo "  项目目录: $HOST_ROOT"
echo "  容器目录: $CONTAINER_ROOT"
echo "  共享端口: $SHARED_PORT_RANGE"
echo "  资源配额: cpus=${CONTAINER_CPUS:-<runtime default>} memory=${CONTAINER_MEMORY:-<runtime default>} memory_swap=${CONTAINER_MEMORY_SWAP:-<runtime default>} pids_limit=${CONTAINER_PIDS_LIMIT} shm_size=${CONTAINER_SHM_SIZE}"
echo "  GPU_ARGS_STRING: ${GPU_ARGS_STRING:-<empty>}"
if [[ ${#DOT_DIR_MASKS[@]} -gt 0 ]]; then
    printf "  隐藏目录遮罩:"
    printf " %s" "${DOT_DIR_MASKS[@]}"
    printf "\n"
else
    echo "  隐藏目录遮罩: <none>"
fi
if [[ -n "$NETWORK_NAME" ]]; then
    echo "  网络: $NETWORK_NAME"
else
    echo "  网络: <runtime default>"
fi

PODMAN_ARGS=(
    run -d
    --name "$CONTAINER_NAME"
    --init
    --privileged
    --pids-limit "$CONTAINER_PIDS_LIMIT"
    --shm-size "$CONTAINER_SHM_SIZE"
    -w "$CONTAINER_ROOT"
    -v "$HOST_ROOT:$CONTAINER_ROOT"
    -p "$SHARED_PORT_RANGE:$SHARED_PORT_RANGE"
)

for dot_dir in "${DOT_DIR_MASKS[@]}"; do
    PODMAN_ARGS+=(--tmpfs "$CONTAINER_ROOT/$dot_dir:$CONTAINER_DOT_DIR_TMPFS_OPTIONS")
done

if [[ -n "$CONTAINER_CPUS" ]]; then
    PODMAN_ARGS+=(--cpus "$CONTAINER_CPUS")
fi
if [[ -n "$CONTAINER_MEMORY" ]]; then
    PODMAN_ARGS+=(--memory "$CONTAINER_MEMORY")
fi
if [[ -n "$CONTAINER_MEMORY_SWAP" ]]; then
    PODMAN_ARGS+=(--memory-swap "$CONTAINER_MEMORY_SWAP")
fi

if [[ -n "$NETWORK_NAME" ]]; then
    PODMAN_ARGS+=(--network "$NETWORK_NAME")
fi

PODMAN_ARGS+=("${GPU_ARGS[@]}" "${EXTRA_ARGS[@]}" "$IMAGE" sleep infinity)
podman "${PODMAN_ARGS[@]}"

echo
echo "容器已启动。下一步:"
echo "  podman exec -it $CONTAINER_NAME bash"
echo "  cd $CONTAINER_ROOT"
echo "  ./scripts/launch/start-server-clients-inside.sh"
