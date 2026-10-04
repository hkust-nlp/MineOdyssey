#!/usr/bin/env bash
##############################################
# 干净停止 run-server-only-inside.sh 启动的 Minecraft 服务端
#
# 步骤:
#   1. 读取 $PROJECT_ROOT/staging/games/$GAME_ID/.game.info 获取 PIDs 和路径
#   2. 给 Minecraft SIGTERM，等待最多 30s 让它保存世界；卡住再 SIGKILL
#   3. 杀掉同步守护进程（下面手动做 rsync --delete 取代它的增量同步）
#   4. 执行最终 rsync -a --delete: staging → persistent，保证两侧完全一致
#   5. 移除 .owner、.game.info、.sync.pid 等标记
#
# 用法:
#   bash scripts/launch/stop-server.sh <GAME_ID>
#   GAME_ID=<tag> bash scripts/launch/stop-server.sh
#
# 环境变量:
#   MCBOTS_PROJECT_ROOT   项目根（默认：脚本所在仓库根）
##############################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# ── 参数 ──
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n '2,/^####/{ /^#/s/^# \?//p }' "$0"
    exit 0
fi

if [[ -n "${GAME_ID:-}" ]]; then
    GAME_ID_RAW="$GAME_ID"
elif [[ -n "${1:-}" ]]; then
    GAME_ID_RAW="$1"
else
    echo "用法: bash $0 <GAME_ID>   或   GAME_ID=<tag> bash $0" >&2
    exit 1
fi

# 与 run-server-only-inside.sh 相同的规范化逻辑
GAME_ID="$(echo "$GAME_ID_RAW" | tr -c 'A-Za-z0-9_-' '_')"
STAGING_ROOT="$PROJECT_ROOT/staging/games/${GAME_ID}"
GAME_INFO_FILE="$STAGING_ROOT/.game.info"

if [[ ! -f "$GAME_INFO_FILE" ]]; then
    echo "✗ 找不到元信息文件: $GAME_INFO_FILE" >&2
    echo "  (此 GAME_ID 未在本机启动过，或已经被停止过)" >&2
    exit 1
fi

# 从 .game.info 读 key=value（避免 source 的注入风险）
info_val() {
    awk -F= -v k="$1" '$1==k { sub(/^[^=]*=/, ""); print; exit }' "$GAME_INFO_FILE"
}

SERVER_PID="$(info_val SERVER_PID)"
SYNC_DAEMON_PID="$(info_val SYNC_DAEMON_PID)"
INFO_STAGING_ROOT="$(info_val STAGING_ROOT)"
PERSISTENT_DIR="$(info_val PERSISTENT_DIR)"
RUN_LOG_DIR="$(info_val RUN_LOG_DIR)"

# 一致性检查：info 里的 STAGING_ROOT 应与计算出的一致
if [[ -n "$INFO_STAGING_ROOT" ]] && [[ "$INFO_STAGING_ROOT" != "$STAGING_ROOT" ]]; then
    echo "⚠ 警告: info 中的 STAGING_ROOT ($INFO_STAGING_ROOT) 与当前计算值 ($STAGING_ROOT) 不一致"
    echo "  以 info 中的值为准"
    STAGING_ROOT="$INFO_STAGING_ROOT"
fi

echo "=========================================="
echo "  停止服务端 '${GAME_ID}'"
echo "=========================================="
echo "  SERVER_PID: ${SERVER_PID:-<未知>}"
echo "  SYNC_DAEMON_PID: ${SYNC_DAEMON_PID:-<未知>}"
echo "  Staging: ${STAGING_ROOT}"
echo "  持久化: ${PERSISTENT_DIR:-<未设置>}"
echo ""

# 递归 kill 进程树（和 run-server-only-inside.sh 保持同样的行为）
kill_process_tree() {
    local root_pid="$1" signal_name="$2"
    local child_pids
    child_pids="$(ps -o pid= --ppid "$root_pid" 2>/dev/null | tr -d ' ' || true)"
    for child_pid in $child_pids; do
        [[ -n "$child_pid" ]] || continue
        kill_process_tree "$child_pid" "$signal_name"
    done
    kill "-${signal_name}" "$root_pid" 2>/dev/null || true
}

# 1) 停 Minecraft：SIGTERM + 等最多 30s + SIGKILL 兜底
if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "→ 停止 Minecraft 服务端 (PID $SERVER_PID)..."
    kill_process_tree "$SERVER_PID" TERM
    for i in $(seq 1 30); do
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "  ✓ 服务端已退出 (用时 ${i}s)"
            break
        fi
        sleep 1
    done
    if kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "  ⚠ 30s 内未退出，发送 SIGKILL"
        kill_process_tree "$SERVER_PID" KILL
    fi
else
    echo "  (服务端进程不存在，跳过)"
fi

# 2) 停同步守护进程（手动 rsync 在下一步做，不依赖它自己的 trap）
if [[ -n "$SYNC_DAEMON_PID" ]] && kill -0 "$SYNC_DAEMON_PID" 2>/dev/null; then
    echo "→ 停止同步守护进程 (PID $SYNC_DAEMON_PID)..."
    kill -KILL "$SYNC_DAEMON_PID" 2>/dev/null || true
    echo "  ✓ 已停止"
else
    echo "  (同步守护进程不在运行，跳过)"
fi

# 3) 最终同步：staging → persistent，--delete 确保镜像完全一致
if [[ -n "$PERSISTENT_DIR" ]] && [[ -d "$STAGING_ROOT" ]]; then
    echo "→ 执行最终同步 (rsync -a --delete)..."
    mkdir -p "$PERSISTENT_DIR"
    if rsync -a --delete "${STAGING_ROOT}/" "${PERSISTENT_DIR}/"; then
        echo "  ✓ 最终同步完成"
    else
        echo "  ⚠ 最终同步失败 (检查 ${RUN_LOG_DIR:-$STAGING_ROOT}/sync.log)"
    fi
else
    echo "  (未开启持久化，或 staging 不存在，跳过最终同步)"
fi

# 4) 清理标记文件
if [[ -n "$PERSISTENT_DIR" ]] && [[ -f "${PERSISTENT_DIR}/.owner" ]]; then
    rm -f "${PERSISTENT_DIR}/.owner"
    echo "→ 已移除 .owner 标记"
fi
rm -f "$GAME_INFO_FILE"

echo ""
echo "✓ 游戏 '${GAME_ID}' 已干净停止"
if [[ -n "$PERSISTENT_DIR" ]]; then
    echo "  持久化副本已更新: ${PERSISTENT_DIR}"
fi
echo "  Staging 目录保留 (${STAGING_ROOT})，可手动删除或下次复用"
