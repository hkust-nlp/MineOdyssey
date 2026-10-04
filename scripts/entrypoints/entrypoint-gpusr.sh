#!/bin/bash
# GPU Screen Recorder 版本入口脚本
# 基于 entrypoint.sh，添加 gpu-screen-recorder 自动启动
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

echo "=========================================="
echo "  Minecraft AI Agent (GPU-SR 版) 启动中..."
echo "=========================================="

# 更新动态链接器缓存，确保 NVIDIA 库可被找到
ldconfig 2>/dev/null || true

# 安装 Python 接口模块（供 workspace 脚本直接 `from minecraft_api import ...`）
if [ -x "$SCRIPT_DIR/../runtime/install-minecraft-api-module.sh" ]; then
    "$SCRIPT_DIR/../runtime/install-minecraft-api-module.sh" || true
fi

# 安装辅助 CLI（若存在）
if [ -x "$SCRIPT_DIR/../runtime/mcapi" ]; then
    ln -sf "$SCRIPT_DIR/../runtime/mcapi" /usr/local/bin/mcapi 2>/dev/null || true
fi
if [ -x "$SCRIPT_DIR/../runtime/xdo" ]; then
    ln -sf "$SCRIPT_DIR/../runtime/xdo" /usr/local/bin/xdo 2>/dev/null || true
fi

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/workspace}"
WORKSPACE_DOCS_DIR="${WORKSPACE_ROOT%/}/docs"
ACTION_DOC_SRC="${ACTION_DOC_SRC:-$PROJECT_ROOT/docs/ACTION-SPACE-REFERENCE.md}"
ACTION_DOC_DST="${WORKSPACE_DOCS_DIR}/ACTION-SPACE-REFERENCE.md"
if [ -f "$ACTION_DOC_SRC" ]; then
    mkdir -p "$WORKSPACE_DOCS_DIR"
    cp "$ACTION_DOC_SRC" "$ACTION_DOC_DST"
    echo "  ✓ 已同步动作空间文档到 workspace/docs: $ACTION_DOC_DST"
else
    echo "  ⚠ 未找到动作空间文档，跳过同步: $ACTION_DOC_SRC"
fi
BARITONE_DOC_EN_SRC="${BARITONE_DOC_EN_SRC:-$PROJECT_ROOT/docs/BARITONE-REFERENCE.md}"
BARITONE_DOC_EN_DST="${WORKSPACE_DOCS_DIR}/BARITONE-REFERENCE.md"
mkdir -p "$WORKSPACE_DOCS_DIR"
if [ -f "$BARITONE_DOC_EN_SRC" ]; then
    cp "$BARITONE_DOC_EN_SRC" "$BARITONE_DOC_EN_DST"
    echo "  ✓ 已同步 Baritone 英文文档到 workspace/docs: $BARITONE_DOC_EN_DST"
else
    echo "  ⚠ 未找到 Baritone 英文文档，跳过同步: $BARITONE_DOC_EN_SRC"
fi

AGENTBRIDGE_PORT="${AGENTBRIDGE_PORT:-8080}"
AGENTBRIDGE_JAVA_PROP="-Dagentbridge.port=${AGENTBRIDGE_PORT}"
if [[ " ${JAVA_TOOL_OPTIONS:-} " != *" ${AGENTBRIDGE_JAVA_PROP} "* ]]; then
    export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:-} ${AGENTBRIDGE_JAVA_PROP}"
fi
GAME_DIR="${GAME_DIR:-$PROJECT_ROOT/game}"
export MCBOTS_WORKSPACE_ROOT="${MCBOTS_WORKSPACE_ROOT:-$WORKSPACE_ROOT}"
export REMOTE_BASH_WORKDIR="${REMOTE_BASH_WORKDIR:-$WORKSPACE_ROOT}"

# 1. 获取 GPU BusID 并生成 xorg.conf
echo "[1/7] 配置 Xorg for GPU rendering..."
BUSID="${GPU_PCI_BUSID:-PCI:1:0:0}"
echo "  使用 GPU PCI BusID: ${BUSID}"
XORG_WIDTH="${DISPLAY_RESOLUTION%%x*}"
_REST="${DISPLAY_RESOLUTION#*x}"
XORG_HEIGHT="${_REST%%x*}"
case "$XORG_WIDTH" in ''|*[!0-9]*) XORG_WIDTH=1024 ;; esac
case "$XORG_HEIGHT" in ''|*[!0-9]*) XORG_HEIGHT=768 ;; esac
echo "  Xorg Virtual: ${XORG_WIDTH}x${XORG_HEIGHT}"
sed -e "s|BUSID_PLACEHOLDER|${BUSID}|" \
    -e "s|VIRTUAL_WIDTH_PLACEHOLDER|${XORG_WIDTH}|" \
    -e "s|VIRTUAL_HEIGHT_PLACEHOLDER|${XORG_HEIGHT}|" \
    "$SCRIPT_DIR/../runtime/xorg.conf.template" > /tmp/xorg.conf
echo "  ✓ xorg.conf 已生成"

# 2. 启动 Xorg (无头模式，GPU 渲染)
echo "[2/7] 启动 Xorg :0 (GPU 渲染服务器)..."
Xorg :0 -config /tmp/xorg.conf -nolisten tcp &
XORG_PID=$!
sleep 3
echo "  ✓ Xorg 已启动 (PID: $XORG_PID)"

# 3. 启动 Xvfb (虚拟显示)
echo "[3/7] 启动 Xvfb :1 (虚拟显示)..."
Xvfb :1 -screen 0 1024x768x24 &
XVFB_PID=$!
sleep 2
echo "  ✓ Xvfb 已启动 (PID: $XVFB_PID)"

# 4. 设置环境变量
export DISPLAY=:0
export PATH=$PATH:/opt/VirtualGL/bin

echo "[4/7] 环境变量配置完成"
echo "  DISPLAY=$DISPLAY"
echo "  AgentBridge 端口=$AGENTBRIDGE_PORT"

# 5. 验证 GPU 渲染配置
echo "[5/7] 验证 GPU 渲染配置..."
if command -v nvidia-smi &> /dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n 1)
    echo "  ✓ GPU: $GPU_NAME"

    if DISPLAY=:0 glxinfo 2>/dev/null | grep -q "OpenGL renderer"; then
        RENDERER=$(DISPLAY=:0 glxinfo 2>/dev/null | grep "OpenGL renderer" | cut -d: -f2)
        echo "  ✓ OpenGL Renderer:$RENDERER"
    else
        echo "  ⚠ 无法获取 OpenGL 渲染器信息"
    fi
else
    echo "  ⚠ nvidia-smi 不可用"
fi

# 6. 安装 portablemc 并启动 Minecraft
echo "[6/7] 准备启动 Minecraft..."

# 安装 portablemc (如果未安装)
if ! command -v portablemc &> /dev/null; then
    echo "  安装 portablemc..."
    pip3 install portablemc --break-system-packages -q
    echo "  ✓ portablemc 已安装"
else
    echo "  ✓ portablemc 已存在"
fi

# 配置 Minecraft 窗口全屏
if [ ! -f "$GAME_DIR/options.txt" ]; then
    echo "  创建 options.txt 配置..."
    mkdir -p "$GAME_DIR"
    cat > "$GAME_DIR/options.txt" <<EOF
fullscreen:true
fullscreenResolution:1024x768
EOF
    echo "  ✓ 全屏配置已设置"
else
    # 如果文件存在，确保全屏设置正确
    if ! grep -q "fullscreen:true" "$GAME_DIR/options.txt"; then
        echo "fullscreen:true" >> "$GAME_DIR/options.txt"
    fi
    if ! grep -q "fullscreenResolution:" "$GAME_DIR/options.txt"; then
        echo "fullscreenResolution:1024x768" >> "$GAME_DIR/options.txt"
    fi
fi

# 启动 Remote Bash Server (后台服务)
echo "  启动 Remote Bash Server (HTTP API on port 9090)..."
python3 "$SCRIPT_DIR/../runtime/remote_bash_server.py" > /tmp/remote_bash.log 2>&1 &
REMOTE_BASH_PID=$!
sleep 1
echo "  ✓ Remote Bash Server 已启动 (PID: $REMOTE_BASH_PID)"

# 启动 Minecraft（后台）
echo "  启动 Minecraft 客户端..."
echo "    玩家: ${PLAYER_NAME}"
echo "    服务端: ${SERVER_HOST}:${SERVER_PORT}"

nohup portablemc \
    --main-dir "$GAME_DIR" \
    --work-dir "$GAME_DIR" \
    start neoforge:21.1.217 \
    --jvm-args="-Xms2G -Xmx4G" \
    --username "${PLAYER_NAME}" \
    -s "${SERVER_HOST}" \
    -p "${SERVER_PORT}" \
    > "$GAME_DIR/minecraft.log" 2>&1 &

MINECRAFT_PID=$!
echo "  ✓ Minecraft 已启动 (PID: $MINECRAFT_PID)"

# 等待游戏窗口出现
echo "  等待游戏窗口..."
for i in {1..30}; do
    if DISPLAY=:0 xdotool search --name "Minecraft" > /dev/null 2>&1; then
        echo "  ✓ 游戏窗口已就绪"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "  ⚠ 等待超时，但继续启动"
    fi
    sleep 2
done

# 7. 启动 GPU Screen Recorder
echo "[7/7] 启动 GPU Screen Recorder..."

# 从环境变量获取配置
GPUSR_FPS="${GPUSR_FPS:-60}"
GPUSR_QUALITY="${GPUSR_QUALITY:-very_high}"
GPUSR_CODEC="${GPUSR_CODEC:-h264}"
GPUSR_OUTPUT_PORT="${GPUSR_OUTPUT_PORT:-9999}"

echo "  配置:"
echo "    帧率: ${GPUSR_FPS} FPS"
echo "    质量: ${GPUSR_QUALITY}"
echo "    编码: ${GPUSR_CODEC}"
echo "    HTTP端口: ${GPUSR_OUTPUT_PORT}"

# 创建非 root 用户运行 gpu-screen-recorder（要求）
if ! id -u gpusr > /dev/null 2>&1; then
    useradd -m -s /bin/bash gpusr
    echo "  ✓ 创建用户 gpusr"
fi

echo "  --- DEBUG MODE: 容器将暂停以供手动测试 ---"
echo "  请使用: podman exec ai-agent-TestBot bash"
echo "  测试命令: su gpusr -c 'DISPLAY=:0 glxinfo | grep renderer'"
sleep infinity

# 给予用户访问 X11 的权限
echo "  设置 X11 访问权限..."
xhost +local:gpusr > /dev/null 2>&1 || true
echo "  ✓ X11 权限已设置"

# 创建日志文件（使用 /tmp 目录避免挂载卷权限问题）
LOG_FILE="/tmp/gpu-screen-recorder.log"
touch $LOG_FILE
chown gpusr:gpusr $LOG_FILE
echo "  ✓ 日志文件已创建: $LOG_FILE"

# 启动 gpu-screen-recorder（以非 root 用户）
echo "  启动 gpu-screen-recorder..."
su gpusr -c "DISPLAY=:0 nohup gpu-screen-recorder \
    -w screen \
    -f ${GPUSR_FPS} \
    -q ${GPUSR_QUALITY} \
    -c ${GPUSR_CODEC} \
    -fm vfr \
    -k h264 \
    -o 'http://0.0.0.0:${GPUSR_OUTPUT_PORT}' \
    >> $LOG_FILE 2>&1 &"

sleep 3
GPUSR_PID=$(pgrep -u gpusr gpu-screen-recorder)
if [ -n "$GPUSR_PID" ]; then
    echo "  ✓ GPU Screen Recorder 已启动 (PID: $GPUSR_PID)"
else
    echo "  ⚠ GPU Screen Recorder 可能启动失败，检查日志: $LOG_FILE"
    # 显示错误信息
    if [ -f "$LOG_FILE" ]; then
        echo "  --- 错误日志 ---"
        tail -20 "$LOG_FILE"
        echo "  ---------------"
    fi
fi

echo ""
echo "=========================================="
echo "  所有服务已启动完成"
echo "=========================================="
echo "  Minecraft PID: $MINECRAFT_PID"
echo "  GPU-SR PID: $GPUSR_PID"
echo "  访问视频流: http://<服务器IP>:${GPUSR_OUTPUT_PORT}"
echo ""
echo "  查看日志:"
echo "    Minecraft: tail -f ${GAME_DIR}/minecraft.log"
echo "    GPU-SR: tail -f /tmp/gpu-screen-recorder.log"
echo "=========================================="

# 保持容器运行（监控进程）
while kill -0 $MINECRAFT_PID 2>/dev/null || kill -0 $GPUSR_PID 2>/dev/null; do
    sleep 10
done

echo "主进程已退出，容器即将停止"
