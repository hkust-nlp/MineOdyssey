#!/bin/bash

set -e

SERVER_PORT="${SERVER_PORT:-25565}"
RCON_PORT="${RCON_PORT:-25575}"

upsert_property() {
    local key="$1"
    local value="$2"
    local file="server.properties"
    if [ ! -f "$file" ]; then
        : > "$file"
    fi
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    else
        echo "${key}=${value}" >> "$file"
    fi
}

echo "=========================================="
echo "  Minecraft 服务端启动中..."
echo "=========================================="

install_neoforge_into() {
    # 在当前目录(由调用方 cd 进去)下载并运行 NeoForge installer
    local installer_url="https://maven.neoforged.net/releases/net/neoforged/neoforge/${NEOFORGE_VERSION}/neoforge-${NEOFORGE_VERSION}-installer.jar"
    local installer="/tmp/neoforge-installer-$$.jar"
    echo "  下载 NeoForge installer..."
    wget --timeout=30 --tries=3 "$installer_url" -O "$installer"
    echo "  运行 installer..."
    java -jar "$installer" --installServer .
    rm -f "$installer"
}

# 检查 NeoForge 是否已安装（通过 run.sh 判断）
if [ ! -f "run.sh" ]; then
    if [ -n "${NEOFORGE_SHARED_DIR:-}" ]; then
        # 共享模式: libraries/versions/run.sh 软链到共享目录
        if [ ! -f "$NEOFORGE_SHARED_DIR/run.sh" ]; then
            echo "[1/3] 共享 NeoForge 未初始化，首次安装到 $NEOFORGE_SHARED_DIR"
            echo "  版本: Minecraft ${MC_VERSION} + NeoForge ${NEOFORGE_VERSION}"
            mkdir -p "$NEOFORGE_SHARED_DIR"
            (
                cd "$NEOFORGE_SHARED_DIR"
                exec 9>".install.lock"
                flock 9
                [ -f "run.sh" ] || install_neoforge_into
            )
            echo "  ✓ 共享 NeoForge 安装完成"
        else
            echo "[1/3] 复用共享 NeoForge: $NEOFORGE_SHARED_DIR"
        fi
        ln -s "$NEOFORGE_SHARED_DIR/libraries" libraries
        ln -s "$NEOFORGE_SHARED_DIR/run.sh"    run.sh
        [ -d "$NEOFORGE_SHARED_DIR/versions" ] && ln -s "$NEOFORGE_SHARED_DIR/versions" versions
        echo "  ✓ libraries / run.sh 已链到共享目录"
    else
        echo "[1/3] NeoForge 未安装，开始安装..."
        echo "  版本: Minecraft ${MC_VERSION} + NeoForge ${NEOFORGE_VERSION}"
        install_neoforge_into
        echo "  ✓ NeoForge 服务端安装完成"
    fi
else
    echo "[1/3] NeoForge 已安装 (run.sh 存在)，跳过安装"
fi

# 创建 EULA 文件
if [ ! -f "eula.txt" ]; then
    echo "[2/3] 创建 EULA 文件"
    echo "eula=true" > eula.txt
else
    echo "[2/3] EULA 文件已存在"
fi

# 创建 mods 目录
if [ ! -d "mods" ]; then
    echo "[3/3] 创建 mods 目录"
    mkdir -p mods
else
    echo "[3/3] mods 目录已存在"
fi

# 配置 RCON（如果启用）
if [ -n "$ENABLE_RCON" ] && [ "$ENABLE_RCON" = "true" ]; then
    RCON_PASSWORD="${RCON_PASSWORD:-minecraft}"

    echo "[4/5] 启用 RCON 功能"
    echo "  端口: ${RCON_PORT}"
    echo "  密码: $RCON_PASSWORD"

    upsert_property "enable-rcon" "true"
    upsert_property "rcon.password" "$RCON_PASSWORD"
    upsert_property "rcon.port" "$RCON_PORT"
    upsert_property "server-port" "$SERVER_PORT"

    echo "  ✓ RCON 已启用"
else
    echo "[4/5] RCON 未启用（设置 ENABLE_RCON=true 以启用）"
    upsert_property "enable-rcon" "false"
    upsert_property "server-port" "$SERVER_PORT"
fi

# 离线模式（允许无正版认证的客户端连入）
ONLINE_MODE_SETTING="${ONLINE_MODE:-false}"
upsert_property "online-mode" "$ONLINE_MODE_SETTING"
upsert_property "enforce-secure-profile" "false"
if [[ "$ONLINE_MODE_SETTING" == "false" ]]; then
    echo "  ✓ 离线模式已启用 (online-mode=false)"
fi

# 可选：难度（peaceful/easy/normal/hard）
if [ -n "${DIFFICULTY:-}" ]; then
    upsert_property "difficulty" "$DIFFICULTY"
    echo "  ✓ difficulty=$DIFFICULTY"
fi

# 可选：最大玩家数
if [ -n "${MAX_PLAYERS:-}" ]; then
    upsert_property "max-players" "$MAX_PLAYERS"
    echo "  ✓ max-players=$MAX_PLAYERS"
fi

# 可选：世界种子（仅在新世界生成时生效）
if [ -d "world" ]; then
    if [ -n "${LEVEL_SEED:-}" ]; then
        echo "  ℹ world/ 已存在，忽略 LEVEL_SEED='$LEVEL_SEED'"
    fi
elif [ -n "${LEVEL_SEED:-}" ]; then
    upsert_property "level-seed" "$LEVEL_SEED"
    echo "  ✓ level-seed=$LEVEL_SEED"
fi

# 配置 OP 玩家（如果指定了）
if [ -n "$OP_PLAYER" ]; then
    # 检查服务器是否为离线模式
    ONLINE_MODE=$(grep -E "^online-mode=" server.properties 2>/dev/null | cut -d'=' -f2 || echo "true")

    if [ "$ONLINE_MODE" = "false" ]; then
        # 离线模式：可以预先设置 OP
        if [ ! -f "ops.json" ] || [ ! -s "ops.json" ] || [ "$(cat ops.json 2>/dev/null)" = "[]" ]; then
            echo "[5/5] 设置管理员: $OP_PLAYER (离线模式)"

            # 计算离线模式 UUID（符合 Minecraft UUID v3 规范）
            MD5=$(echo -n "OfflinePlayer:$OP_PLAYER" | md5sum | awk '{print $1}')
            # 设置版本位（第13个字符 = 3）和变体位（第17个字符）
            VARIANT_CHAR=$(printf "%x" $(( 0x${MD5:16:1} & 0x3 | 0x8 )))
            UUID="${MD5:0:8}-${MD5:8:4}-3${MD5:13:3}-${VARIANT_CHAR}${MD5:17:3}-${MD5:20:12}"

            echo '[
  {
    "uuid": "'"$UUID"'",
    "name": "'"$OP_PLAYER"'",
    "level": 4,
    "bypassesPlayerLimit": false
  }
]' > ops.json
            echo "  UUID: $UUID"
        else
            echo "[5/5] ops.json 已存在且非空，保留现有配置"
        fi
    else
        # 在线模式：无法预先设置 OP
        echo "[5/5] ⚠ 警告: 服务器为在线模式，无法自动设置 OP"
        echo "  请在玩家加入后手动执行: podman exec mc-server rcon-cli op $OP_PLAYER"
        echo "  或使用 podman attach mc-server 进入控制台执行: op $OP_PLAYER"
    fi
fi

echo ""
echo "=========================================="
echo "  启动参数"
echo "=========================================="
echo "  内存: ${MEMORY_MIN} ~ ${MEMORY_MAX}"
echo "  端口: ${SERVER_PORT}"
if [ -n "$OP_PLAYER" ]; then
    echo "  管理员: ${OP_PLAYER}"
fi
echo "=========================================="
echo ""

# 启动服务端
# NeoForge 使用 run.sh 启动，需要传递内存参数到 user_jvm_args.txt
echo "  创建 user_jvm_args.txt 配置内存..."
echo "-Xms${MEMORY_MIN}" > user_jvm_args.txt
echo "-Xmx${MEMORY_MAX}" >> user_jvm_args.txt

echo "  使用 run.sh 启动 NeoForge 服务端"
exec sh run.sh nogui
