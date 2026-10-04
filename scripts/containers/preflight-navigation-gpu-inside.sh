#!/usr/bin/env bash
# Verify that one CDI-injected NVIDIA GPU can render OpenGL through Xorg.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISPLAY_NUMBER="${MCBOTS_GPU_PREFLIGHT_DISPLAY:-99}"
DISPLAY_VALUE=":${DISPLAY_NUMBER}"
XORG_LOG="/tmp/mcbots-navigation-gpu-preflight-xorg.log"
XORG_CONFIG="/tmp/mcbots-navigation-gpu-preflight-xorg.conf"
XORG_PID=""

cleanup() {
    if [[ -n "$XORG_PID" ]] && kill -0 "$XORG_PID" 2>/dev/null; then
        kill "$XORG_PID" 2>/dev/null || true
        wait "$XORG_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

[[ -n "${GPU_PCI_BUSID:-}" ]] || {
    echo "GPU_PCI_BUSID is required" >&2
    exit 2
}
command -v nvidia-smi >/dev/null || {
    echo "nvidia-smi is unavailable inside the GPU container" >&2
    exit 2
}
command -v Xorg >/dev/null || {
    echo "Xorg is unavailable inside the GPU container" >&2
    exit 2
}
command -v glxinfo >/dev/null || {
    echo "glxinfo is unavailable inside the GPU container" >&2
    exit 2
}

nvidia-smi -L
sed -e "s|BUSID_PLACEHOLDER|${GPU_PCI_BUSID}|" \
    -e "s|VIRTUAL_WIDTH_PLACEHOLDER|800|" \
    -e "s|VIRTUAL_HEIGHT_PLACEHOLDER|600|" \
    "$SCRIPT_DIR/../runtime/xorg.conf.template" >"$XORG_CONFIG"

Xorg "$DISPLAY_VALUE" -config "$XORG_CONFIG" -nolisten tcp >"$XORG_LOG" 2>&1 &
XORG_PID=$!
for _ in $(seq 1 100); do
    if ! kill -0 "$XORG_PID" 2>/dev/null; then
        tail -n 100 "$XORG_LOG" >&2 || true
        exit 3
    fi
    if DISPLAY="$DISPLAY_VALUE" glxinfo -B >/tmp/mcbots-navigation-gpu-glxinfo.txt 2>&1; then
        break
    fi
    sleep 0.1
done

DISPLAY="$DISPLAY_VALUE" glxinfo -B >/tmp/mcbots-navigation-gpu-glxinfo.txt
renderer="$(sed -n 's/^[[:space:]]*OpenGL renderer string:[[:space:]]*//p' /tmp/mcbots-navigation-gpu-glxinfo.txt | head -n 1)"
[[ -n "$renderer" ]] || {
    cat /tmp/mcbots-navigation-gpu-glxinfo.txt >&2
    echo "OpenGL renderer could not be read" >&2
    exit 4
}
if [[ "${renderer,,}" == *llvmpipe* || "${renderer,,}" == *softpipe* || "${renderer,,}" == *software* ]]; then
    echo "Software renderer is forbidden for GPU navigation eval: $renderer" >&2
    exit 4
fi

echo "GPU OpenGL preflight passed: $renderer"
