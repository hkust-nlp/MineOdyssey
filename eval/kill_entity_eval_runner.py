#!/usr/bin/env python3
"""
OpenHA-style kill_entity family eval runner wrapper.

This is the generic family entrypoint. Historical `kill_sheep` entrypoints remain
for backward compatibility, but new tasks should use this wrapper.
"""

from __future__ import annotations

from openha_eval_family import KILL_ENTITY_SPEC
from openha_eval_runner_base import run_family_eval_main


def main() -> None:
    run_family_eval_main(KILL_ENTITY_SPEC)


if __name__ == "__main__":
    main()
