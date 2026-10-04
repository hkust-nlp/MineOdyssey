#!/bin/bash
# 启动单个 AI Bot 客户端
# 用法: ./scripts/launch/start-single-bot.sh <BOT_NAME> <GPU_ID|cpu> [SERVER_HOST] [SERVER_PORT]

set -e

# 参数
BOT_NAME="$1"
RENDER_TARGET="$2"
SERVER_HOST="${3:-mc-server}"
SERVER_PORT="${4:-25565}"

# 验证参数
if [ -z "$BOT_NAME" ] || [ -z "$RENDER_TARGET" ]; then
    echo "用法: $0 <BOT_NAME> <GPU_ID|cpu> [SERVER_HOST] [SERVER_PORT]"
    echo ""
    echo "参数:"
    echo "  BOT_NAME      Bot 名称（容器名和玩家名）"
    echo "  GPU_ID|cpu    渲染模式：GPU编号(0,1,2...) 或 cpu"
    echo "  SERVER_HOST   服务端地址（默认: mc-server）"
    echo "  SERVER_PORT   服务端端口（默认: 25565）"
    echo ""
    echo "示例:"
    echo "  $0 Bot1 0                    # 在 GPU 0 上启动 Bot1"
    echo "  $0 Bot2 1                    # 在 GPU 1 上启动 Bot2"
    echo "  $0 Bot3 cpu                  # 使用 CPU 软渲染启动 Bot3"
    echo "  $0 MyBot 0 192.168.1.100     # 连接到外部服务端"
    exit 1
fi

# 配置
IMAGE_CLIENT="mc-agent-gpu-ubuntu2204"
NETWORK_NAME="mc-network"
CONTAINER_NAME="ai-agent-${BOT_NAME}"
AGENTBRIDGE_INTERNAL_PORT=8080
REMOTE_BASH_INTERNAL_PORT=9090
CLIENT_DISPLAY_INDEX="${DISPLAY_INDEX:-1}"

normalize_resolution() {
    local value="$1"
    if [[ "$value" =~ ^[0-9]+x[0-9]+$ ]]; then
        echo "${value}x24"
    elif [[ "$value" =~ ^[0-9]+x[0-9]+x[0-9]+$ ]]; then
        echo "$value"
    else
        echo ""
    fi
}

DISPLAY_RESOLUTION_INPUT="${DISPLAY_RESOLUTION:-800x600x24}"
DISPLAY_RESOLUTION="$(normalize_resolution "$DISPLAY_RESOLUTION_INPUT")"
if [ -z "$DISPLAY_RESOLUTION" ]; then
    echo "错误: DISPLAY_RESOLUTION 格式无效: $DISPLAY_RESOLUTION_INPUT"
    echo "支持格式: WIDTHxHEIGHT 或 WIDTHxHEIGHTxDEPTH (例如 800x600 或 800x600x24)"
    exit 1
fi

RENDER_MODE="gpu"
GPU_ID=""
DRV_VER=""
RENDER_DEVICE=""
XORG_BUSID=""

if [[ "${RENDER_TARGET,,}" == "cpu" ]]; then
    RENDER_MODE="cpu"
    RUNTIME_DISPLAY=":${CLIENT_DISPLAY_INDEX}"
    echo "渲染模式: CPU（将跳过 NVIDIA 检测和设备挂载）"
else
    GPU_ID="$RENDER_TARGET"
    RUNTIME_DISPLAY=":0"
    echo "渲染模式: GPU（GPU ID: $GPU_ID）"

    # 检测驱动版本
    DRV_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1)
    if [ -z "$DRV_VER" ]; then
        echo "错误: 无法检测 NVIDIA 驱动版本"
        exit 1
    fi

    # 查询指定 GPU 的 PCI BusID
    GPU_PCI_BUSID=$(nvidia-smi -i "$GPU_ID" --query-gpu=pci.bus_id --format=csv,noheader)
    if [ -z "$GPU_PCI_BUSID" ]; then
        echo "错误: 无法查询 GPU $GPU_ID 的 PCI BusID"
        exit 1
    fi

    # 提取 PCI 地址（格式：00000000:01:00.0 -> 0000:01:00.0）
    PCI_ADDR=$(echo "$GPU_PCI_BUSID" | tr '[:upper:]' '[:lower:]' | sed 's/^[0-9a-f]*:/0000:/')
    echo "GPU $GPU_ID PCI 地址: $PCI_ADDR"

    # 转换 PCI BusID 为 Xorg 格式（PCI:bus:dev:func）
    # 从 0000:01:00.0 转换为 PCI:1:0:0
    BUS=$(echo "$PCI_ADDR" | cut -d: -f2 | sed 's/^0*//')
    [ -z "$BUS" ] && BUS="0"
    DEV=$(echo "$PCI_ADDR" | cut -d: -f3 | cut -d. -f1 | sed 's/^0*//')
    [ -z "$DEV" ] && DEV="0"
    FUNC=$(echo "$PCI_ADDR" | cut -d. -f2)
    [ -z "$FUNC" ] && FUNC="0"
    XORG_BUSID="PCI:${BUS}:${DEV}:${FUNC}"
    echo "Xorg BusID: $XORG_BUSID"

    # 查找对应的 DRI renderD 设备
    RENDER_DEVICE=$(ls -l /dev/dri/by-path/ | grep "pci-${PCI_ADDR}-render" | awk '{print $NF}' | sed 's|^\.\./||')
    if [ -z "$RENDER_DEVICE" ]; then
        echo "错误: 无法找到 GPU $GPU_ID 的 DRI render 设备"
        exit 1
    fi
    echo "DRI render 设备: /dev/dri/$RENDER_DEVICE"
fi

# 检查或创建网络
if ! podman network exists "$NETWORK_NAME" 2>/dev/null; then
    echo "警告: 网络 $NETWORK_NAME 不存在，正在创建..."
    podman network create "$NETWORK_NAME"
fi

# 创建workspace目录
WORKSPACE_DIR="$(pwd)/workspaces/${BOT_NAME}"
if [ ! -d "$WORKSPACE_DIR" ]; then
    echo "创建workspace目录: $WORKSPACE_DIR"
    mkdir -p "$WORKSPACE_DIR"
fi

# 写入 workspace runtime 配置（供 MinecraftAPI/RemoteBashAPI 读取）
cat > "${WORKSPACE_DIR}/.mcbots_runtime.json" <<EOF
{
  "bot_name": "${BOT_NAME}",
  "workspace_root": "/workspace",
  "agentbridge": {
    "host": "127.0.0.1",
    "port": ${AGENTBRIDGE_INTERNAL_PORT}
  },
  "remote_bash": {
    "host": "127.0.0.1",
    "port": ${REMOTE_BASH_INTERNAL_PORT}
  },
  "x11": {
    "display": "${RUNTIME_DISPLAY}",
    "resolution": "${DISPLAY_RESOLUTION%x*}"
  }
}
EOF
echo "已写入 runtime 配置: ${WORKSPACE_DIR}/.mcbots_runtime.json"

# 清理同名容器
podman rm -f "$CONTAINER_NAME" 2>/dev/null || true

# 启动容器
echo "启动 $CONTAINER_NAME (玩家: $BOT_NAME, 渲染: $RENDER_MODE, 服务端: $SERVER_HOST:$SERVER_PORT)"
echo "显示分辨率: $DISPLAY_RESOLUTION"

PODMAN_ARGS=(
  run -d
  --name "$CONTAINER_NAME"
  --network "$NETWORK_NAME"
  --privileged
  -v "$(pwd)/scripts:/app/scripts:ro"
  -v "$(pwd)/docs:/app/docs:ro"
  -v "$(pwd)/agent:/app/agent:ro"
  -v "$(pwd)/game:/app/game"
  -v "$(pwd)/workspaces/${BOT_NAME}:/workspace"
  -e LD_LIBRARY_PATH=/usr/lib64:/opt/VirtualGL/lib64
  -e PLAYER_NAME="$BOT_NAME"
  -e SERVER_HOST="$SERVER_HOST"
  -e SERVER_PORT="$SERVER_PORT"
  -e RENDER_MODE="$RENDER_MODE"
  -e DISPLAY_INDEX="$CLIENT_DISPLAY_INDEX"
  -e DISPLAY_RESOLUTION="$DISPLAY_RESOLUTION"
)

if [ "$RENDER_MODE" = "gpu" ]; then
  PODMAN_ARGS+=(
    --device "/dev/nvidia${GPU_ID}"
    --device /dev/nvidiactl
    --device /dev/nvidia-uvm
    --device /dev/nvidia-modeset
    --device "/dev/dri/${RENDER_DEVICE}"
    -v /usr/bin/nvidia-smi:/usr/bin/nvidia-smi:ro
    -v "/usr/lib64/libcuda.so.${DRV_VER}:/usr/lib64/libcuda.so.1:ro"
    -v "/usr/lib64/libnvidia-ml.so.${DRV_VER}:/usr/lib64/libnvidia-ml.so.1:ro"
    -v "/usr/lib64/libGLX_nvidia.so.${DRV_VER}:/usr/lib64/libGLX_nvidia.so.0:ro"
    -v "/usr/lib64/libEGL_nvidia.so.${DRV_VER}:/usr/lib64/libEGL_nvidia.so.0:ro"
    -v "/usr/lib64/libnvidia-glcore.so.${DRV_VER}:/usr/lib64/libnvidia-glcore.so.${DRV_VER}:ro"
    -v "/usr/lib64/libnvidia-glsi.so.${DRV_VER}:/usr/lib64/libnvidia-glsi.so.${DRV_VER}:ro"
    -v "/usr/lib64/libnvidia-tls.so.${DRV_VER}:/usr/lib64/libnvidia-tls.so.${DRV_VER}:ro"
    -v "/usr/lib64/libnvidia-gpucomp.so.${DRV_VER}:/usr/lib64/libnvidia-gpucomp.so.${DRV_VER}:ro"
    -v /usr/lib64/xorg/modules/drivers/nvidia_drv.so:/usr/lib/xorg/modules/drivers/nvidia_drv.so:ro
    -v /usr/lib64/xorg/modules/extensions/libglx.so:/usr/lib/xorg/modules/extensions/libglx.so:ro
    -v /usr/lib64/xorg/modules/extensions/libglxserver_nvidia.so:/usr/lib/xorg/modules/extensions/libglxserver_nvidia.so:ro
    -e RENDER_DEVICE="$RENDER_DEVICE"
    -e GPU_PCI_BUSID="$XORG_BUSID"
  )
fi

PODMAN_ARGS+=("$IMAGE_CLIENT" /app/scripts/entrypoints/entrypoint.sh)
podman "${PODMAN_ARGS[@]}"

echo "✓ $CONTAINER_NAME 已启动"
