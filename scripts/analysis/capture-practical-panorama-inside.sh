#!/usr/bin/env bash
# Capture a practical multi-view observation mosaic for one bot.
#
# Layout (three rows, total width = MAIN_W):
#   - Top row: up | feet (each MAIN_W/2 x MAIN_H/2)
#   - Middle: main (MAIN_W x MAIN_H, current yaw/pitch)
#   - Bottom row: left | back | right (each ~MAIN_W/3 x MAIN_H/3)
# All six tiles carry whatever HUD state the bot was in when the run started;
# the script does not toggle the HUD itself, only the camera angle.
# Auxiliary look angles:
#   up: same yaw, pitch clamped to [-90, 0] after subtracting --ud-delta
#   feet: same yaw, pitch clamped to [0, 90] after adding --ud-delta
#   left: yaw + --lr-yaw, same pitch
#   right: yaw - --lr-yaw, same pitch
#   back: yaw + 180, same pitch
# Captions:
#   main: lower-left, "main"
#   auxiliary tiles: upper-left, effective relative angle labels
#     up p-N, feet p+N, left y+N, right y-N, back y+180
#
# Intended to run inside the unified runtime container, e.g.:
#   /workspace/mcbots/scripts/analysis/capture-practical-panorama-inside.sh Bot1 \
#     --sleep-sec 0.25 --main-size 960x720

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

BOT_NAME="Bot1"
OUT_FILE="$PROJECT_ROOT/screenshots/tmp/panorama-practical-up-feet-main-left-right.jpg"
SLEEP_SEC="0.25"
MAIN_SIZE="800x600"
LR_YAW="90"
UD_DELTA="55"
QUALITY="92"
LABEL_SIZE="17"

usage() {
    cat <<EOF
Usage: $0 [BOT_NAME] [options]

Options:
  --out PATH             Output image path
                         (default: screenshots/tmp/panorama-practical-up-feet-main-left-right.jpg)
  --sleep-sec SEC        Sleep after each camera move before screenshot (default: 0.25)
  --main-size WxH        Main tile size (default: 800x600)
                         Auxiliary tiles derive from MAIN_SIZE:
                           top (up/feet): MAIN_W/2 x MAIN_H/2
                           bottom (left/back/right): MAIN_W/3 x MAIN_H/3
  --lr-yaw DEG           Left/right relative yaw offset (default: 90)
  --ud-delta DEG         Up/feet pitch delta magnitude (default: 55)
                         up = pitch - DEG, clamped to [-90, 0]
                         feet = pitch + DEG, clamped to [0, 90]
  --quality N            JPEG quality for raw captures (default: 92)
  --label-size N         Caption font size (default: 17)
  -h, --help             Show this help

Examples:
  $0 Bot1 --sleep-sec 0.35 --main-size 1200x900
  $0 Bot1 --main-size 960x720 --lr-yaw 75 --ud-delta 45
EOF
}

if [[ $# -gt 0 && "$1" != --* && "$1" != "-h" ]]; then
    BOT_NAME="$1"
    shift
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out)
            OUT_FILE="$2"
            shift 2
            ;;
        --sleep-sec)
            SLEEP_SEC="$2"
            shift 2
            ;;
        --main-size)
            MAIN_SIZE="$2"
            shift 2
            ;;
        --lr-yaw)
            LR_YAW="$2"
            shift 2
            ;;
        --ud-delta)
            UD_DELTA="$2"
            shift 2
            ;;
        --quality)
            QUALITY="$2"
            shift 2
            ;;
        --label-size)
            LABEL_SIZE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

parse_size() {
    local value="$1"
    local name="$2"
    python3 - "$value" "$name" <<'PY'
import re
import sys

value, name = sys.argv[1], sys.argv[2]
m = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value.strip().lower())
if not m:
    raise SystemExit(f"invalid {name}: {value!r}; expected WxH")
print(m.group(1), m.group(2))
PY
}

read -r MAIN_W MAIN_H < <(parse_size "$MAIN_SIZE" "--main-size")
TOP_TILE_W=$(( MAIN_W / 2 ))
TOP_TILE_H=$(( MAIN_H / 2 ))
BOT_OUTER_W=$(( MAIN_W / 3 ))
BOT_BACK_W=$(( MAIN_W - 2 * BOT_OUTER_W ))
BOT_TILE_H=$(( MAIN_H / 3 ))
if (( TOP_TILE_W < 1 || TOP_TILE_H < 1 || BOT_OUTER_W < 1 || BOT_TILE_H < 1 )); then
    echo "main size too small to derive tile sizes: $MAIN_SIZE" >&2
    exit 2
fi

WORKSPACE_ROOT="${MCBOTS_WORKSPACE_ROOT:-$PROJECT_ROOT/workspaces/main/$BOT_NAME}"
RUNTIME_CONFIG="${MCBOTS_RUNTIME_CONFIG:-$WORKSPACE_ROOT/.mcbots_runtime.json}"
MCAPI="$PROJECT_ROOT/scripts/runtime/mcapi"

if [[ ! -f "$RUNTIME_CONFIG" ]]; then
    echo "Missing runtime config: $RUNTIME_CONFIG" >&2
    exit 1
fi
if [[ ! -x "$MCAPI" ]]; then
    echo "Missing executable mcapi: $MCAPI" >&2
    exit 1
fi
for bin in xwd convert python3; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "Missing required command: $bin" >&2
        exit 1
    fi
done

DISPLAY_TARGET=$(python3 - "$RUNTIME_CONFIG" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    cfg = json.load(f)
display = ((cfg.get("x11") or {}).get("display") or "").strip()
if not display:
    raise SystemExit("runtime config missing x11.display")
print(display)
PY
)

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mcbots-panorama.XXXXXX")"
mkdir -p "$(dirname "$OUT_FILE")"

YAW0=""
PITCH0=""

restore() {
    if [[ -n "$YAW0" && -n "$PITCH0" ]]; then
        (
            cd "$WORKSPACE_ROOT"
            "$MCAPI" look --yaw "$YAW0" --pitch "$PITCH0" --mode absolute >/dev/null 2>&1 || true
        )
    fi
    rm -rf "$TMP_DIR"
}
trap restore EXIT INT TERM

STATE_JSON=$(cd "$WORKSPACE_ROOT" && "$MCAPI" state)
YAW0=$(python3 -c 'import json,sys; print(float(json.load(sys.stdin)["data"]["rotation"]["yaw"]))' <<<"$STATE_JSON")
PITCH0=$(python3 -c 'import json,sys; print(float(json.load(sys.stdin)["data"]["rotation"]["pitch"]))' <<<"$STATE_JSON")

calc_yaw() {
    python3 -c 'import sys; print(float(sys.argv[1]) + float(sys.argv[2]))' "$YAW0" "$1"
}

calc_feet_pitch() {
    python3 -c 'import sys; p=float(sys.argv[1]) + float(sys.argv[2]); print(min(90.0, max(0.0, p)))' "$PITCH0" "$UD_DELTA"
}

calc_up_pitch() {
    python3 -c 'import sys; p=float(sys.argv[1]) - float(sys.argv[2]); print(max(-90.0, min(0.0, p)))' "$PITCH0" "$UD_DELTA"
}

format_signed_int() {
    python3 -c 'import sys; print(f"{float(sys.argv[1]):+.0f}")' "$1"
}

sub_float() {
    python3 -c 'import sys; print(float(sys.argv[1]) - float(sys.argv[2]))' "$1" "$2"
}

look_abs() {
    local yaw="$1"
    local pitch="$2"
    (
        cd "$WORKSPACE_ROOT"
        "$MCAPI" look --yaw "$yaw" --pitch "$pitch" --mode absolute >/dev/null
    )
    sleep "$SLEEP_SEC"
}

capture_raw() {
    local name="$1"
    DISPLAY="$DISPLAY_TARGET" xwd -root -silent \
        | convert xwd:- -quality "$QUALITY" "$TMP_DIR/raw-$name.jpg"
}

BORDER_PX=2

make_main_tile() {
    local src="$1"
    local label="$2"
    local width="$3"
    local height="$4"
    local out="$5"
    local y0=$(( height - LABEL_SIZE - 11 ))
    local ytext=$(( height - 7 ))
    local x1=$(( ${#label} * (LABEL_SIZE / 2 + 2) + 16 ))

    convert "$TMP_DIR/raw-$src.jpg" -resize "${width}x${height}!" \
        -fill "#00000070" -draw "rectangle 0,$y0,$x1,$height" \
        -fill white -pointsize "$LABEL_SIZE" -font DejaVu-Sans-Bold \
        -annotate +7+"$ytext" "$label" \
        -fill none -stroke white -strokewidth "$BORDER_PX" \
        -draw "rectangle 0,0,$(( width - 1 )),$(( height - 1 ))" \
        "$TMP_DIR/tile-$out.jpg"
}

make_aux_tile() {
    local src="$1"
    local label="$2"
    local width="$3"
    local height="$4"
    local out="$5"
    local label_h=$(( LABEL_SIZE + 11 ))
    local ytext=$(( LABEL_SIZE + 3 ))
    local x1=$(( ${#label} * (LABEL_SIZE / 2 + 2) + 16 ))

    convert "$TMP_DIR/raw-$src.jpg" -resize "${width}x${height}!" \
        -fill "#00000070" -draw "rectangle 0,0,$x1,$label_h" \
        -fill white -pointsize "$LABEL_SIZE" -font DejaVu-Sans-Bold \
        -annotate +7+"$ytext" "$label" \
        -fill none -stroke white -strokewidth "$BORDER_PX" \
        -draw "rectangle 0,0,$(( width - 1 )),$(( height - 1 ))" \
        "$TMP_DIR/tile-$out.jpg"
}

# Main is the exact current view.
look_abs "$YAW0" "$PITCH0"
capture_raw main

FEET_PITCH=$(calc_feet_pitch)
UP_PITCH=$(calc_up_pitch)
LEFT_YAW=$(calc_yaw "$LR_YAW")
RIGHT_YAW=$(calc_yaw "-$LR_YAW")
BACK_YAW=$(calc_yaw "180")
FEET_DPITCH=$(sub_float "$FEET_PITCH" "$PITCH0")
UP_DPITCH=$(sub_float "$UP_PITCH" "$PITCH0")
LEFT_DYAW=$(sub_float "$LEFT_YAW" "$YAW0")
RIGHT_DYAW=$(sub_float "$RIGHT_YAW" "$YAW0")

UP_LABEL="up p$(format_signed_int "$UP_DPITCH")"
FEET_LABEL="feet p$(format_signed_int "$FEET_DPITCH")"
LEFT_LABEL="left y$(format_signed_int "$LEFT_DYAW")"
RIGHT_LABEL="right y$(format_signed_int "$RIGHT_DYAW")"
BACK_LABEL="back y+180"

look_abs "$YAW0" "$UP_PITCH"
capture_raw up

look_abs "$YAW0" "$FEET_PITCH"
capture_raw feet

look_abs "$LEFT_YAW" "$PITCH0"
capture_raw left

look_abs "$RIGHT_YAW" "$PITCH0"
capture_raw right

look_abs "$BACK_YAW" "$PITCH0"
capture_raw back

# Restore camera before composing.
look_abs "$YAW0" "$PITCH0"

make_main_tile main main "$MAIN_W" "$MAIN_H" main
make_aux_tile up "$UP_LABEL" "$TOP_TILE_W" "$TOP_TILE_H" up
make_aux_tile feet "$FEET_LABEL" "$TOP_TILE_W" "$TOP_TILE_H" feet
make_aux_tile left "$LEFT_LABEL" "$BOT_OUTER_W" "$BOT_TILE_H" left
make_aux_tile back "$BACK_LABEL" "$BOT_BACK_W" "$BOT_TILE_H" back
make_aux_tile right "$RIGHT_LABEL" "$BOT_OUTER_W" "$BOT_TILE_H" right

convert "$TMP_DIR/tile-up.jpg" "$TMP_DIR/tile-feet.jpg" +append "$TMP_DIR/top-row.jpg"
convert "$TMP_DIR/tile-left.jpg" "$TMP_DIR/tile-back.jpg" "$TMP_DIR/tile-right.jpg" +append "$TMP_DIR/bottom-row.jpg"
convert "$TMP_DIR/top-row.jpg" "$TMP_DIR/tile-main.jpg" "$TMP_DIR/bottom-row.jpg" -append "$OUT_FILE"

echo "saved: $OUT_FILE"
echo "size: ${MAIN_W}x$(( TOP_TILE_H + MAIN_H + BOT_TILE_H ))"
echo "base_yaw=$YAW0 base_pitch=$PITCH0 up_pitch=$UP_PITCH feet_pitch=$FEET_PITCH left_yaw=$LEFT_YAW right_yaw=$RIGHT_YAW back_yaw=$BACK_YAW"
echo "labels: $UP_LABEL | $FEET_LABEL | $LEFT_LABEL | $BACK_LABEL | $RIGHT_LABEL"
