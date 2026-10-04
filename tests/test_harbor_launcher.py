import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "harbor_launcher", Path(__file__).resolve().parents[1] / "scripts/eval/run-harbor-navigation.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class HarborLauncherTests(unittest.TestCase):
    def test_login_session_enters_user_scope_and_preserves_argv(self):
        arguments = ["--path", "/tmp/tasks with spaces", "--n-concurrent", "3"]
        command = launcher.launch_command(
            "/tmp/bin/harbor", "podman", arguments, podman_info="true systemd v2\n",
            cgroup="0::/user.slice/user-1000.slice/session-42.scope\n", uid=1000)
        self.assertEqual(command, ["systemd-run", "--user", "--scope", "--quiet",
                                  "/tmp/bin/harbor", "run", "--env", "podman", *arguments,
                                  "--verifier", launcher.NAVIGATION_VERIFIER])

    def test_existing_user_service_is_not_wrapped_again(self):
        command = launcher.launch_command(
            "harbor", "podman", [], podman_info="true systemd v2",
            cgroup="0::/user.slice/user-1000.slice/user@1000.service/app.slice/run-1.scope\n", uid=1000)
        self.assertEqual(command, ["harbor", "run", "--env", "podman",
                                   "--verifier", launcher.NAVIGATION_VERIFIER])

    def test_docker_rootful_and_cgroupfs_do_not_require_systemd(self):
        for engine, info in [("docker", "true systemd v2"),
                             ("podman", "false systemd v2"),
                             ("podman", "true cgroupfs v2")]:
            with self.subTest(engine=engine, info=info):
                self.assertEqual(launcher.launch_command("harbor", engine, [], podman_info=info),
                                 ["harbor", "run", "--env", engine,
                                  "--verifier", launcher.NAVIGATION_VERIFIER])

    def test_explicit_verifier_is_preserved(self):
        for arguments in (["--verifier", "custom:Verifier"],
                          ["--verifier=custom:Verifier"],
                          ["--verifier-import-path", "custom:Verifier"],
                          ["--verifier-import-path=custom:Verifier"]):
            self.assertEqual(launcher.launch_command("harbor", "docker", arguments),
                             ["harbor", "run", "--env", "docker", *arguments])
