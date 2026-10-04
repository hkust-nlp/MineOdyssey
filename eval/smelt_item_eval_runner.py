#!/usr/bin/env python3
"""
OpenHA-style smelt_item family eval runner wrapper.
"""

from __future__ import annotations

from openha_eval_family import SMELT_ITEM_SPEC
from openha_eval_runner_base import run_family_eval_main


def main() -> None:
    run_family_eval_main(SMELT_ITEM_SPEC)


if __name__ == "__main__":
    main()
