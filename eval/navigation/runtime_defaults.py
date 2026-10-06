"""Shared defaults for runtime preparation, single tasks, and container fleets."""

import os
from pathlib import Path
import platform


LINUX_CPU_IMAGE = "localhost/anonymous-navigation:linux-cpu"
LINUX_TEMPLATE_PATH = Path("eval/templates/_local/navigation-linux-cpu")


def runtime_template_root(repo_root: Path) -> Path:
    default = (
        LINUX_TEMPLATE_PATH if platform.system() == "Linux"
        else Path("eval/templates/_local/navigation")
    )
    return Path(os.environ.get(
        "MCBOTS_NAV_RUNTIME_TEMPLATE_ROOT", repo_root / default
    )).expanduser().resolve()
