#!/usr/bin/env python3
"""Serve the local live navigation evaluation dashboard."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.live_dashboard import main


if __name__ == "__main__":
    main()
