#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTANCE="${GROUND_NAV_INSTANCE:-$ROOT/game}"
SENDER="$ROOT/scripts/eval/set_ground_navigation_route.py"

usage() {
  cat <<'EOF'
用法：
  bash scripts/runtime/ground_navigation.sh <x> <y> <z>       # 显示到坐标的路线
  bash scripts/runtime/ground_navigation.sh <地图> <任务ID>    # 显示正式评测路线
  bash scripts/runtime/ground_navigation.sh off               # 关闭路线

示例：
  bash scripts/runtime/ground_navigation.sh -510 0 -1700
  bash scripts/runtime/ground_navigation.sh shun-lee slr-n01
EOF
}

if [[ $# -eq 1 && "$1" == "off" ]]; then
  exec python3 "$SENDER" --instance "$INSTANCE" --clear --wait-seconds 10
fi

if [[ $# -eq 3 && "$1" =~ ^-?[0-9]+$ && "$2" =~ ^-?[0-9]+$ && "$3" =~ ^-?[0-9]+$ ]]; then
  exec python3 "$SENDER" \
    --instance "$INSTANCE" \
    --point="$1,$2,$3" \
    --wait-seconds 10
fi

if [[ $# -eq 2 ]]; then
  MAP_DIR="$ROOT/eval/navigation/maps/$1"
  exec python3 "$SENDER" \
    --instance "$INSTANCE" \
    --tasks "$ROOT/eval/navigation/tasks.json" \
    --waypoints "$MAP_DIR/waypoints.json" \
    --task-id "$2" \
    --map-id "$1" \
    --wait-seconds 10
fi

usage >&2
exit 2
