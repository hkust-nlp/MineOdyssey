#!/bin/bash

##############################################
# Minecraft 服务端单独启动脚本
# 用途：只启动服务端，供人类玩家连接
##############################################

set -e

# 配置参数
IMAGE_SERVER="mc-server"
NETWORK_NAME="mc-network"
SERVER_CONTAINER="mc-server"

# 默认配置
SERVER_PORT="${SERVER_PORT:-25565}"
RCON_PORT="${RCON_PORT:-25575}"
ENABLE_RCON="${ENABLE_RCON:-true}"
RCON_PASSWORD="${RCON_PASSWORD:-minecraft}"
SERVER_MEMORY_MAX="${SERVER_MEMORY_MAX:-8G}"
SERVER_MEMORY_MIN="${SERVER_MEMORY_MIN:-2G}"
MC_VERSION="${MC_VERSION:-1.21.1}"
NEOFORGE_VERSION="${NEOFORGE_VERSION:-21.1.217}"
OP_PLAYER="${OP_PLAYER:-}"

# 显示帮助信息
if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    echo "用法: $0"
    echo ""
    echo "通过环境变量配置服务端参数："
    echo ""
    echo "  SERVER_PORT          服务端端口（默认: 25565）"
    echo "  RCON_PORT            RCON 端口（默认: 25575）"
    echo "  ENABLE_RCON          启用 RCON（默认: true）"
    echo "  RCON_PASSWORD        RCON 密码（默认: minecraft）"
    echo "  SERVER_MEMORY_MAX    最大内存（默认: 8G）"
    echo "  SERVER_MEMORY_MIN    最小内存（默认: 2G）"
    echo "  MC_VERSION           Minecraft 版本（默认: 1.21.1）"
    echo "  NEOFORGE_VERSION     NeoForge 版本（默认: 21.1.217）"
    echo "  OP_PLAYER            管理员玩家名（可选）"
    echo ""
    echo "示例："
    echo "  $0                                        # 使用默认配置"
    echo "  SERVER_PORT=25566 $0                      # 使用 25566 端口"
    echo "  SERVER_MEMORY_MAX=16G $0                  # 服务端最大 16G 内存"
    echo "  MC_VERSION=1.20.1 NEOFORGE_VERSION=20.1.84 $0 # 使用 Minecraft 1.20.1"
    echo "  OP_PLAYER=lockon0927 $0                   # 设置 lockon0927 为管理员"
    echo "  ENABLE_RCON=false $0                      # 禁用 RCON"
    echo "  RCON_PASSWORD=mypassword $0               # 自定义 RCON 密码"
    echo ""
    exit 0
fi

echo "=========================================="
echo "  Minecraft 服务端启动"
echo "=========================================="
echo "配置："
echo "  游戏版本: Minecraft ${MC_VERSION} + NeoForge ${NEOFORGE_VERSION}"
echo "  服务端内存: ${SERVER_MEMORY_MIN} ~ ${SERVER_MEMORY_MAX}"
echo "  游戏端口: ${SERVER_PORT}"
if [ "$ENABLE_RCON" = "true" ]; then
    echo "  RCON 端口: ${RCON_PORT}"
    echo "  RCON 密码: ${RCON_PASSWORD}"
fi
if [ -n "$OP_PLAYER" ]; then
    echo "  管理员: ${OP_PLAYER}"
fi
echo "=========================================="

# 清理旧容器
echo "清理旧容器..."
podman rm -f $SERVER_CONTAINER 2>/dev/null || true

# 创建 Podman 网络（如果不存在）
echo "确保容器网络存在: $NETWORK_NAME"
if ! podman network exists $NETWORK_NAME 2>/dev/null; then
    podman network create $NETWORK_NAME
    echo "  ✓ 网络已创建"
else
    echo "  ✓ 网络已存在"
fi

# 创建服务端数据目录
mkdir -p ./server-data ./server-mods

# 如果 server.properties 不存在，从模板复制
if [ ! -f ./server-data/server.properties ]; then
    if [ -f ./config/server.properties ]; then
        echo "从模板复制 server.properties..."
        cp ./config/server.properties ./server-data/server.properties
    fi
fi

# 启动服务端容器
echo ""
echo "=========================================="
echo "  启动服务端容器"
echo "=========================================="

# 构建端口映射参数
PORT_MAPPINGS="-p ${SERVER_PORT}:25565"
if [ "$ENABLE_RCON" = "true" ]; then
    PORT_MAPPINGS="$PORT_MAPPINGS -p ${RCON_PORT}:25575"
fi

podman run -d \
  --name $SERVER_CONTAINER \
  --network $NETWORK_NAME \
  $PORT_MAPPINGS \
  -v $(pwd)/server-data:/server \
  -v $(pwd)/server-mods:/server/mods \
  -e MEMORY_MIN=$SERVER_MEMORY_MIN \
  -e MEMORY_MAX=$SERVER_MEMORY_MAX \
  -e MC_VERSION=$MC_VERSION \
  -e NEOFORGE_VERSION=$NEOFORGE_VERSION \
  -e OP_PLAYER=$OP_PLAYER \
  -e ENABLE_RCON=$ENABLE_RCON \
  -e RCON_PASSWORD=$RCON_PASSWORD \
  $IMAGE_SERVER

echo ""
echo "✓ 服务端已启动: $SERVER_CONTAINER"
echo "  游戏端口: 0.0.0.0:${SERVER_PORT} → $SERVER_CONTAINER:25565"
if [ "$ENABLE_RCON" = "true" ]; then
    echo "  RCON 端口: 0.0.0.0:${RCON_PORT} → $SERVER_CONTAINER:25575"
fi
echo ""
echo "=========================================="
echo "  使用指南"
echo "=========================================="
echo "查看服务端日志（等待启动完成）:"
echo "  podman logs -f $SERVER_CONTAINER"
echo ""
echo "等待看到这行日志表示启动完成："
echo "  [Server thread/INFO]: Done (xx.xxxs)!"
echo ""
if [ "$ENABLE_RCON" = "true" ]; then
    echo "使用 RCON 管理服务器："
    echo "  # 查看在线玩家"
    echo "  podman exec $SERVER_CONTAINER mcrcon -H localhost -P 25575 -p $RCON_PASSWORD \"list\""
    echo ""
    echo "  # 提升玩家为管理员"
    echo "  podman exec $SERVER_CONTAINER mcrcon -H localhost -P 25575 -p $RCON_PASSWORD \"op 玩家名\""
    echo ""
    echo "  # 发送公告消息"
    echo "  podman exec $SERVER_CONTAINER mcrcon -H localhost -P 25575 -p $RCON_PASSWORD \"say Hello!\""
    echo ""
    echo "  # 从宿主机使用 RCON（需要安装 mcrcon）"
    echo "  mcrcon -H $(hostname -I | awk '{print $1}') -P ${RCON_PORT} -p $RCON_PASSWORD \"list\""
    echo ""
fi
echo "从你的笔记本连接（Minecraft ${MC_VERSION}）:"
echo "  1. 打开 Minecraft 客户端（需要 NeoForge）"
echo "  2. 多人游戏 → 添加服务器"
echo "  3. 服务器地址: $(hostname -I | awk '{print $1}'):${SERVER_PORT}"
echo ""
echo "修改服务端配置:"
echo "  编辑 ./server-data/server.properties（首次启动后自动生成）"
echo "  修改后重启: podman restart $SERVER_CONTAINER"
echo ""
echo "添加服务端 Mod:"
echo "  下载 .jar 文件到 ./server-mods/"
echo "  重启服务端: podman restart $SERVER_CONTAINER"
echo ""
echo "切换游戏版本:"
echo "  方式1（自动下载）:"
echo "    rm -rf ./server-data/run.sh ./server-data/libraries"
echo "    MC_VERSION=1.20.1 NEOFORGE_VERSION=20.1.84 ./scripts/launch/run-server-only.sh"
echo "  方式2（手动安装）:"
echo "    下载 NeoForge installer 并在 server-data 目录中运行安装"
echo ""
echo "停止服务端:"
echo "  podman stop $SERVER_CONTAINER"
echo ""
echo "查看服务端状态:"
echo "  podman ps -a --filter name=$SERVER_CONTAINER"
echo ""
