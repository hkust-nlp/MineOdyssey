#!/bin/bash
# 自动截图脚本（宿主机模式）- 每5秒截取AI agent的游戏画面（JPEG 75%质量）
# 从 display-registry.json 自动查找 BOT 对应的 DISPLAY
# 用法: ./scripts/analysis/auto-screenshot-inside.sh [BOT_NAME] [截图间隔秒数] [--latest]
#   --latest: 不带时间戳，固定保存到 {BOT_NAME}/screenshot.jpg，方便快速查看当前画面

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# 参数解析
BOT_NAME="${1:-Bot1}"
INTERVAL="${2:-5}"
LATEST_MODE=false
for arg in "$@"; do
    [[ "$arg" == "--latest" ]] && LATEST_MODE=true
done

DISPLAY_REGISTRY_FILE="${DISPLAY_REGISTRY_FILE:-$PROJECT_ROOT/runtime/display-registry.json}"

# ── 从 display-registry.json 查找 DISPLAY ──
if [[ ! -f "$DISPLAY_REGISTRY_FILE" ]]; then
    echo "错误: display 注册表不存在: $DISPLAY_REGISTRY_FILE"
    exit 1
fi

DISPLAY_INDEX=$(python3 -c "
import json, sys
with open('$DISPLAY_REGISTRY_FILE') as f:
    data = json.load(f)
for entry in data.get('allocations', {}).values():
    if ':client:${BOT_NAME}' in entry.get('owner', ''):
        print(entry['display_index'])
        sys.exit(0)
print('', end='')
sys.exit(1)
" 2>/dev/null) || true

if [[ -z "$DISPLAY_INDEX" ]]; then
    echo "错误: 在 display 注册表中找不到 Bot '$BOT_NAME'"
    echo "当前注册表内容:"
    cat "$DISPLAY_REGISTRY_FILE"
    exit 1
fi

DISPLAY_TARGET=":${DISPLAY_INDEX}"

# 截图路径
SCREENSHOT_BASE="${PROJECT_ROOT}/screenshots/${BOT_NAME}"
mkdir -p "$SCREENSHOT_BASE"
if [[ "$LATEST_MODE" == "true" ]]; then
    SCREENSHOT_PATH="${SCREENSHOT_BASE}/screenshot.jpg"
else
    SCREENSHOT_PATH=""  # 每帧动态生成
fi

echo "=========================================="
echo "  自动截图脚本已启动（宿主机模式，JPEG 75%）"
echo "=========================================="
echo "Bot 名称: $BOT_NAME"
if [[ "$LATEST_MODE" == "true" ]]; then
    echo "保存位置: $SCREENSHOT_PATH (latest 模式)"
else
    echo "保存位置: $SCREENSHOT_BASE/<timestamp>.jpg"
fi
echo "截图间隔: ${INTERVAL}秒"
echo "图像格式: JPEG 75% 质量"
echo "截图 DISPLAY: $DISPLAY_TARGET"
echo "按 Ctrl+C 停止"
echo "=========================================="
echo ""

# 主循环
while true; do
    if [[ "$LATEST_MODE" == "true" ]]; then
        OUT_FILE="$SCREENSHOT_PATH"
    else
        OUT_FILE="${SCREENSHOT_BASE}/$(date '+%Y-%m-%d_%H-%M-%S').jpg"
    fi

    if DISPLAY="$DISPLAY_TARGET" xwd -root -silent | convert xwd:- -quality 75 "$OUT_FILE" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') - ✓ Screenshot saved: $OUT_FILE ($(du -h "$OUT_FILE" | cut -f1))"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') - ✗ Screenshot failed (DISPLAY=${DISPLAY_TARGET} unavailable)"
    fi

    # 如果 INTERVAL 为 -1，执行一次后退出
    if [ "$INTERVAL" -eq -1 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') - Single shot mode, exiting"
        exit 0
    fi

    sleep "$INTERVAL"
done
