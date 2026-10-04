#!/usr/bin/env python3
"""Run PortableMC with its NeoForge 1.21.11 ROOT-variable compatibility shim."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


def _inject_linux_arm64_lwjgl(
    argv: list[str],
    *,
    system: str | None = None,
    machine: str | None = None,
    version: str | None = None,
) -> list[str]:
    """Select PortableMC's ARM64 LWJGL artifacts on Linux.

    Mojang's Linux metadata only names the conventional x86-64 native
    classifier. PortableMC's --lwjgl override resolves the matching ARM64
    artifacts instead. Keep explicit caller choices authoritative.
    """

    current_system = (system or platform.system()).lower()
    current_machine = (machine or platform.machine()).lower()
    if current_system != "linux" or current_machine not in {"aarch64", "arm64"}:
        return argv
    if any(arg == "--lwjgl" or arg.startswith("--lwjgl=") for arg in argv):
        return argv
    try:
        start_index = argv.index("start")
    except ValueError:
        return argv

    selected = (
        version
        if version is not None
        else os.environ.get("MCBOTS_PORTABLEMC_LWJGL_VERSION", "3.3.3")
    ).strip()
    if selected.lower() in {"", "none", "off"}:
        return argv
    return [
        *argv[: start_index + 1],
        "--lwjgl",
        selected,
        *argv[start_index + 1 :],
    ]


def _install_root_variable_shim() -> None:
    from portablemc import forge

    original_finalize = forge.ForgeVersion._finalize_forge_internal

    def finalize_with_root(self, watcher):  # type: ignore[no-untyped-def]
        post_info = getattr(self, "_forge_post_info", None)
        if post_info is not None:
            main_dir = Path(self.context.versions_dir).parent
            post_info.variables.setdefault("ROOT", str(main_dir.absolute()))
        return original_finalize(self, watcher)

    forge.ForgeVersion._finalize_forge_internal = finalize_with_root


def main() -> int:
    sys.argv = _inject_linux_arm64_lwjgl(sys.argv)
    _install_root_variable_shim()
    from portablemc.cli import main as portablemc_main

    return int(portablemc_main() or 0)


if __name__ == "__main__":
    if sys.argv[0].endswith(".exe"):
        sys.argv[0] = sys.argv[0][:-4]
    raise SystemExit(main())
