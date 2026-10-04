# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**mcbots** is a Minecraft AI Agent evaluation framework. GPU-accelerated Podman containers run Minecraft with a custom NeoForge mod (AgentBridge) that exposes HTTP APIs. Python agents perceive the game via screenshots and control it via HTTP endpoints, xdotool, and Baritone chat commands.

**Tech stack**: Minecraft 1.21.1 + NeoForge 21.1.77 + Baritone 1.11.2 | Python 3.12+ (uv) | Java 21 (Gradle) | Podman/NVIDIA GPU | OpenAI API for VLM decisions

## Architecture

```
┌─ Minecraft Container (GPU) ─────────────────────────────┐
│  AgentBridge Mod (NanoHTTPD :8080) → HTTP API endpoints  │
│  Baritone Mod → pathfinding via chat commands (#goto etc) │
│  Xvfb :1 + VirtualGL → headless GPU rendering            │
│  remote_bash_server.py (Flask :9090) → xdotool/file ops  │
└──────────────────────────────────────────────────────────┘
                    ↓ HTTP
┌─ Python Agent (host) ───────────────────────────────────┐
│  main.py → orchestrates Environment + Agent + VLM        │
│  env.py → async screenshot + action execution threads    │
│  agent.py → observation loop + VLM decision loop         │
│  minecraft_api.py → HTTP client for AgentBridge + Bash   │
│  frame_filter.py → frame deduplication + adaptive budget  │
└──────────────────────────────────────────────────────────┘
```

**Control layers** (low → high): AgentBridge API (:8080, ~2ms) → Remote Bash / xdotool (:9090) → Baritone chat commands

## Build & Test Commands

```bash
# Python
uv sync                                                    # Install dependencies
uv run python -m unittest tests/test_frame_filter.py       # Unit tests (no container needed)
uv run python tests/test_apis.py                           # Integration tests (needs running container)
uv run python tests/test_async_env.py                      # Async env smoke test (needs container)

# Java AgentBridge Mod
cd src/agentbridge && ./gradlew build                      # Output: build/libs/agentbridge-1.0.0.jar

# Container images
podman build -t mc-agent-gpu-ubuntu2204 -f containers/Containerfile .
podman build -t mc-agent-unified-ubuntu2204 -f containers/Containerfile.unified .

# Launch
./scripts/launch/start-single-bot.sh Bot1 0                # Single bot on GPU 0
./scripts/launch/start-single-bot-vnc.sh BotVNC cpu        # With VNC
./scripts/containers/run-unified-container-once.sh          # Unified dev container
```

## Module Layout

| Path | Purpose |
|------|---------|
| `agent/` | Python agent: orchestration (main.py), environment (env.py), VLM decision loop (agent.py), API client (minecraft_api.py), frame optimization (frame_filter.py) |
| `src/agentbridge/` | Java NeoForge mod: HTTP server (BridgeServer), API handlers (api/), Baritone integration |
| `eval/` | OpenHA benchmarks: task runners (*_eval_runner.py), CLI (openha_eval_cli.py), snapshots, templates |
| `scripts/launch/` | Bot/server startup (start-single-bot*.sh, run-server-only.sh) |
| `scripts/runtime/` | CLI tools: `mcapi` (AgentBridge wrapper), `xdo` (xdotool wrapper), port/display registries, remote_bash_server.py |
| `scripts/eval/` | Eval bootstrap, asset import, task validation |
| `scripts/entrypoints/` | Container init scripts (entrypoint.sh, server-entrypoint.sh) |
| `containers/` | Containerfiles: base GPU, unified, server, VNC, stream variants |
| `tests/` | test_frame_filter.py (unit), test_apis.py + test_async_env.py (integration, needs container) |
| `config/` | Templates: api_models.example.json, server.properties |

## Conventions

- **Commit messages**: `<scope>: <imperative message>` — scopes: agent, eval, docs, scripts, containers
- **Python**: snake_case functions, PascalCase classes, 4-space indent
- **Java**: PascalCase classes, camelCase methods, package `com.mcagent.bridge`
- **Shell**: `set -euo pipefail`, UPPER_SNAKE_CASE env vars
- **Scripts**: organized by category under scripts/ — never add top-level scripts
- **Tests**: automated tests in `tests/test_*.py`; unit tests must be container-independent

## Configuration

- **Runtime config** (`.mcbots_runtime.json`): generated per-agent with agentbridge/remote_bash endpoints
- **API keys** (`config/api_models.json`): derived from `config/api_models.example.json` — never commit
- **Eval mode**: env vars EVAL_MODE, TASK_NAME, TASK_CONFIG control task evaluation in main.py
- Runtime artifacts (`game/`, `workspaces/`, `eval/results/`, logs, screenshots) are gitignored
