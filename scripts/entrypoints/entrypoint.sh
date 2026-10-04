#!/bin/bash
set -e  # 遇到错误立即退出

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
MC_VERSION="${MC_VERSION:-1.21.1}"
NEOFORGE_VERSION="${NEOFORGE_VERSION:-21.1.217}"

run_portablemc() {
    if [[ -n "${MCBOTS_PORTABLEMC_SHIM:-}" ]]; then
        run_project_python "$MCBOTS_PORTABLEMC_SHIM" "$@"
    else
        portablemc "$@"
    fi
}

run_project_python() {
    local configured_python="${MCBOTS_PROJECT_PYTHON:-}"
    local project_environment="${UV_PROJECT_ENVIRONMENT:-}"
    if [[ -n "$configured_python" && -x "$configured_python" ]]; then
        "$configured_python" "$@"
    elif [[ -n "$project_environment" && -x "$project_environment/bin/python" ]]; then
        "$project_environment/bin/python" "$@"
    elif command -v uv >/dev/null 2>&1 && [[ -f "$PROJECT_ROOT/pyproject.toml" ]]; then
        uv run --project "$PROJECT_ROOT" python "$@"
    else
        python3 "$@"
    fi
}

BG_PIDS=()

is_pid_running() {
    local pid="$1"
    [[ -n "$pid" ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

kill_process_tree() {
    local root_pid="$1"
    local signal_name="$2"
    local child_pid
    local child_pids
    child_pids="$(ps -o pid= --ppid "$root_pid" 2>/dev/null | tr -d ' ' || true)"
    for child_pid in $child_pids; do
        [[ -n "$child_pid" ]] || continue
        kill_process_tree "$child_pid" "$signal_name"
    done
    kill "-${signal_name}" "$root_pid" 2>/dev/null || true
}

register_bg_pid() {
    local pid="$1"
    [[ -n "$pid" ]] || return 0
    BG_PIDS+=("$pid")
    return 0
}

cleanup_background_processes() {
    local pid
    for pid in "${BG_PIDS[@]:-}"; do
        [[ -n "$pid" ]] || continue
        if is_pid_running "$pid"; then
            kill_process_tree "$pid" TERM
        fi
    done
    sleep 0.2
    for pid in "${BG_PIDS[@]:-}"; do
        [[ -n "$pid" ]] || continue
        if is_pid_running "$pid"; then
            kill_process_tree "$pid" KILL
        fi
    done
}

ensure_display_available() {
    local display="$1"
    local display_num="${display#:}"
    local lock_file="/tmp/.X${display_num}-lock"
    local socket_file="/tmp/.X11-unix/X${display_num}"

    if [[ -f "$lock_file" ]]; then
        local lock_pid
        lock_pid="$(tr -dc '0-9' < "$lock_file" | head -c 16 || true)"
        if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
            echo "  ✗ 显示号已被占用: ${display} (pid=${lock_pid})"
            return 1
        fi
        rm -f "$lock_file" || true
    fi

    if [[ -S "$socket_file" ]]; then
        rm -f "$socket_file" || true
    fi
    return 0
}

start_xvfb_checked() {
    local display="$1"
    local resolution="$2"
    local max_retries="${XVFB_RETRY_COUNT:-50}"
    local display_num="${display#:}"
    local attempt=0

    while (( attempt <= max_retries )); do
        local cur_display=":${display_num}"
        local log_file="/tmp/mcbots-xvfb-${display_num}.log"
        local socket_file="/tmp/.X11-unix/X${display_num}"

        if ! ensure_display_available "$cur_display"; then
            echo "  ⚠ display :${display_num} 已被占用，尝试下一个..."
            (( display_num++ )) || true
            (( attempt++ )) || true
            continue
        fi

        Xvfb "$cur_display" -screen 0 "$resolution" -nolisten tcp >"$log_file" 2>&1 &
        XVFB_PID="$!"
        register_bg_pid "$XVFB_PID"

        local started=false
        for _ in $(seq 1 20); do
            if ! is_pid_running "$XVFB_PID"; then
                break
            fi
            if [[ -S "$socket_file" ]]; then
                started=true
                break
            fi
            sleep 0.1
        done

        if $started; then
            echo "  ✓ Xvfb 已启动 (PID: $XVFB_PID, display=${cur_display})"
            XVFB_DISPLAY="$cur_display"
            DISPLAY_INDEX="$display_num"
            return 0
        fi

        if grep -q "server already running\|Cannot establish any listening sockets" "$log_file" 2>/dev/null; then
            echo "  ⚠ display :${display_num} 被其他进程/容器占用，尝试下一个..."
            (( display_num++ )) || true
            (( attempt++ )) || true
        else
            echo "  ✗ Xvfb 启动失败: display=${cur_display}"
            tail -n 60 "$log_file" || true
            return 1
        fi
    done

    echo "  ✗ Xvfb 连续 ${max_retries} 次重试均失败，无可用 display"
    return 1
}

trap cleanup_background_processes EXIT INT TERM

echo "=========================================="
echo "  Minecraft AI Agent 容器启动中..."
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

RENDER_MODE="${RENDER_MODE:-gpu}"
DISPLAY_RESOLUTION="${DISPLAY_RESOLUTION:-800x600x24}"
DISPLAY_INDEX="${DISPLAY_INDEX:-1}"
XVFB_DISPLAY=":${DISPLAY_INDEX}"
GAME_DIR="${GAME_DIR:-$PROJECT_ROOT/game}"
GAME_WORK_DIR="${GAME_WORK_DIR:-$GAME_DIR}"
GAME_MODS_TEMPLATE_DIR="${GAME_MODS_TEMPLATE_DIR:-$PROJECT_ROOT/game/mods}"
COPY_GAME_MODS_TEMPLATE="${COPY_GAME_MODS_TEMPLATE:-true}"
ENABLE_REMOTE_BASH="${ENABLE_REMOTE_BASH:-true}"
REMOTE_BASH_PORT="${REMOTE_BASH_PORT:-9090}"
AGENTBRIDGE_PORT="${AGENTBRIDGE_PORT:-8080}"
export MCBOTS_WORKSPACE_ROOT="${MCBOTS_WORKSPACE_ROOT:-$WORKSPACE_ROOT}"
export REMOTE_BASH_WORKDIR="${REMOTE_BASH_WORKDIR:-$WORKSPACE_ROOT}"
if [[ "${RENDER_MODE,,}" == "cpu" ]]; then
    RENDER_MODE="cpu"
    RENDER_DESC="CPU 软渲染"
else
    RENDER_MODE="gpu"
    RENDER_DESC="GPU 渲染"
fi

echo "[1/6] 初始化渲染环境 (${RENDER_DESC})..."
if [ "$RENDER_MODE" = "gpu" ]; then
    BUSID="${GPU_PCI_BUSID:-PCI:1:0:0}"
    echo "  使用 GPU PCI BusID: ${BUSID}"

    # 从 DISPLAY_RESOLUTION (e.g. "1024x768x24") 解析宽高，填进 Xorg Virtual
    XORG_WIDTH="${DISPLAY_RESOLUTION%%x*}"
    _REST="${DISPLAY_RESOLUTION#*x}"
    XORG_HEIGHT="${_REST%%x*}"
    # 兜底：解析失败时回到 1024x768
    case "$XORG_WIDTH" in ''|*[!0-9]*) XORG_WIDTH=1024 ;; esac
    case "$XORG_HEIGHT" in ''|*[!0-9]*) XORG_HEIGHT=768 ;; esac
    echo "  Xorg Virtual: ${XORG_WIDTH}x${XORG_HEIGHT}"

    sed -e "s|BUSID_PLACEHOLDER|${BUSID}|" \
        -e "s|VIRTUAL_WIDTH_PLACEHOLDER|${XORG_WIDTH}|" \
        -e "s|VIRTUAL_HEIGHT_PLACEHOLDER|${XORG_HEIGHT}|" \
        "$SCRIPT_DIR/../runtime/xorg.conf.template" > /tmp/xorg.conf
    echo "  ✓ xorg.conf 已生成"

    echo "  启动 Xorg :0 (GPU 渲染服务器)..."
    Xorg :0 -config /tmp/xorg.conf -nolisten tcp >/tmp/mcbots-xorg-0.log 2>&1 &
    XORG_PID=$!
    register_bg_pid "$XORG_PID"
    sleep 3
    if ! is_pid_running "$XORG_PID"; then
        echo "  ✗ Xorg 启动失败"
        tail -n 80 /tmp/mcbots-xorg-0.log || true
        exit 1
    fi
    echo "  ✓ Xorg 已启动 (PID: $XORG_PID, display=:0)"

    echo "  启动 Xvfb ${XVFB_DISPLAY} (备用虚拟显示, ${DISPLAY_RESOLUTION})..."
    start_xvfb_checked "${XVFB_DISPLAY}" "$DISPLAY_RESOLUTION"

    export DISPLAY=:0
else
    echo "  CPU 模式: 跳过 Xorg/NVIDIA 初始化"
    echo "  启动 Xvfb ${XVFB_DISPLAY} (CPU 软渲染显示, ${DISPLAY_RESOLUTION})..."
    start_xvfb_checked "${XVFB_DISPLAY}" "$DISPLAY_RESOLUTION"

    export DISPLAY="${XVFB_DISPLAY}"
fi

export PATH=$PATH:/opt/VirtualGL/bin

echo "[2/6] 环境变量配置完成"
echo "  DISPLAY=$DISPLAY"
echo "  渲染模式=$RENDER_MODE"
echo "  AgentBridge 端口=$AGENTBRIDGE_PORT"

AGENTBRIDGE_JAVA_PROP="-Dagentbridge.port=${AGENTBRIDGE_PORT}"
if [[ " ${JAVA_TOOL_OPTIONS:-} " != *" ${AGENTBRIDGE_JAVA_PROP} "* ]]; then
    export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:-} ${AGENTBRIDGE_JAVA_PROP}"
fi

echo "[3/6] 验证渲染配置..."
if [ "$RENDER_MODE" = "gpu" ]; then
    if command -v nvidia-smi &> /dev/null; then
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n 1)
        echo "  ✓ GPU: $GPU_NAME"

        if DISPLAY=:0 glxinfo 2>/dev/null | grep -q "OpenGL renderer"; then
            RENDERER=$(DISPLAY=:0 glxinfo 2>/dev/null | grep "OpenGL renderer" | cut -d: -f2)
            echo "  ✓ Xorg GPU 渲染器:$RENDERER"
        else
            echo "  ⚠ 警告: Xorg :0 GPU 渲染不可用"
        fi
    else
        echo "  ⚠ 警告: nvidia-smi 不可用"
    fi
else
    if DISPLAY="$DISPLAY" glxinfo 2>/dev/null | grep -q "OpenGL renderer"; then
        RENDERER=$(DISPLAY="$DISPLAY" glxinfo 2>/dev/null | grep "OpenGL renderer" | cut -d: -f2)
        echo "  ✓ CPU 软件渲染器:$RENDERER"
    else
        echo "  ⚠ 警告: 无法获取 CPU 渲染器信息"
    fi
fi

# 6. 配置运行目录（Mods + options）
echo "[4/6] 配置游戏选项..."
RUNTIME_GAME_DIR="$GAME_WORK_DIR"
OPTIONS_FILE="$RUNTIME_GAME_DIR/options.txt"
mkdir -p "$RUNTIME_GAME_DIR"

set_option() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}:" "$OPTIONS_FILE" 2>/dev/null; then
        sed -i "s/^${key}:.*/${key}:${value}/" "$OPTIONS_FILE"
    else
        echo "${key}:${value}" >> "$OPTIONS_FILE"
    fi
}

# Only write when the key is absent, so in-game adjustments persist across launches.
set_option_default() {
    local key="$1"
    local value="$2"
    if ! grep -q "^${key}:" "$OPTIONS_FILE" 2>/dev/null; then
        echo "${key}:${value}" >> "$OPTIONS_FILE"
    fi
}

if [[ "${COPY_GAME_MODS_TEMPLATE,,}" != "true" ]]; then
    echo "  ✓ 保留运行目录的严格 Mods 清单（未复制共享模板）"
elif [ -d "$GAME_MODS_TEMPLATE_DIR" ]; then
    mkdir -p "$RUNTIME_GAME_DIR/mods"
    copied_mods=0
    for mod_jar in "$GAME_MODS_TEMPLATE_DIR"/*.jar; do
        [ -e "$mod_jar" ] || continue
        mod_name="$(basename "$mod_jar")"
        if [ ! -f "$RUNTIME_GAME_DIR/mods/$mod_name" ]; then
            cp "$mod_jar" "$RUNTIME_GAME_DIR/mods/$mod_name"
            copied_mods=$((copied_mods + 1))
        fi
    done
    echo "  ✓ 已检查基础 Mods: template=$GAME_MODS_TEMPLATE_DIR copied=$copied_mods target=$RUNTIME_GAME_DIR/mods"
else
    echo "  ⚠ 未找到基础 Mods 模板目录: $GAME_MODS_TEMPLATE_DIR"
fi

if [ ! -f "$OPTIONS_FILE" ]; then
    touch "$OPTIONS_FILE"
fi

# MC stores FOV as normalized [-1,1] -> [30,110]°: degrees = 70 + 40*value. 0.75 -> 100°.
# Override via MC_FOV (e.g. MC_FOV=0.0 for 70°).
set_option_default "fov" "${MC_FOV:-0.75}"

set_option "fullscreen" "true"
# Skip first-launch accessibility wizard and multiplayer warning so quick-play can proceed unattended.
set_option "onboardAccessibility" "false"
set_option "skipMultiplayerWarning" "true"
echo "  ✓ 已写入 fullscreen/onboardAccessibility/skipMultiplayerWarning 到 $OPTIONS_FILE"

# 7. 启动 Remote Bash Server (后台服务，可选)
if [[ "${ENABLE_REMOTE_BASH,,}" == "true" ]]; then
    echo "[5/6] 启动 Remote Bash Server (HTTP API on port ${REMOTE_BASH_PORT})..."
    run_project_python "$SCRIPT_DIR/../runtime/remote_bash_server.py" "$REMOTE_BASH_PORT" &
    REMOTE_BASH_PID=$!
    register_bg_pid "$REMOTE_BASH_PID"
    sleep 1
    if ! is_pid_running "$REMOTE_BASH_PID"; then
        echo "  ✗ Remote Bash Server 启动失败"
        exit 1
    fi
    echo "  ✓ Remote Bash Server 已启动 (PID: $REMOTE_BASH_PID)"
else
    echo "[5/6] 跳过 Remote Bash Server (ENABLE_REMOTE_BASH=${ENABLE_REMOTE_BASH})"
fi

# 8. 启动 Minecraft（使用 Rust 版 PortableMC）
echo "[6/6] 启动 Minecraft ${MC_VERSION} (NeoForge ${NEOFORGE_VERSION})..."
echo "=========================================="

# 获取配置参数（通过环境变量）
PLAYER_NAME="${PLAYER_NAME:-OfflinePlayer}"
SERVER_HOST="${SERVER_HOST:-}"
SERVER_PORT="${SERVER_PORT:-25565}"
MC_EXIT_CODE=0

if [ -n "$SERVER_HOST" ]; then
    echo "  模式: 多人游戏"
    echo "  玩家: $PLAYER_NAME"
    echo "  服务器: $SERVER_HOST:$SERVER_PORT"
    echo "  渲染: $RENDER_DESC (DISPLAY=$DISPLAY)"
    echo "=========================================="

    # 使用 Rust 版 PortableMC 启动 NeoForge
    cd "$GAME_DIR"
    set +e
    run_portablemc --main-dir "$GAME_DIR" --work-dir "$GAME_WORK_DIR" \
        start "neoforge:${NEOFORGE_VERSION}" \
        --username "$PLAYER_NAME" \
        -s "$SERVER_HOST" \
        -p "$SERVER_PORT"
    MC_EXIT_CODE=$?
    set -e
else
    echo "  模式: 单人游戏"
    echo "  玩家: $PLAYER_NAME"
    echo "  渲染: $RENDER_DESC (DISPLAY=$DISPLAY)"
    echo "=========================================="

    cd "$GAME_DIR"
    set +e
    run_portablemc --main-dir "$GAME_DIR" --work-dir "$GAME_WORK_DIR" \
        start "neoforge:${NEOFORGE_VERSION}" \
        --username "$PLAYER_NAME"
    MC_EXIT_CODE=$?
    set -e
fi

echo "=========================================="
echo "  游戏进程已退出"
echo "  退出码: ${MC_EXIT_CODE}"
echo "=========================================="
exit "$MC_EXIT_CODE"
