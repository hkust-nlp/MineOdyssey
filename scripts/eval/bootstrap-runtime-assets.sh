#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Auto-detect IPv4 connectivity; if unavailable, tell Java to prefer IPv6.
if ! curl -4 -s --connect-timeout 3 https://maven.neoforged.net -o /dev/null 2>/dev/null; then
    echo "[net] IPv4 unreachable, enabling Java IPv6 mode"
    _JAVA_IPV6_OPTS="-Djava.net.preferIPv4Stack=false -Djava.net.preferIPv6Addresses=true"
    export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:+${JAVA_TOOL_OPTIONS} }${_JAVA_IPV6_OPTS}"
fi

MC_VERSION="${MC_VERSION:-1.21.1}"
NEOFORGE_VERSION="${NEOFORGE_VERSION:-21.1.217}"
GAME_DIR="${GAME_DIR:-$PROJECT_ROOT/game}"
SERVER_DATA_DIR="${SERVER_DATA_DIR:-$PROJECT_ROOT/server-data}"
EVAL_CLIENT_TEMPLATE_DST="${EVAL_CLIENT_TEMPLATE_DST:-$PROJECT_ROOT/eval/templates/client_game}"
EVAL_SERVER_TEMPLATE_DST="${EVAL_SERVER_TEMPLATE_DST:-$PROJECT_ROOT/eval/templates/server-data-neoforge-1.21.1}"
CLIENT_WARMUP_TIMEOUT_SEC="${CLIENT_WARMUP_TIMEOUT_SEC:-240}"
BOOTSTRAP_PLAYER="${BOOTSTRAP_PLAYER:-bootstrap}"

SKIP_CLIENT_WARMUP="false"
SKIP_SERVER_INSTALL="false"
SKIP_EVAL_TEMPLATES="false"
SKIP_VDISPLAY_PREREQS="false"
FORCE_EVAL_TEMPLATES="true"
FORCE_CLIENT_WARMUP="false"
CLIENT_WARMUP_RETRIES="${CLIENT_WARMUP_RETRIES:-5}"
PRUNE_SERVER_VOLATILE="true"
DRY_RUN="false"
AUTO_INSTALL_PREREQS="${AUTO_INSTALL_PREREQS:-false}"

usage() {
    cat <<'EOF'
Pre-download / pre-install runtime assets for main chain and eval templates.

Default behavior:
  1) Ensure virtual-display/runtime capture tools are present (Xvfb + xdotool + xwd + convert + ffmpeg).
  2) Warm client runtime in GAME_DIR (portablemc neoforge bootstrap run).
  3) Install NeoForge server runtime in SERVER_DATA_DIR if missing.
  4) Generate eval templates (client + server-data) from GAME_DIR/SERVER_DATA_DIR.

Usage:
  scripts/eval/bootstrap-runtime-assets.sh [options]

Options:
  --project-root DIR                Project root (default: auto detect)
  --game-dir DIR                    Main client game dir source (default: <project>/game)
  --server-data-dir DIR             Main server-data dir source (default: <project>/server-data)
  --eval-client-template-dst DIR    Eval client template dst (default: <project>/eval/templates/client_game)
  --eval-server-template-dst DIR    Eval server-data template dst (default: <project>/eval/templates/server-data-neoforge-1.21.1)
  --mc-version VER                  Minecraft version label (default: 1.21.1)
  --neoforge-version VER            NeoForge version (default: 21.1.217)
  --client-warmup-timeout-sec N     Timeout for portablemc warmup launch (default: 240)
  --bootstrap-player NAME           Username for warmup launch (default: bootstrap)
  --skip-virtual-display-prereqs    Skip Xvfb/xdotool/xwd/convert/ffmpeg dependency check/install
  --skip-client-warmup              Skip portablemc client warmup
  --skip-server-install             Skip server NeoForge install check
  --skip-eval-templates             Skip eval template generation
  --no-force-eval-templates         Do not overwrite existing eval template dirs
  --no-prune-server-volatile        Keep world/logs/crash-reports/mods in eval server-data template
  --auto-install-prereqs            Auto-install missing java/portablemc/Xvfb/xdotool/xwd/convert/ffmpeg when possible
  --dry-run                         Print planned actions only
  -h, --help                        Show help

Examples:
  scripts/eval/bootstrap-runtime-assets.sh
  scripts/eval/bootstrap-runtime-assets.sh --auto-install-prereqs
  scripts/eval/bootstrap-runtime-assets.sh --skip-virtual-display-prereqs
  scripts/eval/bootstrap-runtime-assets.sh --skip-client-warmup
  scripts/eval/bootstrap-runtime-assets.sh --skip-eval-templates --client-warmup-timeout-sec 120
EOF
}

log() {
    echo "[bootstrap] $*"
}

die() {
    echo "[bootstrap][error] $*" >&2
    exit 2
}

run_cmd() {
    if [[ "${DRY_RUN,,}" == "true" ]]; then
        echo "[dry-run] $*"
        return 0
    fi
    "$@"
}

is_nonempty_dir() {
    local p="$1"
    [[ -d "$p" ]] || return 1
    [[ -n "$(ls -A "$p" 2>/dev/null || true)" ]]
}

resolve_to_abs() {
    local path="$1"
    if [[ "$path" = /* ]]; then
        printf '%s\n' "$path"
    else
        printf '%s\n' "$PROJECT_ROOT/$path"
    fi
}

prepend_path_if_dir() {
    local dir="$1"
    [[ -d "$dir" ]] || return 0
    case ":$PATH:" in
        *":$dir:"*) ;;
        *) PATH="$dir:$PATH" ;;
    esac
}

ensure_user_local_bin_path() {
    prepend_path_if_dir "$HOME/.local/bin"
    prepend_path_if_dir "$HOME/.local/pipx/bin"
}

run_privileged_cmd() {
    if [[ "${DRY_RUN,,}" == "true" ]]; then
        echo "[dry-run] $*"
        return 0
    fi

    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
        return
    fi

    if command -v sudo >/dev/null 2>&1; then
        sudo "$@"
        return
    fi

    die "auto-install requires root privileges (run as root or ensure sudo is available)"
}

ensure_java() {
    if command -v java >/dev/null 2>&1; then
        return
    fi

    if [[ "${AUTO_INSTALL_PREREQS,,}" != "true" ]]; then
        die "java not found. Use --auto-install-prereqs or install Java first."
    fi

    log "java not found; attempting auto-install"
    if command -v apt-get >/dev/null 2>&1; then
        run_privileged_cmd apt-get update
        run_privileged_cmd apt-get install -y \
            -o Dpkg::Options::=--force-confdef \
            -o Dpkg::Options::=--force-confold \
            openjdk-21-jre-headless || run_privileged_cmd apt-get install -y \
            -o Dpkg::Options::=--force-confdef \
            -o Dpkg::Options::=--force-confold \
            openjdk-17-jre-headless
    elif command -v dnf >/dev/null 2>&1; then
        run_privileged_cmd dnf install -y java-21-openjdk-headless || run_privileged_cmd dnf install -y java-17-openjdk-headless
    elif command -v microdnf >/dev/null 2>&1; then
        run_privileged_cmd microdnf install -y java-21-openjdk-headless || run_privileged_cmd microdnf install -y java-17-openjdk-headless
    elif command -v yum >/dev/null 2>&1; then
        run_privileged_cmd yum install -y java-21-openjdk-headless || run_privileged_cmd yum install -y java-17-openjdk-headless
    elif command -v apk >/dev/null 2>&1; then
        run_privileged_cmd apk add --no-cache openjdk21-jre || run_privileged_cmd apk add --no-cache openjdk17-jre
    else
        die "java not found and no supported package manager detected (apt-get/dnf/microdnf/yum/apk)"
    fi

    if [[ "${DRY_RUN,,}" == "true" ]]; then
        log "dry-run: assume java would be installed"
        return
    fi

    command -v java >/dev/null 2>&1 || die "java auto-install failed"
}

ensure_portablemc() {
    ensure_user_local_bin_path
    if command -v portablemc >/dev/null 2>&1; then
        return
    fi

    if [[ "${AUTO_INSTALL_PREREQS,,}" != "true" ]]; then
        die "portablemc not found. Run inside runtime container, install portablemc first, or use --auto-install-prereqs."
    fi

    log "portablemc not found; attempting auto-install"
    if command -v pip3 >/dev/null 2>&1; then
        if [[ "$(id -u)" -eq 0 ]]; then
            run_cmd pip3 install portablemc --break-system-packages -q
        else
            run_cmd pip3 install --user portablemc -q
        fi
    elif command -v python3 >/dev/null 2>&1 && python3 -m pip --version >/dev/null 2>&1; then
        if [[ "$(id -u)" -eq 0 ]]; then
            run_cmd python3 -m pip install portablemc --break-system-packages -q
        else
            run_cmd python3 -m pip install --user portablemc -q
        fi
    elif command -v uv >/dev/null 2>&1; then
        run_cmd uv tool install portablemc
    else
        die "portablemc not found and no installer available (pip3/python3 -m pip/uv)"
    fi

    ensure_user_local_bin_path
    if [[ "${DRY_RUN,,}" == "true" ]]; then
        log "dry-run: assume portablemc would be installed"
        return
    fi

    command -v portablemc >/dev/null 2>&1 || die "portablemc auto-install failed. Ensure ~/.local/bin is in PATH."
}

ensure_virtual_display_tools() {
    local missing=()
    command -v Xvfb >/dev/null 2>&1 || missing+=("Xvfb")
    command -v xdotool >/dev/null 2>&1 || missing+=("xdotool")
    command -v xwd >/dev/null 2>&1 || missing+=("xwd")
    command -v convert >/dev/null 2>&1 || missing+=("convert")
    command -v ffmpeg >/dev/null 2>&1 || missing+=("ffmpeg")

    if [[ "${#missing[@]}" -eq 0 ]]; then
        return
    fi

    if [[ "${AUTO_INSTALL_PREREQS,,}" != "true" ]]; then
        die "missing virtual display/runtime capture tools: ${missing[*]}. Use --auto-install-prereqs or install Xvfb/xdotool/xwd/convert/ffmpeg first."
    fi

    log "virtual display/runtime capture tools missing (${missing[*]}); attempting auto-install"
    if command -v apt-get >/dev/null 2>&1; then
        run_privileged_cmd apt-get update
        run_privileged_cmd apt-get install -y \
            -o Dpkg::Options::=--force-confdef \
            -o Dpkg::Options::=--force-confold \
            xvfb xauth xdotool x11-apps imagemagick ffmpeg
    elif command -v dnf >/dev/null 2>&1; then
        run_privileged_cmd dnf install -y xorg-x11-server-Xvfb xdotool xorg-x11-xauth xorg-x11-apps ImageMagick ffmpeg || run_privileged_cmd dnf install -y xorg-x11-server-Xvfb xdotool xauth xorg-x11-apps ImageMagick ffmpeg
    elif command -v microdnf >/dev/null 2>&1; then
        run_privileged_cmd microdnf install -y xorg-x11-server-Xvfb xdotool xorg-x11-xauth xorg-x11-apps ImageMagick ffmpeg || run_privileged_cmd microdnf install -y xorg-x11-server-Xvfb xdotool xauth xorg-x11-apps ImageMagick ffmpeg
    elif command -v yum >/dev/null 2>&1; then
        run_privileged_cmd yum install -y xorg-x11-server-Xvfb xdotool xorg-x11-xauth xorg-x11-apps ImageMagick ffmpeg || run_privileged_cmd yum install -y xorg-x11-server-Xvfb xdotool xauth xorg-x11-apps ImageMagick ffmpeg
    elif command -v apk >/dev/null 2>&1; then
        run_privileged_cmd apk add --no-cache xvfb xauth xdotool imagemagick xwd ffmpeg
    else
        die "virtual display tools missing and no supported package manager detected (apt-get/dnf/microdnf/yum/apk)"
    fi

    if [[ "${DRY_RUN,,}" == "true" ]]; then
        log "dry-run: assume virtual display tools would be installed"
        return
    fi

    command -v Xvfb >/dev/null 2>&1 || die "Xvfb auto-install failed"
    command -v xdotool >/dev/null 2>&1 || die "xdotool auto-install failed"
    command -v xwd >/dev/null 2>&1 || die "xwd auto-install failed"
    command -v convert >/dev/null 2>&1 || die "convert auto-install failed"
    command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg auto-install failed"
}

ensure_virtual_display_prereqs_if_needed() {
    if [[ "${SKIP_VDISPLAY_PREREQS,,}" == "true" ]]; then
        log "skip virtual display prereqs"
        return
    fi
    ensure_virtual_display_tools
}

resolve_warmup_agentbridge_port() {
    local explicit_port="${AGENTBRIDGE_PORT:-}"
    if [[ -n "$explicit_port" ]]; then
        if [[ "$explicit_port" =~ ^[0-9]+$ ]] && [[ "$explicit_port" -ge 1 ]] && [[ "$explicit_port" -le 65535 ]]; then
            printf '%s\n' "$explicit_port"
            return 0
        fi
        die "invalid AGENTBRIDGE_PORT: $explicit_port"
    fi

    if command -v python3 >/dev/null 2>&1; then
        python3 - <<'PY'
import socket

for port in range(18080, 30000):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            continue
        print(port)
        break
else:
    print(18080)
PY
        return 0
    fi

    printf '%s\n' "18080"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project-root)
            [[ $# -ge 2 ]] || die "--project-root requires a value"
            PROJECT_ROOT="$2"
            shift 2
            ;;
        --game-dir)
            [[ $# -ge 2 ]] || die "--game-dir requires a value"
            GAME_DIR="$2"
            shift 2
            ;;
        --server-data-dir)
            [[ $# -ge 2 ]] || die "--server-data-dir requires a value"
            SERVER_DATA_DIR="$2"
            shift 2
            ;;
        --eval-client-template-dst)
            [[ $# -ge 2 ]] || die "--eval-client-template-dst requires a value"
            EVAL_CLIENT_TEMPLATE_DST="$2"
            shift 2
            ;;
        --eval-server-template-dst)
            [[ $# -ge 2 ]] || die "--eval-server-template-dst requires a value"
            EVAL_SERVER_TEMPLATE_DST="$2"
            shift 2
            ;;
        --mc-version)
            [[ $# -ge 2 ]] || die "--mc-version requires a value"
            MC_VERSION="$2"
            shift 2
            ;;
        --neoforge-version)
            [[ $# -ge 2 ]] || die "--neoforge-version requires a value"
            NEOFORGE_VERSION="$2"
            shift 2
            ;;
        --client-warmup-timeout-sec)
            [[ $# -ge 2 ]] || die "--client-warmup-timeout-sec requires a value"
            CLIENT_WARMUP_TIMEOUT_SEC="$2"
            shift 2
            ;;
        --bootstrap-player)
            [[ $# -ge 2 ]] || die "--bootstrap-player requires a value"
            BOOTSTRAP_PLAYER="$2"
            shift 2
            ;;
        --skip-virtual-display-prereqs)
            SKIP_VDISPLAY_PREREQS="true"
            shift
            ;;
        --skip-client-warmup)
            SKIP_CLIENT_WARMUP="true"
            shift
            ;;
        --force-client-warmup)
            FORCE_CLIENT_WARMUP="true"
            shift
            ;;
        --skip-server-install)
            SKIP_SERVER_INSTALL="true"
            shift
            ;;
        --skip-eval-templates)
            SKIP_EVAL_TEMPLATES="true"
            shift
            ;;
        --no-force-eval-templates)
            FORCE_EVAL_TEMPLATES="false"
            shift
            ;;
        --no-prune-server-volatile)
            PRUNE_SERVER_VOLATILE="false"
            shift
            ;;
        --auto-install-prereqs)
            AUTO_INSTALL_PREREQS="true"
            shift
            ;;
        --dry-run)
            DRY_RUN="true"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

PROJECT_ROOT="$(resolve_to_abs "$PROJECT_ROOT")"
GAME_DIR="$(resolve_to_abs "$GAME_DIR")"
SERVER_DATA_DIR="$(resolve_to_abs "$SERVER_DATA_DIR")"
EVAL_CLIENT_TEMPLATE_DST="$(resolve_to_abs "$EVAL_CLIENT_TEMPLATE_DST")"
EVAL_SERVER_TEMPLATE_DST="$(resolve_to_abs "$EVAL_SERVER_TEMPLATE_DST")"

if ! [[ "$CLIENT_WARMUP_TIMEOUT_SEC" =~ ^[0-9]+$ ]] || [[ "$CLIENT_WARMUP_TIMEOUT_SEC" -le 0 ]]; then
    die "--client-warmup-timeout-sec must be a positive integer"
fi

client_runtime_ready() {
    is_nonempty_dir "$GAME_DIR/libraries" && is_nonempty_dir "$GAME_DIR/versions"
}

warm_client_runtime_if_needed() {
    if [[ "${SKIP_CLIENT_WARMUP,,}" == "true" ]]; then
        log "skip client warmup"
        return
    fi

    if [[ "${FORCE_CLIENT_WARMUP,,}" == "true" ]]; then
        log "force client warmup: clearing $GAME_DIR"
        rm -rf "${GAME_DIR:?}/libraries" "${GAME_DIR:?}/versions" "${GAME_DIR:?}/assets" "${GAME_DIR:?}/jvm"
    fi

    if client_runtime_ready; then
        log "client runtime already present: $GAME_DIR"
        return
    fi

    ensure_portablemc
    command -v timeout >/dev/null 2>&1 || die "timeout command not found."

    run_cmd mkdir -p "$GAME_DIR"
    local warmup_agentbridge_port
    warmup_agentbridge_port="$(resolve_warmup_agentbridge_port)"
    local warmup_java_tool_options="${JAVA_TOOL_OPTIONS:-}"
    if [[ "$warmup_java_tool_options" == *"-Dagentbridge.port="* ]]; then
        log "client warmup uses existing JAVA_TOOL_OPTIONS agentbridge.port"
    else
        warmup_java_tool_options="${warmup_java_tool_options} -Dagentbridge.port=${warmup_agentbridge_port}"
        warmup_java_tool_options="${warmup_java_tool_options# }"
        log "client warmup agentbridge.port=${warmup_agentbridge_port}"
    fi
    log "warming client runtime via portablemc (timeout=${CLIENT_WARMUP_TIMEOUT_SEC}s)"

    local rc=0
    local -a base_cmd=(
        portablemc
        --main-dir "$GAME_DIR"
        --work-dir "$GAME_DIR"
        start "neoforge:${NEOFORGE_VERSION}"
        --username "$BOOTSTRAP_PLAYER"
    )

    local attempt=0
    local max_attempts="$CLIENT_WARMUP_RETRIES"
    while [[ "$attempt" -lt "$max_attempts" ]]; do
        attempt=$((attempt + 1))
        log "client warmup attempt ${attempt}/${max_attempts}"

        rc=0
        if [[ "${DRY_RUN,,}" == "true" ]]; then
            echo "[dry-run] timeout ${CLIENT_WARMUP_TIMEOUT_SEC}s ${base_cmd[*]}"
            rc=0
        else
            set +e
            (
                export JAVA_TOOL_OPTIONS="$warmup_java_tool_options"
                if command -v xvfb-run >/dev/null 2>&1; then
                    timeout "${CLIENT_WARMUP_TIMEOUT_SEC}s" xvfb-run -a "${base_cmd[@]}"
                    rc=$?
                else
                    timeout "${CLIENT_WARMUP_TIMEOUT_SEC}s" "${base_cmd[@]}"
                    rc=$?
                fi
                exit "$rc"
            )
            rc=$?
            set -e
        fi

        # rc=0: clean exit, rc=124/143: timeout (expected for warmup)
        if [[ "$rc" -eq 0 || "$rc" -eq 124 || "$rc" -eq 143 ]]; then
            break
        fi

        log "portablemc warmup exited with rc=${rc} (attempt ${attempt}/${max_attempts})"
        if [[ "$attempt" -lt "$max_attempts" ]]; then
            log "retrying in 5s... (portablemc will resume incomplete downloads)"
            sleep 5
        fi
    done

    if [[ "${DRY_RUN,,}" == "true" ]]; then
        log "dry-run: skip client runtime presence verification"
        return
    fi

    client_runtime_ready || die "client runtime warmup did not produce libraries/versions under: $GAME_DIR"
    log "client runtime ready: $GAME_DIR"
}

install_server_runtime_if_needed() {
    if [[ "${SKIP_SERVER_INSTALL,,}" == "true" ]]; then
        log "skip server install check"
        return
    fi

    if [[ -f "$SERVER_DATA_DIR/run.sh" ]] && is_nonempty_dir "$SERVER_DATA_DIR/libraries"; then
        log "server runtime already present: $SERVER_DATA_DIR"
        return
    fi

    ensure_java
    command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || die "curl or wget required."

    run_cmd mkdir -p "$SERVER_DATA_DIR"
    local installer_url="https://maven.neoforged.net/releases/net/neoforged/neoforge/${NEOFORGE_VERSION}/neoforge-${NEOFORGE_VERSION}-installer.jar"
    local installer_jar
    installer_jar="$(mktemp /tmp/neoforge-installer.XXXXXX.jar)"

    log "installing server runtime (mc=${MC_VERSION}, neoforge=${NEOFORGE_VERSION})"
    log "download: ${installer_url}"

    if [[ "${DRY_RUN,,}" == "true" ]]; then
        echo "[dry-run] download installer -> $installer_jar"
        echo "[dry-run] java -jar $installer_jar --installServer $SERVER_DATA_DIR"
    else
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL "$installer_url" -o "$installer_jar"
        else
            wget --timeout=30 --tries=3 "$installer_url" -O "$installer_jar"
        fi
        (
            cd "$SERVER_DATA_DIR"
            java -jar "$installer_jar" --installServer .
        )
        [[ -f "$SERVER_DATA_DIR/eula.txt" ]] || echo "eula=true" > "$SERVER_DATA_DIR/eula.txt"
    fi

    run_cmd rm -f "$installer_jar"

    if [[ "${DRY_RUN,,}" != "true" ]]; then
        [[ -f "$SERVER_DATA_DIR/run.sh" ]] || die "server runtime install failed: missing $SERVER_DATA_DIR/run.sh"
        is_nonempty_dir "$SERVER_DATA_DIR/libraries" || die "server runtime install failed: missing $SERVER_DATA_DIR/libraries"
    fi
    log "server runtime ready: $SERVER_DATA_DIR"
}

prepare_eval_templates() {
    if [[ "${SKIP_EVAL_TEMPLATES,,}" == "true" ]]; then
        log "skip eval template generation"
        return
    fi

    local helper="$SCRIPT_DIR/prepare-openha-eval-runtime-templates.sh"
    [[ -x "$helper" ]] || die "helper script not executable: $helper"

    local -a args=(
        --client-src "$GAME_DIR"
        --server-data-src "$SERVER_DATA_DIR"
        --client-template-dst "$EVAL_CLIENT_TEMPLATE_DST"
        --server-template-dst "$EVAL_SERVER_TEMPLATE_DST"
    )
    if [[ "${FORCE_EVAL_TEMPLATES,,}" == "true" ]]; then
        args+=(--force)
    fi
    if [[ "${PRUNE_SERVER_VOLATILE,,}" != "true" ]]; then
        args+=(--no-prune-server-volatile)
    fi
    if [[ "${DRY_RUN,,}" == "true" ]]; then
        args+=(--dry-run)
    fi

    log "generating eval templates"
    run_cmd "$helper" "${args[@]}"
}

log "project_root=$PROJECT_ROOT"
log "game_dir=$GAME_DIR"
log "server_data_dir=$SERVER_DATA_DIR"
log "eval_client_template_dst=$EVAL_CLIENT_TEMPLATE_DST"
log "eval_server_template_dst=$EVAL_SERVER_TEMPLATE_DST"
log "mc_version=$MC_VERSION neoforge_version=$NEOFORGE_VERSION"
log "skip_virtual_display_prereqs=$SKIP_VDISPLAY_PREREQS skip_client_warmup=$SKIP_CLIENT_WARMUP skip_server_install=$SKIP_SERVER_INSTALL skip_eval_templates=$SKIP_EVAL_TEMPLATES"
log "auto_install_prereqs=$AUTO_INSTALL_PREREQS"

ensure_virtual_display_prereqs_if_needed
warm_client_runtime_if_needed
install_server_runtime_if_needed
prepare_eval_templates

log "done"
