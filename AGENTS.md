# Repository Guidelines

## Project Structure & Module Organization
- `agent/`: Python runtime and control logic (`agent.py`, `env.py`, `main.py`, `minecraft_api.py`).
- `eval/`: OpenHA task runners, assets, templates/snapshots, and result tooling.
- `scripts/`: Script categories only; avoid top-level script files.
- `scripts/eval/`: OpenHA eval entrypoints/helpers, asset tooling, and eval validation scripts.
- `scripts/containers/`: Container lifecycle entry scripts.
- `scripts/entrypoints/`: Container-internal process entrypoints.
- `scripts/launch/`: Local server/client launch orchestration scripts.
- `scripts/snapshot/`: Snapshot planning/build/coverage validation scripts.
- `scripts/analysis/`: Recording/telemetry analysis and visualization helpers.
- `scripts/runtime/`: Runtime helper tools (registries, remote bash service, mcapi/xdo helpers).
- `src/agentbridge/`: Java NeoForge mod (`com.mcagent.bridge`).
- `containers/`: Container build variants (base/server/VNC/stream).
- `tests/`: Unit-style tests plus runtime integration scripts.
- `docs/`: Small or topic-specific documents.
- `config/`: Config templates (`api_models.example.json`, `server.properties`).
- `configs/`: Tracked navigation fleet model parameters, eval settings, and task lists.

## Build, Test, and Development Commands
- `uv sync`: Install Python dependencies.
- `uv run python -m unittest tests/test_frame_filter.py`: Run deterministic unit tests.
- `uv run python tests/test_apis.py`: Manual API integration check (needs running bot container).
- `uv run python tests/test_async_env.py`: Manual async env smoke test (needs runtime endpoints).
- `cd src/agentbridge && ./gradlew build`: Build AgentBridge mod jar.
- `./scripts/containers/run-unified-container-once.sh`: Start unified dev container.

## Coding Style & Naming Conventions
- Python: 4-space indentation, type hints when useful, `snake_case` functions/modules, `PascalCase` classes.
- Java: `PascalCase` classes and `camelCase` methods under `com.mcagent.bridge`.
- Shell: executable scripts, explicit `UPPER_SNAKE_CASE` env vars, use `set -euo pipefail` where practical.

## Testing Guidelines
- Put automated tests in `tests/test_*.py`, and keep logic tests independent from container state.
- Treat `tests/test_apis.py` and `tests/test_async_env.py` as integration/manual checks against a live runtime.
- No hard coverage gate yet; add tests for behavior changes in `agent/` and eval utilities.

## Documentation Workflow
- Place small or topic-focused docs in `docs/` (example: `docs/runtime-port-layout.md`).
- Keep `docs/README.md` as the index.
- Whenever docs are added/renamed/removed, update both `docs/README.md` and the map below in the same change.

## Documentation Map (Keep In Sync)
- `eval/harbor/innopolis-006/README.md`: Harbor task template, pinned hkust-nlp/MineOdyssey release map download, runtime setup and historical validation scope.
- `docs/assets/paper/README.md`: README figures matched to the September 29 manuscript package, with source versions, PDF/PNG hashes, credits and presentation boundaries.
- `docs/task-catalog.md`: Source-bound 180-task/30-map inventory, locale tags, task schema and the matching 30-map release with pinned hashes.
- `docs/harbor-parity-audit.md`: Original navigation-agent versus Harbor parity audit, effective retry policy, context/action/event differences and runtime evidence.
- `docs/harbor-pilot.md`: Actual formal GLM policy and coordinate-lock validation, rootless Podman launch, trusted model metadata, transport failure classification, catalog task export from all 30 verified release map archives or snapshot caches, multi-task Harbor rollouts, shared Agent integration, formal native and historical XML prompt hashes, generated instructions, dependency parity, networkless game service, trusted verification, bounded synchronous CLI timeouts, explicit unscored terminal reasons, container-local original Agent execution and ephemeral credential delivery.
- `README.md`: Public MineOdyssey overview at hkust-nlp/MineOdyssey and scope of agentic spatial intelligence, CPU quickstart, 30-map downloads, named formal runs and aggregation, shared batch setup, task and evaluation contract, original/Harbor branches, and validation scope.
- `docs/README.md`: Index for focused docs under `docs/`, with the canonical hkust-nlp/MineOdyssey repository and setup links.
- `docs/navigation-eval.md`: Minecraft 1.21.11 single-task and batch evaluation, shared runtime defaults, GPU setup, release-aware aggregation, and labeled legacy preparation notes.
- `docs/scripts-layout.md`: Script directory grouping and lookup guide.
- `docs/self-reward-grader-quirks.md`: Self-reward grader quirks (Kimi reasoning_content drift + "no reasoning/no response" cases).
- `agent/examples/README.md`: Bot-side API usage examples.

## Commit & Pull Request Guidelines
- Follow concise, imperative commit messages, commonly with scope prefixes (for example `agent:`, `eval:`, `docs:`).
- Keep commits focused by concern (agent, eval, scripts/infra, docs).
- PRs should include goal, key changes, verification commands, and runtime context (container mode, GPU/CPU path).
- For doc changes, include matching index/map updates (`docs/README.md` and this file).

## Security & Configuration Tips
- Never commit secrets or local credentials; keep `config/api_models.json` local and derive it from `config/api_models.example.json`.
- Avoid committing runtime artifacts (`game/`, `workspaces/`, `eval/results/`, logs/screenshots), which are intentionally ignored.

- `docs/anonymous-release.md`: Selected runtime fixes, nine waypoint corrections, formal-profile alignment, anonymous-export versus development-branch scope and validation limits.

- `docs/linux-quickstart.md`: Generic Linux Docker/Podman CPU setup, CPU/GPU rendering scope and virtual display, resource-measurement status, actual graphics/sandbox checks, verified downloads and imports for one or all 30 release maps, named formal runs, result aggregation, and shared single-task/batch runtime defaults.
