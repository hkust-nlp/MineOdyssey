#!/usr/bin/env bash
set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok_count=0
warn_count=0
fail_count=0

ok() {
  echo -e "${GREEN}OK${NC}   $1"
  ok_count=$((ok_count + 1))
}

warn() {
  echo -e "${YELLOW}WARN${NC} $1"
  warn_count=$((warn_count + 1))
}

fail() {
  echo -e "${RED}FAIL${NC} $1"
  fail_count=$((fail_count + 1))
}

check_file() {
  local p="$1"
  if [ -e "$p" ]; then
    ok "$p"
  else
    fail "$p (missing)"
  fi
}

check_dev() {
  local p="$1"
  if [ -e "$p" ]; then
    ok "$p"
  else
    fail "$p (missing)"
  fi
}

echo "=== GPU mount self-check (inside container) ==="
echo "Date: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo

echo "[1/5] Environment"
if [ "${RENDER_MODE:-}" = "gpu" ]; then
  ok "RENDER_MODE=gpu"
elif [ -z "${RENDER_MODE:-}" ]; then
  warn "RENDER_MODE is not set"
else
  warn "RENDER_MODE=${RENDER_MODE} (not gpu)"
fi

if [ -n "${GPU_PCI_BUSID:-}" ]; then
  ok "GPU_PCI_BUSID=${GPU_PCI_BUSID}"
else
  warn "GPU_PCI_BUSID is not set"
fi

if [ -n "${RENDER_DEVICE:-}" ]; then
  ok "RENDER_DEVICE=${RENDER_DEVICE}"
else
  warn "RENDER_DEVICE is not set"
fi

echo

echo "[2/5] NVIDIA device nodes"
check_dev /dev/nvidiactl
check_dev /dev/nvidia-uvm
check_dev /dev/nvidia-modeset

if ls /dev/nvidia[0-9]* >/dev/null 2>&1; then
  for d in /dev/nvidia[0-9]*; do
    check_dev "$d"
  done
else
  fail "/dev/nvidia[0-9]* (no GPU device node found)"
fi

if [ -d /dev/dri ]; then
  ok "/dev/dri"
  if [ -n "${RENDER_DEVICE:-}" ]; then
    check_dev "/dev/dri/${RENDER_DEVICE}"
  else
    if ls /dev/dri/renderD* >/dev/null 2>&1; then
      for d in /dev/dri/renderD*; do
        check_dev "$d"
      done
    else
      fail "/dev/dri/renderD* (no render node found)"
    fi
  fi
else
  fail "/dev/dri (missing)"
fi

echo

echo "[3/5] NVIDIA userspace libs"
check_file /usr/lib64/libcuda.so.1
check_file /usr/lib64/libnvidia-ml.so.1
check_file /usr/lib64/libGLX_nvidia.so.0
check_file /usr/lib64/libEGL_nvidia.so.0

if ls /usr/lib64/libnvidia-glcore.so.* >/dev/null 2>&1; then
  for f in /usr/lib64/libnvidia-glcore.so.*; do
    check_file "$f"
  done
else
  fail "/usr/lib64/libnvidia-glcore.so.* (missing)"
fi

if ls /usr/lib64/libnvidia-glsi.so.* >/dev/null 2>&1; then
  for f in /usr/lib64/libnvidia-glsi.so.*; do
    check_file "$f"
  done
else
  fail "/usr/lib64/libnvidia-glsi.so.* (missing)"
fi

if ls /usr/lib64/libnvidia-tls.so.* >/dev/null 2>&1; then
  for f in /usr/lib64/libnvidia-tls.so.*; do
    check_file "$f"
  done
else
  fail "/usr/lib64/libnvidia-tls.so.* (missing)"
fi

if ls /usr/lib64/libnvidia-gpucomp.so.* >/dev/null 2>&1; then
  for f in /usr/lib64/libnvidia-gpucomp.so.*; do
    check_file "$f"
  done
else
  fail "/usr/lib64/libnvidia-gpucomp.so.* (missing)"
fi

echo

echo "[4/5] Xorg NVIDIA modules"
check_file /usr/lib/xorg/modules/drivers/nvidia_drv.so
check_file /usr/lib/xorg/modules/extensions/libglx.so
check_file /usr/lib/xorg/modules/extensions/libglxserver_nvidia.so

echo

echo "[5/5] Runtime commands"
if command -v nvidia-smi >/dev/null 2>&1; then
  ok "nvidia-smi is available"
  if nvidia-smi -L >/dev/null 2>&1; then
    ok "nvidia-smi can list GPUs"
  else
    fail "nvidia-smi exists but cannot query GPU"
  fi
else
  fail "nvidia-smi command missing"
fi

if command -v glxinfo >/dev/null 2>&1; then
  ok "glxinfo is available"
else
  warn "glxinfo is not installed"
fi

echo
printf 'Summary: OK=%d  WARN=%d  FAIL=%d\n' "$ok_count" "$warn_count" "$fail_count"

if [ "$fail_count" -gt 0 ]; then
  exit 1
fi

exit 0
