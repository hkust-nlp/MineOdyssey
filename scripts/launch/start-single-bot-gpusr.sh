#!/bin/bash
# 启动单个带 GPU Screen Recorder 的 AI Bot 客户端
# 用法: ./scripts/launch/start-single-bot-gpusr.sh <BOT_NAME> <GPU_ID> [SERVER_HOST] [SERVER_PORT]
#
# 特点：零拷贝高性能捕获，延迟 ~20-30ms，60+ FPS

set -e

# 参数
BOT_NAME="$1"
GPU_ID="$2"
SERVER_HOST="${3:-mc-server}"
SERVER_PORT="${4:-25565}"

# 验证参数
if [ -z "$BOT_NAME" ] || [ -z "$GPU_ID" ]; then
    echo "用法: $0 <BOT_NAME> <GPU_ID> [SERVER_HOST] [SERVER_PORT]"
    echo ""
    echo "参数:"
    echo "  BOT_NAME      Bot 名称（容器名和玩家名）"
    echo "  GPU_ID        使用的 GPU ID (0, 1, 2, ...)"
    echo "  SERVER_HOST   服务端地址（默认: mc-server）"
    echo "  SERVER_PORT   服务端端口（默认: 25565）"
    echo ""
    echo "示例:"
    echo "  $0 Bot1 0                    # 在 GPU 0 上启动 Bot1"
    echo "  $0 Bot2 1                    # 在 GPU 1 上启动 Bot2"
    echo "  $0 MyBot 0 192.168.1.100     # 连接到外部服务端"
    echo ""
    echo "性能特点:"
    echo "  - 零拷贝GPU捕获（直接从显存读取）"
    echo "  - 延迟: ~20-30ms（比 x11vnc 快 8 倍）"
    echo "  - 帧率: 60+ FPS（比 VNC 快 10 倍）"
    echo "  - CPU 占用: 几乎为 0"
    echo ""
    echo "视频流端口: 自动从 9999 开始查找可用端口"
    echo "访问方式: http://<服务器IP>:<端口>"
    exit 1
fi

# 配置
IMAGE_CLIENT="mc-agent-gpu-gpusr-ubuntu2204"
NETWORK_NAME="mc-network"
CONTAINER_NAME="ai-agent-${BOT_NAME}"

# 查找可用端口的函数
find_available_port() {
    local base_port=$1
    local port=$base_port
    while ss -tuln | grep -q ":$port " 2>/dev/null; do
        port=$((port + 1))
    done
    echo $port
}

# 自动分配可用端口
GPUSR_PORT=$(find_available_port 9999)
AGENTBRIDGE_PORT=$(find_available_port 8080)
REMOTE_BASH_PORT=$(find_available_port 9090)
AGENTBRIDGE_INTERNAL_PORT=8080
REMOTE_BASH_INTERNAL_PORT=9090

# 可选配置（可通过环境变量覆盖）
GPUSR_FPS="${GPUSR_FPS:-60}"
GPUSR_QUALITY="${GPUSR_QUALITY:-very_high}"
GPUSR_CODEC="${GPUSR_CODEC:-h264}"

# 检测驱动版本
DRV_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1)
if [ -z "$DRV_VER" ]; then
    echo "错误: 无法检测 NVIDIA 驱动版本"
    exit 1
fi

echo "检测到驱动版本: $DRV_VER"

# 检查驱动版本是否支持 NVFBC（需要 >= 550）
DRV_MAJOR=$(echo $DRV_VER | cut -d. -f1)
if [ "$DRV_MAJOR" -lt 550 ]; then
    echo "⚠ 警告: 驱动版本 $DRV_VER < 550，NVFBC 可能不可用"
    echo "  建议升级驱动到 >= 550 以获得最佳性能"
else
    echo "✓ 驱动版本支持 NVFBC"
fi

# 查询指定 GPU 的 PCI BusID
GPU_PCI_BUSID=$(nvidia-smi -i $GPU_ID --query-gpu=pci.bus_id --format=csv,noheader)
if [ -z "$GPU_PCI_BUSID" ]; then
    echo "错误: 无法查询 GPU $GPU_ID 的 PCI BusID"
    exit 1
fi

# 提取 PCI 地址
PCI_ADDR=$(echo $GPU_PCI_BUSID | tr '[:upper:]' '[:lower:]' | sed 's/^[0-9a-f]*:/0000:/')
echo "GPU $GPU_ID PCI 地址: $PCI_ADDR"

# 转换 PCI BusID 为 Xorg 格式
BUS=$(echo $PCI_ADDR | cut -d: -f2 | sed 's/^0*//')
[ -z "$BUS" ] && BUS="0"
DEV=$(echo $PCI_ADDR | cut -d: -f3 | cut -d. -f1 | sed 's/^0*//')
[ -z "$DEV" ] && DEV="0"
FUNC=$(echo $PCI_ADDR | cut -d. -f2)
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

# 检查或创建网络
if ! podman network exists $NETWORK_NAME 2>/dev/null; then
    echo "创建网络 $NETWORK_NAME..."
    podman network create $NETWORK_NAME
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
    "display": ":0",
    "resolution": "1024x768"
  }
}
EOF
echo "已写入 runtime 配置: ${WORKSPACE_DIR}/.mcbots_runtime.json"

# 清理同名容器
podman rm -f $CONTAINER_NAME 2>/dev/null || true

# 启动容器
echo ""
echo "=========================================="
echo "  启动 $CONTAINER_NAME (GPU Screen Recorder 版)"
echo "=========================================="
echo "  玩家: $BOT_NAME"
echo "  GPU: $GPU_ID"
echo "  服务端: $SERVER_HOST:$SERVER_PORT"
echo "  视频流: http://<服务器IP>:$GPUSR_PORT"
echo "  配置: ${GPUSR_FPS}fps / ${GPUSR_QUALITY} / ${GPUSR_CODEC}"
echo "=========================================="

podman run -d \
  --name $CONTAINER_NAME \
  --network $NETWORK_NAME \
  --privileged \
  --device /dev/nvidia${GPU_ID} \
  --device /dev/nvidiactl \
  --device /dev/nvidia-uvm \
  --device /dev/nvidia-modeset \
  --device /dev/dri/${RENDER_DEVICE} \
  -v /usr/bin/nvidia-smi:/usr/bin/nvidia-smi:ro \
  -v /usr/lib64/libcuda.so.${DRV_VER}:/usr/lib64/libcuda.so.1:ro \
  -v /usr/lib64/libnvidia-ml.so.${DRV_VER}:/usr/lib64/libnvidia-ml.so.1:ro \
  -v /usr/lib64/libGLX_nvidia.so.${DRV_VER}:/usr/lib64/libGLX_nvidia.so.0:ro \
  -v /usr/lib64/libEGL_nvidia.so.${DRV_VER}:/usr/lib64/libEGL_nvidia.so.0:ro \
  -v /usr/lib64/libnvidia-glcore.so.${DRV_VER}:/usr/lib64/libnvidia-glcore.so.${DRV_VER}:ro \
  -v /usr/lib64/libnvidia-glsi.so.${DRV_VER}:/usr/lib64/libnvidia-glsi.so.${DRV_VER}:ro \
  -v /usr/lib64/libnvidia-tls.so.${DRV_VER}:/usr/lib64/libnvidia-tls.so.${DRV_VER}:ro \
  -v /usr/lib64/libnvidia-gpucomp.so.${DRV_VER}:/usr/lib64/libnvidia-gpucomp.so.${DRV_VER}:ro \
  -v /usr/lib64/libnvidia-encode.so.${DRV_VER}:/usr/lib64/libnvidia-encode.so.${DRV_VER}:ro \
  -v /usr/lib64/libnvidia-encode.so.${DRV_VER}:/usr/lib64/libnvidia-encode.so.1:ro \
  -v /usr/lib64/libnvidia-fbc.so.${DRV_VER}:/usr/lib64/libnvidia-fbc.so.1:ro \
  -v /usr/lib64/xorg/modules/drivers/nvidia_drv.so:/usr/lib/xorg/modules/drivers/nvidia_drv.so:ro \
  -v /usr/lib64/xorg/modules/extensions/libglx.so:/usr/lib/xorg/modules/extensions/libglx.so:ro \
  -v /usr/lib64/xorg/modules/extensions/libglxserver_nvidia.so:/usr/lib/xorg/modules/extensions/libglxserver_nvidia.so:ro \
  -v $(pwd)/scripts:/app/scripts:ro \
  -v $(pwd)/docs:/app/docs:ro \
  -v $(pwd)/agent:/app/agent:ro \
  -v $(pwd)/game:/app/game \
  -v $(pwd)/workspaces/${BOT_NAME}:/workspace \
  -p $GPUSR_PORT:9999 \
  -p $AGENTBRIDGE_PORT:8080 \
  -p $REMOTE_BASH_PORT:9090 \
  -e LD_LIBRARY_PATH=/usr/lib64:/opt/VirtualGL/lib64 \
  -e PLAYER_NAME=$BOT_NAME \
  -e SERVER_HOST=$SERVER_HOST \
  -e SERVER_PORT=$SERVER_PORT \
  -e RENDER_DEVICE=$RENDER_DEVICE \
  -e GPU_PCI_BUSID=$XORG_BUSID \
  -e GPUSR_FPS=$GPUSR_FPS \
  -e GPUSR_QUALITY=$GPUSR_QUALITY \
  -e GPUSR_CODEC=$GPUSR_CODEC \
  -e GPUSR_OUTPUT_PORT=9999 \
  $IMAGE_CLIENT \
  /app/scripts/entrypoints/entrypoint-gpusr.sh

echo ""
echo "✓ $CONTAINER_NAME 已启动"
echo ""
echo "=========================================="
echo "  访问视频流"
echo "=========================================="
echo "  1. SSH 端口转发:"
echo "     ssh -N -L $GPUSR_PORT:localhost:$GPUSR_PORT <服务器>"
echo ""
echo "  2. 本地播放器打开（低延迟）:"
echo "     vlc http://localhost:$GPUSR_PORT/ --network-caching=0"
echo "     mpv http://localhost:$GPUSR_PORT/ --cache=no"
echo "     ffplay http://localhost:$GPUSR_PORT/ -fflags nobuffer"
echo ""
echo "  3. 浏览器打开（会有延迟）:"
echo "     http://localhost:$GPUSR_PORT/"
echo ""
echo "  查看日志:"
echo "    podman logs -f $CONTAINER_NAME"
echo "    podman exec $CONTAINER_NAME tail -f /tmp/gpu-screen-recorder.log"
echo "=========================================="
