from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM_PATH = REPO_ROOT / "scripts/runtime/portablemc-neoforge-root-shim.py"
SPEC = importlib.util.spec_from_file_location("portablemc_neoforge_root_shim", SHIM_PATH)
assert SPEC is not None and SPEC.loader is not None
SHIM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHIM)


class PortableMCNeoForgeShimTests(unittest.TestCase):
    def test_injects_lwjgl_override_on_linux_arm64(self) -> None:
        original = ["shim.py", "--main-dir", "/game", "start", "neoforge:21.11.44"]

        actual = SHIM._inject_linux_arm64_lwjgl(
            original,
            system="Linux",
            machine="aarch64",
            version="3.3.3",
        )

        self.assertEqual(
            actual,
            [
                "shim.py",
                "--main-dir",
                "/game",
                "start",
                "--lwjgl",
                "3.3.3",
                "neoforge:21.11.44",
            ],
        )
        self.assertEqual(
            original,
            ["shim.py", "--main-dir", "/game", "start", "neoforge:21.11.44"],
        )

    def test_preserves_explicit_lwjgl_choice(self) -> None:
        original = [
            "shim.py",
            "start",
            "--lwjgl",
            "3.3.4",
            "neoforge:21.11.44",
        ]

        actual = SHIM._inject_linux_arm64_lwjgl(
            original,
            system="Linux",
            machine="arm64",
            version="3.3.3",
        )

        self.assertIs(actual, original)

    def test_does_not_change_non_arm_linux(self) -> None:
        original = ["shim.py", "start", "neoforge:21.11.44"]

        actual = SHIM._inject_linux_arm64_lwjgl(
            original,
            system="Linux",
            machine="x86_64",
            version="3.3.3",
        )

        self.assertIs(actual, original)

    def test_can_disable_automatic_override(self) -> None:
        original = ["shim.py", "start", "neoforge:21.11.44"]

        actual = SHIM._inject_linux_arm64_lwjgl(
            original,
            system="Linux",
            machine="aarch64",
            version="off",
        )

        self.assertIs(actual, original)


if __name__ == "__main__":
    unittest.main()
