# Scripts Layout

This repository now groups `scripts/` by purpose to reduce top-level clutter.

## Top-Level (`scripts/`)
- Keep top-level clean: no first-level script files.
- Place all scripts in categorized subdirectories below.

## `scripts/eval/`
- OpenHA eval tooling and validation helpers.
- Examples:
  - `scripts/eval/run-openha-task-eval-inside.sh`
  - `scripts/eval/run-openha-dev-subset-eval-inside.sh`
  - `scripts/eval/resolve-openha-task-snapshot.py`
  - `scripts/eval/run-openha-dev-subset-eval-inside.py`
  - `scripts/eval/prepare-openha-eval-runtime-templates.sh`
  - `scripts/eval/validate-openha-*.py`
  - `scripts/eval/run-navigation-benchmark.py`
  - `scripts/eval/run-navigation-fleet.py`
  - `scripts/eval/aggregate-navigation-results.py`
  - `scripts/eval/set_ground_navigation_route.py`

## `scripts/containers/`
- Container lifecycle entry scripts.
- Examples:
  - `scripts/containers/run-unified-container-once.sh`
  - `scripts/containers/navigation-cpu-macos.sh`
  - `scripts/containers/start-openha-eval-container.sh`

## `scripts/entrypoints/`
- Container-internal process entrypoints.
- Examples:
  - `scripts/entrypoints/entrypoint.sh`
  - `scripts/entrypoints/entrypoint-vnc.sh`
  - `scripts/entrypoints/entrypoint-gpusr.sh`
  - `scripts/entrypoints/server-entrypoint.sh`

## `scripts/launch/`
- Local launch/orchestration scripts for server and bot clients.
- Examples:
  - `scripts/launch/start-single-bot.sh`
  - `scripts/launch/start-single-bot-vnc.sh`
  - `scripts/launch/start-single-bot-stream.sh`
  - `scripts/launch/start-single-bot-gpusr.sh`
  - `scripts/launch/start-server-clients-inside.sh`
  - `scripts/launch/run-server-only.sh`

## `scripts/snapshot/`
- Snapshot planning/building and coverage validation.
- Examples:
  - `scripts/snapshot/build-seed-snapshot.py`
  - `scripts/snapshot/build-snapshot-groups.py`
  - `scripts/snapshot/prepare-navigation-snapshot.py`
  - `scripts/snapshot/validate-snapshot-group-coverage.py`

## `scripts/analysis/`
- Recording/telemetry analysis and visualization tools.
- Examples:
  - `scripts/analysis/capture-practical-panorama-inside.sh`
  - `scripts/analysis/replay_frame_filter_on_screenshots.py`
  - `scripts/analysis/summarize_frame_filter_telemetry.py`
  - `scripts/analysis/trajectory_viewer.py`

## `scripts/runtime/`
- Runtime helper utilities used by entrypoints/eval core.
- Examples:
  - `scripts/runtime/port_registry.py`
  - `scripts/runtime/display_registry.py`
  - `scripts/runtime/remote_bash_server.py`
  - `scripts/runtime/portablemc-neoforge-root-shim.py`
  - `scripts/runtime/ground_navigation.sh`
  - `scripts/runtime/mcapi`, `scripts/runtime/xdo`
