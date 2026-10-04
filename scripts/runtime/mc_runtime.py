#!/usr/bin/env python3
"""Shared runtime config helpers for mcapi/xdo CLI tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

DEFAULT_RUNTIME_CONFIG_PATH = Path("/workspace/.mcbots_runtime.json")


def resolve_runtime_config_path() -> Path:
    override = os.environ.get("MCBOTS_RUNTIME_CONFIG", "").strip()
    if override:
        return Path(override)

    workspace_root = os.environ.get("MCBOTS_WORKSPACE_ROOT", "").strip()
    if workspace_root:
        return Path(workspace_root) / ".mcbots_runtime.json"

    cwd_candidate = Path.cwd() / ".mcbots_runtime.json"
    if cwd_candidate.is_file():
        return cwd_candidate

    for candidate in (
        DEFAULT_RUNTIME_CONFIG_PATH,
        Path("/workspace/mcbots/.mcbots_runtime.json"),
    ):
        if candidate.is_file():
            return candidate

    return DEFAULT_RUNTIME_CONFIG_PATH


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(
            f"Missing runtime config: {path}. "
            "Start the bot first so the launcher can generate this file."
        )
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to parse runtime config {path}: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid runtime config in {path}: root must be an object")
    return data


def load_runtime_config() -> Dict[str, Any]:
    return _read_json_file(resolve_runtime_config_path())


def get_endpoint(config: Dict[str, Any], section: str) -> Tuple[str, int]:
    runtime_config_path = resolve_runtime_config_path()
    raw = config.get(section)
    if not isinstance(raw, dict):
        raise RuntimeError(f"Missing endpoint object '{section}' in {runtime_config_path}")
    host = raw.get("host")
    port = raw.get("port")
    if not isinstance(host, str) or not host.strip():
        raise RuntimeError(f"Invalid '{section}.host' in {runtime_config_path}")
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise RuntimeError(f"Invalid '{section}.port' in {runtime_config_path}")
    return host.strip(), port


def get_display(config: Dict[str, Any]) -> str:
    runtime_config_path = resolve_runtime_config_path()
    x11 = config.get("x11")
    if not isinstance(x11, dict):
        raise RuntimeError(f"Missing 'x11' section in {runtime_config_path}")
    display = x11.get("display")
    if not isinstance(display, str) or not display.strip():
        raise RuntimeError(f"Invalid 'x11.display' in {runtime_config_path}")
    return display.strip()
