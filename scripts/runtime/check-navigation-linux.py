#!/usr/bin/env python3
"""Exercise the actual CPU graphics and command-sandbox dependencies, without a model."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def checked(argv: list[str]) -> str:
    result = subprocess.run(argv, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} failed: {result.stderr[-2000:] or result.stdout[-2000:]}")
    return result.stdout.strip()


def main() -> None:
    required = ["java", "uv", "mcrcon", "Xvfb", "xvfb-run", "xauth", "glxinfo",
                "xdotool", "xwd", "convert", "bwrap", "x11vnc", "websockify"]
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise SystemExit("Missing runtime tools: " + ", ".join(missing))
    from PIL import Image  # noqa: F401
    import flask  # noqa: F401
    import openai  # noqa: F401
    import portablemc  # noqa: F401
    graphics = checked(["xvfb-run", "-a", "-s", "-screen 0 800x600x24", "glxinfo", "-B"])
    with tempfile.TemporaryDirectory(prefix="navigation-doctor-") as temp:
        shot = str(Path(temp) / "frame.png")
        checked(["xvfb-run", "-a", "-s", "-screen 0 800x600x24", "bash", "-o", "pipefail", "-c",
                 'xwd -root -silent | convert xwd:- "$1"', "capture", shot])
        with Image.open(shot) as picture:
            assert picture.size == (800, 600), picture.size
    # Check the same filesystem/capability isolation primitives the agent uses.
    sandbox = checked(["bwrap", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
                       "--unshare-pid", "--unshare-ipc", "--unshare-uts",
                       "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib",
                       "--symlink", "/usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev",
                       "--tmpfs", "/workspace", "--chdir", "/workspace",
                       *(["--ro-bind", "/lib64", "/lib64"] if Path('/lib64').exists() else []),
                       "/bin/bash", "-c", 'test "$PWD" = /workspace && test ! -e /workspace/mcbots && echo sandbox-ok'])
    java = subprocess.run(["java", "-version"], capture_output=True, text=True, check=True).stderr.splitlines()[0]
    print(json.dumps({"status": "ok", "java": java, "graphics": graphics,
                      "screenshot_size": [800, 600], "sandbox": sandbox,
                      "render_mode": os.environ.get("RENDER_MODE")}, indent=2))


if __name__ == "__main__":
    main()
