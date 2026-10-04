#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

TASK_NAME="${TASK_NAME:-kill_entity:sheep}"
USE_IMPORTED_TASK_CONFIG="${USE_IMPORTED_TASK_CONFIG:-true}"
USE_IMPORTED_TASK_CONFIG="${USE_IMPORTED_TASK_CONFIG,,}"
MCBOTS_SINGLE_TASK_RESULT_LAYOUT="${MCBOTS_SINGLE_TASK_RESULT_LAYOUT:-single_task_summary_dir}"
export MCBOTS_SINGLE_TASK_RESULT_LAYOUT
OPENHA_SNAPSHOT_TEMPLATE_MODE="${OPENHA_SNAPSHOT_TEMPLATE_MODE:-required}"
OPENHA_SNAPSHOT_TEMPLATE_MODE="${OPENHA_SNAPSHOT_TEMPLATE_MODE,,}"
OPENHA_SNAPSHOT_PLAN="${OPENHA_SNAPSHOT_PLAN:-$PROJECT_ROOT/eval/runtime/openha_snapshot_group_plan_all.json}"
OPENHA_SNAPSHOT_ROOT="${OPENHA_SNAPSHOT_ROOT:-$PROJECT_ROOT/eval/snapshots}"
OPENHA_SNAPSHOT_WORLD_KIND="${OPENHA_SNAPSHOT_WORLD_KIND:-upgraded}"
OPENHA_SNAPSHOT_RAW_VERSION="${OPENHA_SNAPSHOT_RAW_VERSION:-1.16.5}"
OPENHA_SNAPSHOT_UPGRADED_VERSION="${OPENHA_SNAPSHOT_UPGRADED_VERSION:-1.21.1}"

canonicalize_task_name_compat() {
    local task_name="$1"
    case "$task_name" in
        "craft item "*)
            local suffix="${task_name#craft item }"
            suffix="${suffix// /_}"
            printf 'craft_item:%s\n' "$suffix"
            ;;
        *)
            printf '%s\n' "$task_name"
            ;;
    esac
}

infer_family_from_task_name() {
    local task_name="$1"
    case "$task_name" in
        kill_entity:*) printf 'kill_entity\n' ;;
        mine_block:*) printf 'mine_block\n' ;;
        custom:interact_with_*) printf 'interact_block\n' ;;
        interact_block:*) printf 'interact_block\n' ;;
        craft_item:*) printf 'craft_item\n' ;;
        smelt_item:*) printf 'smelt_item\n' ;;
        *)
            printf 'unknown\n'
            ;;
    esac
}

infer_runner_for_family() {
    local family="$1"
    case "$family" in
        kill_entity) printf '%s\n' "$PROJECT_ROOT/eval/kill_entity_eval_runner.py" ;;
        mine_block) printf '%s\n' "$PROJECT_ROOT/eval/mine_block_family_eval_runner.py" ;;
        interact_block) printf '%s\n' "$PROJECT_ROOT/eval/interact_block_eval_runner.py" ;;
        craft_item) printf '%s\n' "$PROJECT_ROOT/eval/craft_item_eval_runner.py" ;;
        smelt_item) printf '%s\n' "$PROJECT_ROOT/eval/smelt_item_eval_runner.py" ;;
        *)
            return 1
            ;;
    esac
}

infer_min_config_for_family() {
    local family="$1"
    case "$family" in
        kill_entity) printf '%s\n' "$PROJECT_ROOT/eval/openha_assets/kill_entity_min.json" ;;
        mine_block) printf '%s\n' "$PROJECT_ROOT/eval/openha_assets/mine_block_min.json" ;;
        interact_block) printf '%s\n' "$PROJECT_ROOT/eval/openha_assets/interact_block_min.json" ;;
        craft_item) printf '%s\n' "$PROJECT_ROOT/eval/openha_assets/craft_item_min.json" ;;
        smelt_item) printf '%s\n' "$PROJECT_ROOT/eval/openha_assets/smelt_item_min.json" ;;
        *)
            return 1
            ;;
    esac
}

TASK_NAME="$(canonicalize_task_name_compat "$TASK_NAME")"
family="$(infer_family_from_task_name "$TASK_NAME")"
if [[ "$family" == "unknown" ]]; then
    echo "Unsupported or unrecognized TASK_NAME family: $TASK_NAME" >&2
    echo "Set TASK_CONFIG and RUNNER_PATH explicitly, or use a supported family prefix." >&2
    exit 2
fi

if [[ -z "${RUNNER_PATH:-}" ]]; then
    if ! RUNNER_PATH="$(infer_runner_for_family "$family")"; then
        echo "No runner mapping yet for family=$family (TASK_NAME=$TASK_NAME)" >&2
        exit 2
    fi
fi
export RUNNER_PATH

if [[ -z "${TASK_CONFIG:-}" ]]; then
    imported_cfg="$PROJECT_ROOT/eval/openha_assets/imported/task_configs/${family}.json"
    if [[ "$USE_IMPORTED_TASK_CONFIG" == "true" && -f "$imported_cfg" ]]; then
        TASK_CONFIG="$imported_cfg"
    else
        if ! TASK_CONFIG="$(infer_min_config_for_family "$family")"; then
            echo "No default TASK_CONFIG mapping yet for family=$family (TASK_NAME=$TASK_NAME)" >&2
            echo "Imported config missing: $imported_cfg" >&2
            exit 2
        fi
    fi
fi
export TASK_CONFIG

if [[ -z "${TASK_INSTRUCTION_FILE:-}" ]]; then
    imported_instructions="$PROJECT_ROOT/eval/openha_assets/imported/instructions_by_task.json"
    if [[ "$USE_IMPORTED_TASK_CONFIG" == "true" && -f "$imported_instructions" ]]; then
        TASK_INSTRUCTION_FILE="$imported_instructions"
        export TASK_INSTRUCTION_FILE
    fi
fi

export TASK_NAME

if [[ -z "${TEMPLATE_WORLD_DIR:-}" && "$OPENHA_SNAPSHOT_TEMPLATE_MODE" != "off" ]]; then
    if [[ -f "$OPENHA_SNAPSHOT_PLAN" ]]; then
        if resolved_snapshot="$(
            python3 "$SCRIPT_DIR/resolve-openha-task-snapshot.py" \
                --task-name "$TASK_NAME" \
                --task-config "$TASK_CONFIG" \
                --plan "$OPENHA_SNAPSHOT_PLAN" \
                --snapshot-root "$OPENHA_SNAPSHOT_ROOT" \
                --project-root "$PROJECT_ROOT" \
                --world-kind "$OPENHA_SNAPSHOT_WORLD_KIND" \
                --raw-version "$OPENHA_SNAPSHOT_RAW_VERSION" \
                --upgraded-version "$OPENHA_SNAPSHOT_UPGRADED_VERSION" \
                2>/dev/null
        )"; then
            if [[ -n "$resolved_snapshot" ]]; then
                TEMPLATE_WORLD_DIR="$resolved_snapshot"
                export TEMPLATE_WORLD_DIR
                echo "[info] using snapshot-backed TEMPLATE_WORLD_DIR: $TEMPLATE_WORLD_DIR"
            fi
        else
            if [[ "$OPENHA_SNAPSHOT_TEMPLATE_MODE" == "required" ]]; then
                echo "[error] failed to resolve task snapshot template (mode=required)." >&2
                echo "  task=$TASK_NAME task_config=$TASK_CONFIG plan=$OPENHA_SNAPSHOT_PLAN" >&2
                exit 2
            fi
            echo "[info] no matching built snapshot for task; fallback to default template world."
        fi
    else
        if [[ "$OPENHA_SNAPSHOT_TEMPLATE_MODE" == "required" ]]; then
            echo "[error] snapshot plan not found (mode=required): $OPENHA_SNAPSHOT_PLAN" >&2
            exit 2
        fi
        echo "[info] snapshot plan not found; fallback to default template world: $OPENHA_SNAPSHOT_PLAN"
    fi
fi

exec "$SCRIPT_DIR/run-openha-eval-inside.sh" "$@"
