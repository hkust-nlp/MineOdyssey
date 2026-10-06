"""Host launcher boundaries: no credentials in argv; portable paths and safe defaults."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eval.navigation import runtime_defaults
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("navigation_linux", ROOT / "scripts/launch/navigation-linux.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LinuxLauncherTest(unittest.TestCase):
    def args(self, *argv):
        return launcher.parser().parse_args(list(argv))

    def test_tasks_do_not_require_container_engine(self):
        self.assertTrue(launcher.task_catalog())
        self.assertIn('innopolis-006', launcher.task_catalog())

    def test_default_review_uses_no_key_and_exposes_no_host_ports(self):
        args = self.args('--dry-run', 'run', '--task', 'innopolis-006')
        with patch.dict(os.environ, {}, clear=True):
            launcher.validate(args)
            cmd = launcher.container_command(args, 'podman')
        self.assertNotIn('--publish', cmd)
        self.assertNotIn('MCBOTS_API_KEY', cmd)
        self.assertNotIn('--privileged', cmd)
        self.assertIn('review', cmd)
        self.assertIn('LIBGL_ALWAYS_SOFTWARE=1', cmd)

    def test_vnc_is_loopback_only(self):
        args = self.args('--dry-run', 'run', '--task', 'innopolis-006', '--vnc', '--vnc-port', '6088')
        cmd = launcher.container_command(args, 'docker')
        self.assertEqual(cmd[cmd.index('--publish')+1], '127.0.0.1:6088:6080')

    def test_provider_key_never_appears_in_command(self):
        args = self.args('--dry-run', 'run', '--mode', 'pilot', '--task', 'innopolis-006', '--model-id', 'example-model')
        with patch.dict(os.environ, {'MCBOTS_API_KEY': 'sensitive-test-value', 'MCBOTS_BASE_URL': 'https://example.invalid/v1'}):
            cmd = launcher.container_command(args, 'docker')
        self.assertIn('MCBOTS_API_KEY', cmd)
        self.assertNotIn('sensitive-test-value', ' '.join(cmd))
        self.assertNotIn('https://example.invalid/v1', ' '.join(cmd))

    def test_real_model_run_without_key_fails_before_starting_container(self):
        args = self.args('run', '--mode', 'pilot', '--task', 'innopolis-006', '--model-id', 'example-model')
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'MCBOTS_API_KEY'):
            launcher.validate(args)

    def test_unknown_task_or_map_rejected(self):
        for argv in [('run','--task','missing'), ('prepare-map','--map','../other')]:
            with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, 'Unknown'):
                launcher.validate(self.args('--dry-run', *argv))

    def test_missing_archive_reports_exact_required_file(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args('prepare-map', '--map', 'innopolis', '--downloads-dir', directory)
            with self.assertRaisesRegex(ValueError, 'Map archive missing:'):
                launcher.validate(args)

    def test_all_benchmark_maps_accept_release_archive_names(self):
        manifest = json.loads((ROOT / launcher.RELEASE_MANIFEST).read_text())
        with tempfile.TemporaryDirectory() as directory:
            for row in manifest['assets']:
                (Path(directory) / row['asset_name']).touch()
            for map_id in {row['map_id'] for row in launcher.task_catalog().values()}:
                with self.subTest(map_id=map_id):
                    args = self.args('prepare-map', '--map', map_id, '--downloads-dir', directory)
                    launcher.validate(args)
                    cmd = launcher.container_command(args, 'podman')
                    self.assertIn('scripts/snapshot/import-navigation-map-release.py', cmd)
                    self.assertNotIn('scripts/snapshot/prepare-navigation-snapshot.py', cmd)
            all_args = self.args('prepare-maps', '--downloads-dir', directory)
            launcher.validate(all_args)
            self.assertNotIn('--map', launcher.container_command(all_args, 'podman'))

    def test_old_original_archive_is_not_accepted_as_release_map(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'AFR-Cape.Town.zip').touch()
            args = self.args('prepare-map', '--map', 'cape-town', '--downloads-dir', directory)
            with self.assertRaisesRegex(ValueError, 'navigation-1.21.11-cape-town.zip'):
                launcher.validate(args)

    def test_all_maps_fail_early_if_one_release_archive_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args('prepare-maps', '--downloads-dir', directory)
            with self.assertRaisesRegex(ValueError, 'Map archive missing:'):
                launcher.validate(args)

    def test_paths_with_spaces_remain_single_arguments(self):
        args = self.args('--dry-run','prepare-map','--map','innopolis','--downloads-dir','/tmp/map archives')
        cmd = launcher.container_command(args, 'podman', Path('/tmp/source with spaces'))
        self.assertIn('/tmp/source with spaces:/workspace/mcbots:rw',cmd)
        self.assertIn('/tmp/map archives:/inputs:ro',cmd)
        self.assertNotIn('--download-missing', cmd)

    def test_host_proxy_forwarding_is_explicit(self):
        default = launcher.container_command(self.args('--dry-run', 'build'), 'podman')
        explicit = launcher.container_command(self.args('--forward-proxy', '--dry-run', 'build'), 'podman')
        self.assertIn('--http-proxy=false', default)
        self.assertNotIn('--http-proxy=false', explicit)

    def test_runtime_setup_keeps_live_smoke_enabled(self):
        args = self.args('--dry-run','prepare-runtime')
        cmd = launcher.container_command(args,'docker')
        self.assertNotIn('--skip-smoke',cmd)
        self.assertNotIn('--allow-unverified-runtime',cmd)

    def test_named_run_is_forwarded_and_unsafe_names_fail_early(self):
        args = self.args('--dry-run', 'run', '--task', 'innopolis-006', '--run-id', 'quickstart-001')
        launcher.validate(args)
        cmd = launcher.container_command(args, 'podman')
        self.assertEqual(cmd[cmd.index('--run-id') + 1], 'quickstart-001')
        for name in ['', '.', '..', '/tmp/run', '../run', 'a/b', 'a/']:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'single directory name'):
                launcher.validate(self.args('--dry-run', 'run', '--task', 'innopolis-006', '--run-id', name))

    def test_runtime_override_is_used_for_preparation_and_receipt_lookup(self):
        custom = ROOT / 'eval/templates/_local/custom-runtime'
        with patch.dict(os.environ, {'MCBOTS_NAV_RUNTIME_TEMPLATE_ROOT': str(custom)}):
            cmd = launcher.container_command(self.args('prepare-runtime'), 'podman')
            self.assertIn('MCBOTS_NAV_RUNTIME_TEMPLATE_ROOT=/workspace/mcbots/eval/templates/_local/custom-runtime', cmd)
            with patch.object(Path, 'is_file', autospec=True, return_value=True) as exists:
                launcher.validate(self.args('run', '--task', 'innopolis-006'))
            exists.assert_called_once_with(custom / '1.21.11/runtime-receipt.json')

    def test_runtime_outside_repository_is_rejected_before_container_start(self):
        with patch.dict(os.environ, {'MCBOTS_NAV_RUNTIME_TEMPLATE_ROOT': '/tmp/other-runtime'}):
            with self.assertRaisesRegex(ValueError, 'inside the repository'):
                launcher.container_command(self.args('prepare-runtime'), 'podman')

    def test_non_linux_native_default_is_preserved(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(runtime_defaults.platform, 'system', return_value='Darwin'):
                self.assertEqual(runtime_defaults.runtime_template_root(ROOT), ROOT / 'eval/templates/_local/navigation')
            with patch.object(runtime_defaults.platform, 'system', return_value='Linux'):
                self.assertEqual(runtime_defaults.runtime_template_root(ROOT), ROOT / 'eval/templates/_local/navigation-linux-cpu')


if __name__ == '__main__':
    unittest.main()
