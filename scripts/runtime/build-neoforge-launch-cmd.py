#!/usr/bin/env python3
"""
解析 NeoForge 和 Minecraft 版本 JSON，生成正确的启动命令
"""
import json
import sys
from pathlib import Path

def main():
    game_dir = Path("/app/game")
    neoforge_version = "21.1.217"
    mc_version = "1.21.1"

    # 读取 NeoForge 版本 JSON
    neoforge_json_path = game_dir / f"versions/neoforge-{neoforge_version}/neoforge-{neoforge_version}.json"
    with open(neoforge_json_path) as f:
        neoforge_data = json.load(f)

    # 读取基础 Minecraft 版本 JSON
    mc_json_path = game_dir / f"versions/{mc_version}/{mc_version}.json"
    with open(mc_json_path) as f:
        mc_data = json.load(f)

    # 变量替换函数
    def substitute_vars(text, var_dict):
        for key, value in var_dict.items():
            text = text.replace(f"${{{key}}}", str(value))
        return text

    # 定义变量映射
    var_map = {
        "library_directory": str(game_dir / "libraries"),
        "classpath_separator": ":",
        "version_name": f"neoforge-{neoforge_version}",
        "natives_directory": str(game_dir / f"versions/neoforge-{neoforge_version}/natives"),
        "game_directory": str(game_dir),
        "assets_root": str(game_dir / "assets"),
        "assets_index_name": mc_data.get("assets", "17"),
        "auth_player_name": "${PLAYER_NAME}",
        "auth_uuid": "${UUID}",
        "auth_access_token": "null",
        "clientid": "null",
        "auth_xuid": "null",
        "user_type": "legacy",
        "version_type": "release",
        "launcher_name": "minecraft-launcher",
        "launcher_version": "2"
    }

    # 1. 构建 JVM 参数
    jvm_args = []

    # 添加用户 JVM 参数（内存等）
    jvm_args.extend(["-Xmx2G", "-Xms512M"])
    jvm_args.append("-XX:+UnlockExperimentalVMOptions")
    jvm_args.append("-XX:+UseG1GC")
    jvm_args.append("-XX:G1NewSizePercent=20")
    jvm_args.append("-XX:G1ReservePercent=20")
    jvm_args.append("-XX:MaxGCPauseMillis=50")
    jvm_args.append("-XX:G1HeapRegionSize=32M")

    # 添加 NeoForge 的 JVM 参数
    for arg in neoforge_data.get("arguments", {}).get("jvm", []):
        if isinstance(arg, str):
            jvm_args.append(substitute_vars(arg, var_map))
        elif isinstance(arg, dict) and "value" in arg:
            # 跳过有条件的参数
            pass

    # 添加 Minecraft 基础 JVM 参数（natives目录等）
    for arg in mc_data.get("arguments", {}).get("jvm", []):
        if isinstance(arg, str):
            jvm_args.append(substitute_vars(arg, var_map))
        elif isinstance(arg, dict) and "value" in arg:
            # 跳过有条件的参数（如 Windows/macOS 专用）
            pass

    # 2. 构建 classpath (来自 Minecraft 基础库)
    classpath = []
    classpath_set = set()  # 用于去重

    # 添加 Minecraft 客户端 jar
    mc_jar = str(game_dir / f"versions/{mc_version}/{mc_version}.jar")
    classpath.append(mc_jar)
    classpath_set.add(mc_jar)

    # 添加所有库（合并并去重）
    for lib_data in mc_data.get("libraries", []) + neoforge_data.get("libraries", []):
        name = lib_data.get("name")
        if not name:
            continue

        # 检查规则（跳过特定平台的库）
        rules = lib_data.get("rules", [])
        if rules:
            # 简化处理：跳过有规则的库（natives等）
            continue

        # 获取库路径
        downloads = lib_data.get("downloads", {})
        artifact = downloads.get("artifact", {})
        path = artifact.get("path")

        if path:
            lib_path = str(game_dir / "libraries" / path)
            # 去重：只添加未见过的路径
            if lib_path not in classpath_set:
                classpath.append(lib_path)
                classpath_set.add(lib_path)

    # 3. mainClass
    main_class = neoforge_data.get("mainClass")

    # 4. 构建游戏参数
    game_args = []

    # 添加 NeoForge 游戏参数
    for arg in neoforge_data.get("arguments", {}).get("game", []):
        if isinstance(arg, str):
            game_args.append(arg)

    # 添加 Minecraft 基础游戏参数
    for arg in mc_data.get("arguments", {}).get("game", []):
        if isinstance(arg, str):
            # 变量替换
            game_args.append(substitute_vars(arg, var_map))
        elif isinstance(arg, dict):
            # 跳过有条件的参数
            pass

    # 5. 生成完整命令
    print("#!/bin/bash")
    print("# NeoForge 客户端启动脚本（自动生成）")
    print()
    print("# 设置变量")
    print("PLAYER_NAME=\"${PLAYER_NAME:-Bot1}\"")
    print("SERVER_HOST=\"${SERVER_HOST:-}\"")
    print("SERVER_PORT=\"${SERVER_PORT:-25565}\"")
    print()
    print("# 计算离线模式 UUID")
    print('MD5=$(echo -n "OfflinePlayer:$PLAYER_NAME" | md5sum | awk \'{print $1}\')')
    print('VARIANT_CHAR=$(printf "%x" $(( 0x${MD5:16:1} & 0x3 | 0x8 )))')
    print('UUID="${MD5:0:8}-${MD5:8:4}-3${MD5:13:3}-${VARIANT_CHAR}${MD5:17:3}-${MD5:20:12}"')
    print()
    print("# 启动命令")
    print("java \\")

    # JVM 参数
    for arg in jvm_args:
        # 处理 -cp 参数
        if arg == "-cp":
            print(f"  {arg} \\")
            # 下一个参数替换为我们构建的 classpath
            continue
        elif arg == "${classpath}":
            print(f"  {':'.join(classpath)} \\")
            continue
        else:
            # 转义特殊字符
            escaped_arg = arg.replace('$', '\\$').replace('"', '\\"')
            print(f"  {escaped_arg} \\")

    # mainClass
    print(f"  {main_class} \\")

    # 游戏参数
    for arg in game_args:
        escaped_arg = arg.replace('$', '\\$').replace('"', '\\"')
        print(f"  {escaped_arg} \\")

    # 服务器连接参数（如果提供）
    print('  ${SERVER_HOST:+--server} \\')
    print('  ${SERVER_HOST:+"$SERVER_HOST"} \\')
    print('  ${SERVER_HOST:+--port} \\')
    print('  ${SERVER_HOST:+"$SERVER_PORT"}')

if __name__ == "__main__":
    main()
