#!/usr/bin/env bash

set -euo pipefail

PROFILE="baseline"
CONTAINER_NAME=""

usage() {
    cat <<'EOF'
用法:
  ./scripts/runtime/check-container-tools.sh
  ./scripts/runtime/check-container-tools.sh --container mc-unified
  ./scripts/runtime/check-container-tools.sh --container mc-unified --profile debug

说明:
  - 默认检查当前 shell 环境（适合在容器内直接执行）。
  - 传入 --container 时，会通过 podman exec 在目标容器内执行检查。
  - 会包含一个 jq 管道场景 smoke test（模拟 agent 常见 JSON 管道处理）。

参数:
  --container <name>     通过 podman exec 检查指定容器
  --profile <name>       baseline | debug（默认: baseline）
  -h, --help             显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --container)
            CONTAINER_NAME="${2:-}"
            if [[ -z "$CONTAINER_NAME" ]]; then
                echo "错误: --container 需要容器名"
                exit 1
            fi
            shift 2
            ;;
        --profile)
            PROFILE="${2:-}"
            if [[ -z "$PROFILE" ]]; then
                echo "错误: --profile 需要取值"
                exit 1
            fi
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "错误: 未知参数: $1"
            usage
            exit 1
            ;;
    esac
done

case "$PROFILE" in
    baseline|debug)
        ;;
    *)
        echo "错误: 不支持的 profile: $PROFILE（仅支持 baseline/debug）"
        exit 1
        ;;
esac

BASELINE_COMMANDS=(
    jq
    curl
    wget
    ps
    pkill
    pstree
    ip
    ping
    netstat
    lsof
    which
    rg
    tree
    column
    file
    tar
    unzip
    zip
    less
    vim
    find
    grep
    sed
    awk
)

DEBUG_COMMANDS=(
    strace
    tcpdump
    dig
    htop
    tmux
)

run_in_target() {
    local cmd="$1"
    if [[ -n "$CONTAINER_NAME" ]]; then
        podman exec "$CONTAINER_NAME" bash -lc "$cmd"
    else
        bash -lc "$cmd"
    fi
}

target_desc() {
    if [[ -n "$CONTAINER_NAME" ]]; then
        echo "容器 $CONTAINER_NAME"
    else
        echo "当前环境"
    fi
}

echo "检查目标: $(target_desc)"
echo "检查 profile: $PROFILE"

if [[ -n "$CONTAINER_NAME" ]]; then
    if ! run_in_target "true" >/dev/null 2>&1; then
        echo "错误: 无法通过 podman exec 访问容器 '$CONTAINER_NAME'。"
        echo "请先确认容器在运行，且当前用户具备 podman 访问权限。"
        exit 1
    fi
fi

missing=()
commands=("${BASELINE_COMMANDS[@]}")
if [[ "$PROFILE" == "debug" ]]; then
    commands+=("${DEBUG_COMMANDS[@]}")
fi

for cmd in "${commands[@]}"; do
    lookup_cmd="${cmd}"
    if [[ "$cmd" == "vim" ]]; then
        lookup_cmd="command -v vim >/dev/null 2>&1 || command -v vi >/dev/null 2>&1"
    else
        lookup_cmd="command -v ${cmd} >/dev/null 2>&1"
    fi

    if run_in_target "$lookup_cmd"; then
        if [[ "$cmd" == "vim" ]]; then
            path="$(run_in_target "command -v vim || command -v vi" | head -n1 | tr -d '\r')"
        else
            path="$(run_in_target "command -v ${cmd}" | tr -d '\r')"
        fi
        printf '[OK] %s -> %s\n' "$cmd" "$path"
    else
        printf '[MISS] %s\n' "$cmd"
        missing+=("$cmd")
    fi
done

echo "执行 jq 管道 smoke test..."
if run_in_target "printf '%s\n' '{\"data\":{\"position\":[1,2,3]}}' | jq -c '.data.position' >/tmp/jq-smoke.out"; then
    result="$(run_in_target "cat /tmp/jq-smoke.out" | tr -d '\r')"
    if [[ "$result" == "[1,2,3]" ]]; then
        echo "[OK] jq pipeline smoke test -> $result"
    else
        echo "[FAIL] jq pipeline smoke test 输出异常: $result"
        exit 1
    fi
else
    echo "[FAIL] jq pipeline smoke test 执行失败"
    exit 1
fi

if [[ "${#missing[@]}" -gt 0 ]]; then
    echo
    echo "缺失命令 (${#missing[@]}): ${missing[*]}"
    exit 1
fi

echo
echo "工具检查通过（profile=$PROFILE）。"
