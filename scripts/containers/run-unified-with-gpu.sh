#!/usr/bin/env bash
#
# Wrapper around run-unified-container-once.sh that auto-detects GPU
# devices/libraries on the host and assembles GPU_ARGS_STRING.
#
# 用法:
#   ./scripts/containers/run-unified-with-gpu.sh                  # 默认全部 GPU
#   GPU_IDS=0,1,2,3 ./scripts/containers/run-unified-with-gpu.sh  # 指定 GPU
#   GPU_IDS=all    ./scripts/containers/run-unified-with-gpu.sh   # 显式全部
#   GPU_IDS=none   ./scripts/containers/run-unified-with-gpu.sh   # 纯 CPU
#
# 透传给 run-unified-container-once.sh 的环境变量:
#   IMAGE, CONTAINER_NAME, HOST_ROOT, CONTAINER_ROOT, NETWORK_NAME,
#   SHARED_PORT_RANGE, CONTAINER_CPUS, CONTAINER_MEMORY, CONTAINER_MEMORY_SWAP,
#   CONTAINER_PIDS_LIMIT, CONTAINER_SHM_SIZE
#
# 额外 podman run 参数可以放在 -- 之后:
#   ./scripts/containers/run-unified-with-gpu.sh -- -e MY_VAR=foo

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GPU_IDS="${GPU_IDS:-all}"

usage() {
    cat <<'EOF'
用法:
  ./scripts/containers/run-unified-with-gpu.sh
  GPU_IDS=0,1,2,3 ./scripts/containers/run-unified-with-gpu.sh
  GPU_IDS=none ./scripts/containers/run-unified-with-gpu.sh -- -e EXTRA=1

环境变量:
  GPU_IDS    要挂载的 GPU 列表（逗号分隔），或 "all" / "none"
             默认: all（自动挂载 nvidia-smi 列出的全部 GPU）

其他变量透传给 run-unified-container-once.sh，详见其 -h 帮助。
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

EXTRA_ARGS=()
if [[ "${1:-}" == "--" ]]; then
    shift
    EXTRA_ARGS=("$@")
elif [[ $# -gt 0 ]]; then
    echo "错误: 未知参数: $*" >&2
    echo "使用 -h 查看帮助。" >&2
    exit 1
fi

# ----- CPU-only 直接退出去 -----
if [[ "${GPU_IDS,,}" == "none" ]]; then
    echo "GPU_IDS=none，使用 CPU-only 模式启动统一容器。"
    GPU_ARGS_STRING="" exec "$SCRIPT_DIR/run-unified-container-once.sh" \
        ${EXTRA_ARGS[@]:+-- "${EXTRA_ARGS[@]}"}
fi

# ----- 解析 GPU 列表 -----
if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "错误: 未找到 nvidia-smi，无法挂载 GPU。" >&2
    echo "提示: 设置 GPU_IDS=none 可使用 CPU-only 模式。" >&2
    exit 1
fi

if [[ "${GPU_IDS,,}" == "all" ]]; then
    mapfile -t GPU_LIST < <(nvidia-smi --query-gpu=index --format=csv,noheader)
else
    IFS=',' read -ra GPU_LIST <<< "$GPU_IDS"
fi

if [[ ${#GPU_LIST[@]} -eq 0 ]]; then
    echo "错误: GPU 列表为空。" >&2
    exit 1
fi

# ----- 检测驱动版本（所有 GPU 共用） -----
DRV_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1)
if [[ -z "$DRV_VER" ]]; then
    echo "错误: 无法检测 NVIDIA 驱动版本。" >&2
    exit 1
fi
echo "NVIDIA 驱动版本: $DRV_VER"

# ----- 收集 GPU 设备 -----
GPU_ARGS=()

# 全局 NVIDIA 控制设备（所有 GPU 共用，挂一次）
GLOBAL_DEVICES=(
    /dev/nvidiactl
    /dev/nvidia-uvm
    /dev/nvidia-uvm-tools
    /dev/nvidia-modeset
)
for dev in "${GLOBAL_DEVICES[@]}"; do
    if [[ -e "$dev" ]]; then
        GPU_ARGS+=(--device "$dev")
    fi
done

# 每个 GPU 的 /dev/nvidia${ID} 和 /dev/dri/renderD${X}
for gpu_id in "${GPU_LIST[@]}"; do
    gpu_id="${gpu_id// /}"  # 去空格
    nvidia_dev="/dev/nvidia${gpu_id}"
    if [[ ! -e "$nvidia_dev" ]]; then
        echo "错误: 设备不存在 $nvidia_dev (GPU $gpu_id)" >&2
        exit 1
    fi
    GPU_ARGS+=(--device "$nvidia_dev")

    # 查 PCI BusID 和 DRI render 设备
    pci_busid=$(nvidia-smi -i "$gpu_id" --query-gpu=pci.bus_id --format=csv,noheader)
    pci_addr=$(echo "$pci_busid" | tr '[:upper:]' '[:lower:]' | sed 's/^[0-9a-f]*:/0000:/')
    render_dev=$(ls -l /dev/dri/by-path/ 2>/dev/null \
        | grep "pci-${pci_addr}-render" \
        | awk '{print $NF}' \
        | sed 's|^\.\./||' || true)
    if [[ -z "$render_dev" ]]; then
        echo "警告: GPU $gpu_id 没有找到 DRI render 设备 (PCI $pci_addr)" >&2
    else
        GPU_ARGS+=(--device "/dev/dri/${render_dev}")
        echo "  GPU $gpu_id -> $nvidia_dev, /dev/dri/${render_dev}"
    fi
done

# 整个 /dev/dri 也挂一份（兼容部分应用）
# 注意：如果想严格隔离，删掉这行
# GPU_ARGS+=(-v /dev/dri:/dev/dri)

# ----- 挂载 NVIDIA 用户态库（按驱动版本号） -----
NVIDIA_LIBS_VERSIONED=(
    libnvidia-glcore
    libnvidia-glsi
    libnvidia-tls
    libnvidia-gpucomp
    libnvidia-encode
    libnvidia-opticalflow
    libnvidia-fbc
)
NVIDIA_LIBS_SYMLINKED=(
    "libcuda:libcuda.so.1"
    "libnvidia-ml:libnvidia-ml.so.1"
    "libGLX_nvidia:libGLX_nvidia.so.0"
    "libEGL_nvidia:libEGL_nvidia.so.0"
    "libnvidia-encode:libnvidia-encode.so.1"
)

for lib in "${NVIDIA_LIBS_VERSIONED[@]}"; do
    src="/usr/lib64/${lib}.so.${DRV_VER}"
    if [[ -e "$src" ]]; then
        GPU_ARGS+=(-v "${src}:${src}:ro")
    fi
done

for entry in "${NVIDIA_LIBS_SYMLINKED[@]}"; do
    libname="${entry%:*}"
    target="${entry#*:}"
    src="/usr/lib64/${libname}.so.${DRV_VER}"
    if [[ -e "$src" ]]; then
        GPU_ARGS+=(-v "${src}:/usr/lib64/${target}:ro")
    fi
done

# nvidia-smi 二进制
if [[ -e /usr/bin/nvidia-smi ]]; then
    GPU_ARGS+=(-v /usr/bin/nvidia-smi:/usr/bin/nvidia-smi:ro)
fi

# Xorg NVIDIA 模块（可选，缺失不致命）
XORG_MODULES=(
    "/usr/lib64/xorg/modules/drivers/nvidia_drv.so:/usr/lib/xorg/modules/drivers/nvidia_drv.so"
    "/usr/lib64/xorg/modules/extensions/libglx.so:/usr/lib/xorg/modules/extensions/libglx.so"
    "/usr/lib64/xorg/modules/extensions/libglxserver_nvidia.so:/usr/lib/xorg/modules/extensions/libglxserver_nvidia.so"
)
for entry in "${XORG_MODULES[@]}"; do
    src="${entry%:*}"
    dst="${entry#*:}"
    if [[ -e "$src" ]]; then
        GPU_ARGS+=(-v "${src}:${dst}:ro")
    fi
done

# ----- 序列化为字符串透传 -----
GPU_ARGS_STRING="${GPU_ARGS[*]}"
echo
echo "组装的 GPU_ARGS (${#GPU_ARGS[@]} 项):"
printf '  %s\n' "${GPU_ARGS[@]}"
echo

export GPU_ARGS_STRING
exec "$SCRIPT_DIR/run-unified-container-once.sh" \
    ${EXTRA_ARGS[@]:+-- "${EXTRA_ARGS[@]}"}
