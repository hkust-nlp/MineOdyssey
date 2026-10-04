#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

CLIENT_SRC=""
SERVER_DATA_SRC=""
CLIENT_TEMPLATE_DST="$PROJECT_ROOT/eval/templates/client_game"
SERVER_DATA_TEMPLATE_DST="$PROJECT_ROOT/eval/templates/server-data-neoforge-1.21.1"
FORCE="false"
DRY_RUN="false"
PRUNE_SERVER_VOLATILE="true"

usage() {
    cat <<'EOF'
Prepare dedicated eval runtime templates (client game dir + server-data dir).

This script explicitly copies from source dirs you provide into eval-only template paths.
It does NOT auto-read main-chain runtime dirs unless you pass them as --*-src.

Usage:
  scripts/eval/prepare-openha-eval-runtime-templates.sh [options]

Options:
  --client-src DIR            Source client game directory to snapshot into eval template
  --server-data-src DIR       Source installed server-data directory (with NeoForge/run.sh)
  --client-template-dst DIR   Destination client template dir
  --server-template-dst DIR   Destination server-data template dir
  --no-prune-server-volatile  Keep world/logs/mods in copied server-data template
  --force                     Replace existing destination template dirs
  --dry-run                   Print actions only
  -h, --help                  Show this help

Notes:
  - At least one of --client-src / --server-data-src is required.
  - Server-data template copy prunes volatile directories by default:
    world/, logs/, crash-reports/, mods/
EOF
}

die() {
    echo "ERROR: $*" >&2
    exit 2
}

is_nonempty_dir() {
    local p="$1"
    [[ -d "$p" ]] || return 1
    [[ -n "$(ls -A "$p" 2>/dev/null || true)" ]]
}

copy_dir_snapshot() {
    local src="$1"
    local dst="$2"
    local label="$3"
    local src_real=""

    [[ -d "$src" ]] || die "$label source not found: $src"
    is_nonempty_dir "$src" || die "$label source is empty: $src"
    src_real="$(cd "$src" && pwd -P)"
    [[ -n "$src_real" ]] || die "$label failed to resolve real source path: $src"

    if [[ -e "$dst" ]]; then
        if [[ "${FORCE,,}" != "true" ]]; then
            die "$label destination already exists (use --force to replace): $dst"
        fi
        if [[ "${DRY_RUN,,}" == "true" ]]; then
            echo "[dry-run] rm -rf $dst"
        else
            rm -rf "$dst"
        fi
    fi

    if [[ "${DRY_RUN,,}" == "true" ]]; then
        echo "[dry-run] mkdir -p $(dirname "$dst")"
        echo "[dry-run] cp -a '$src_real' '$dst'"
        return
    fi

    mkdir -p "$(dirname "$dst")"
    cp -a --reflink=auto "$src_real" "$dst" 2>/dev/null || cp -a "$src_real" "$dst"
}

prune_server_data_template() {
    local dst="$1"
    local path
    for path in \
        "$dst/world" \
        "$dst/logs" \
        "$dst/crash-reports" \
        "$dst/mods"
    do
        if [[ -e "$path" || -L "$path" ]]; then
            if [[ "${DRY_RUN,,}" == "true" ]]; then
                echo "[dry-run] rm -rf $path"
            else
                rm -rf "$path"
            fi
        fi
    done
}

validate_server_data_template() {
    local dst="$1"
    local has_run_sh="false"
    local has_run_bat="false"
    local has_libraries="false"
    [[ -f "$dst/run.sh" ]] && has_run_sh="true"
    [[ -f "$dst/run.bat" ]] && has_run_bat="true"
    [[ -d "$dst/libraries" ]] && has_libraries="true"

    if [[ "$has_run_sh" != "true" && "$has_run_bat" != "true" && "$has_libraries" != "true" ]]; then
        die "server-data template copy looks invalid (missing run.sh/run.bat/libraries): $dst"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --client-src)
            [[ $# -ge 2 ]] || die "--client-src requires a path"
            CLIENT_SRC="$2"
            shift 2
            ;;
        --server-data-src)
            [[ $# -ge 2 ]] || die "--server-data-src requires a path"
            SERVER_DATA_SRC="$2"
            shift 2
            ;;
        --client-template-dst)
            [[ $# -ge 2 ]] || die "--client-template-dst requires a path"
            CLIENT_TEMPLATE_DST="$2"
            shift 2
            ;;
        --server-template-dst)
            [[ $# -ge 2 ]] || die "--server-template-dst requires a path"
            SERVER_DATA_TEMPLATE_DST="$2"
            shift 2
            ;;
        --no-prune-server-volatile)
            PRUNE_SERVER_VOLATILE="false"
            shift
            ;;
        --force)
            FORCE="true"
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
            die "unknown arg: $1 (use --help)"
            ;;
    esac
done

if [[ -z "$CLIENT_SRC" && -z "$SERVER_DATA_SRC" ]]; then
    die "at least one of --client-src / --server-data-src is required"
fi

if [[ -n "$CLIENT_SRC" ]]; then
    [[ "$CLIENT_SRC" = /* ]] || CLIENT_SRC="$PROJECT_ROOT/$CLIENT_SRC"
    [[ "$CLIENT_TEMPLATE_DST" = /* ]] || CLIENT_TEMPLATE_DST="$PROJECT_ROOT/$CLIENT_TEMPLATE_DST"
    echo "Prepare eval client template:"
    echo "  src=$CLIENT_SRC"
    echo "  dst=$CLIENT_TEMPLATE_DST"
    copy_dir_snapshot "$CLIENT_SRC" "$CLIENT_TEMPLATE_DST" "client template"
fi

if [[ -n "$SERVER_DATA_SRC" ]]; then
    [[ "$SERVER_DATA_SRC" = /* ]] || SERVER_DATA_SRC="$PROJECT_ROOT/$SERVER_DATA_SRC"
    [[ "$SERVER_DATA_TEMPLATE_DST" = /* ]] || SERVER_DATA_TEMPLATE_DST="$PROJECT_ROOT/$SERVER_DATA_TEMPLATE_DST"
    echo "Prepare eval server-data template:"
    echo "  src=$SERVER_DATA_SRC"
    echo "  dst=$SERVER_DATA_TEMPLATE_DST"
    copy_dir_snapshot "$SERVER_DATA_SRC" "$SERVER_DATA_TEMPLATE_DST" "server-data template"
    if [[ "${PRUNE_SERVER_VOLATILE,,}" == "true" ]]; then
        echo "  prune volatile dirs: world logs crash-reports mods"
        prune_server_data_template "$SERVER_DATA_TEMPLATE_DST"
    fi
    if [[ "${DRY_RUN,,}" != "true" ]]; then
        validate_server_data_template "$SERVER_DATA_TEMPLATE_DST"
    fi
fi

echo "Done."
