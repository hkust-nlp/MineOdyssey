#!/usr/bin/env bash
#
# Switch the server world to a custom map.
#
# Usage:
#   ./switch_map.sh                    # list maps
#   ./switch_map.sh <map_name>         # swap server-data/world to custom_maps/maps/<map_name>/world
#
# After running this you must restart the server/bot for it to take effect.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MAPS_DIR="$SCRIPT_DIR/maps"
SERVER_WORLD_DIR="$PROJECT_ROOT/server-data/world"

if [[ $# -eq 0 ]]; then
    echo "Available maps in $MAPS_DIR:"
    for d in "$MAPS_DIR"/*/; do
        [[ -d "$d/world" ]] || continue
        name=$(basename "$d")
        size=$(du -sh "$d/world" | cut -f1)
        printf "  %-18s %6s\n" "$name" "$size"
    done
    echo
    echo "Usage: $0 <map_name>"
    exit 0
fi

MAP="$1"
SRC="$MAPS_DIR/$MAP/world"

if [[ ! -d "$SRC" ]]; then
    echo "ERROR: map '$MAP' not found at $SRC"
    exit 1
fi

echo "Switching server world:"
echo "  from: $SERVER_WORLD_DIR"
echo "  to:   $SRC"

if [[ -d "$SERVER_WORLD_DIR" ]]; then
    rm -rf "$SERVER_WORLD_DIR"
fi

cp -r "$SRC" "$SERVER_WORLD_DIR"

# wipe playerdata so bot joins as fresh player (full hp)
rm -f "$SERVER_WORLD_DIR"/playerdata/*.dat "$SERVER_WORLD_DIR"/playerdata/*.dat_old 2>/dev/null || true

# force peaceful difficulty by default
SERVER_PROPS="$PROJECT_ROOT/server-data/server.properties"
if [[ -f "$SERVER_PROPS" ]]; then
    sed -i 's/^difficulty=.*/difficulty=peaceful/' "$SERVER_PROPS"
fi

echo "  ✓ world swapped, playerdata cleaned, difficulty=peaceful"
echo
echo "Now restart server and bot:"
echo "  # stop: kill any running server/bot, then"
echo "  cd $PROJECT_ROOT"
echo "  PORT_RANGE_START=50000 PORT_RANGE_END=60000 \\"
echo "    ./scripts/launch/run-server-only-inside.sh --runtime-config runtime/server.json"
echo "  # then start bot"
echo "  PORT_RANGE_START=50000 PORT_RANGE_END=60000 BOT_NAME=bot \\"
echo "    RENDER_MODE=gpu DISPLAY_RESOLUTION=1920x1080x24 GPU_PCI_BUSID=PCI:1:0:0 \\"
echo "    ./scripts/launch/start-single-bot-inside.sh"
