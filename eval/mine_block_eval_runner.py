#!/usr/bin/env python3
"""
OpenHA-style mine_block:dirt eval runner wrapper.

This script keeps the historical entrypoint path stable while delegating the
shared eval flow to `openha_eval_runner_base.py`.
"""

from __future__ import annotations

from openha_eval_family import MINE_BLOCK_DIRT_SPEC
from openha_eval_runner_base import run_family_eval_main


def main() -> None:
    run_family_eval_main(MINE_BLOCK_DIRT_SPEC)


if __name__ == "__main__":
    main()
