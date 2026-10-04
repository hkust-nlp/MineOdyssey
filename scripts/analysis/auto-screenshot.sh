#!/bin/bash
# 自动截图脚本 - 每5秒截取AI agent的游戏画面（JPEG 75%质量）
# 用法: ./scripts/analysis/auto-screenshot.sh [容器名] [截图间隔秒数]

set -e

# 参数
CONTAINER_NAME="${1:-ai-agent-1}"
INTERVAL="${2:-5}"  # 截图间隔（秒）

detect_display() {
    local container="$1"
    local render_mode
    render_mode=$(podman inspect "$container" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^RENDER_MODE=' | head -n 1 | cut -d= -f2)
    if [ "$render_mode" = "cpu" ]; then
        echo ":1"
    else
        echo ":0"
    fi
}

# 获取容器的启动时间
CONTAINER_CREATED=$(podman inspect "$CONTAINER_NAME" --format '{{.Created}}')
if [ -z "$CONTAINER_CREATED" ]; then
    echo "错误: 容器 $CONTAINER_NAME 不存在"
    exit 1
fi

# 转换时间格式为: YYYY-MM-DD_HH-MM-SS.nanoseconds
TIMESTAMP=$(date -d "$CONTAINER_CREATED" '+%Y-%m-%d_%H-%M-%S.%N' 2>/dev/null || echo "")
if [ -z "$TIMESTAMP" ]; then
    # 如果 date -d 不支持（macOS等），使用 podman 的原始格式解析
    TIMESTAMP=$(echo "$CONTAINER_CREATED" | sed 's/T/_/' | sed 's/\..*//' | tr ':' '-')
    TIMESTAMP="${TIMESTAMP}.$(echo "$CONTAINER_CREATED" | grep -o '\.[0-9]*' | tr -d '.')"
fi

# 截图目录
SCREENSHOT_DIR="./screenshots/${CONTAINER_NAME}_${TIMESTAMP}"

# 确保截图目录存在
mkdir -p "$SCREENSHOT_DIR"

echo "=========================================="
echo "  自动截图脚本已启动（JPEG 75%）"
echo "=========================================="
echo "容器名称: $CONTAINER_NAME"
echo "容器创建时间: $CONTAINER_CREATED"
echo "保存位置: $SCREENSHOT_DIR/screenshot.jpg"
echo "截图间隔: ${INTERVAL}秒"
echo "图像格式: JPEG 75% 质量（~27ms/帧，~130KB）"
DISPLAY_TARGET="$(detect_display "$CONTAINER_NAME")"
echo "截图 DISPLAY: $DISPLAY_TARGET"
echo "按 Ctrl+C 停止"
echo "=========================================="
echo ""

# 主循环  
while true; do  
    # 截图（使用容器名作为临时文件前缀避免冲突）  
    TEMP_FILE="/app/game/temp_${CONTAINER_NAME}"  
    if podman exec "$CONTAINER_NAME" bash -c "export DISPLAY=${DISPLAY_TARGET} && xwd -root -silent | convert xwd:- -quality 75 ${TEMP_FILE}.jpg" 2>/dev/null; then  
        # 复制到目标位置  
        cp ./game/temp_${CONTAINER_NAME}.jpg "$SCREENSHOT_DIR/screenshot.jpg" 2>/dev/null  
        echo "$(date '+%Y-%m-%d %H:%M:%S') - ✓ Screenshot updated ($(du -h "$SCREENSHOT_DIR/screenshot.jpg" | cut -f1))"  
    else  
        echo "$(date '+%Y-%m-%d %H:%M:%S') - ✗ Screenshot failed (DISPLAY=${DISPLAY_TARGET} unavailable or container not running)"  
    fi  

    # 如果 INTERVAL 为 -1，执行一次后退出
    if [ "$INTERVAL" -eq -1 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') - Single shot mode, exiting"
        exit 0
    fi

    # 等待  
    sleep "$INTERVAL"  
done
