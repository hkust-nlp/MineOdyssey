#!/usr/bin/env python3
"""Run Harbor inside the delegated user cgroup when rootless Podman needs it."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

NAVIGATION_VERIFIER = "eval.harbor_agents.verifier:NavigationVerifier"


def in_user_service(cgroup, uid):
    return f"/user@{uid}.service/" in cgroup


def launch_command(harbor, engine, arguments, *, podman_info="", cgroup="", uid=0):
    arguments = list(arguments)
    if not any(a in {"--verifier", "--verifier-import-path"}
               or a.startswith(("--verifier=", "--verifier-import-path=")) for a in arguments):
        arguments += ["--verifier", NAVIGATION_VERIFIER]
    command = [harbor, "run", "--env", engine, *arguments]
    if (engine == "podman" and podman_info.strip() == "true systemd v2"
            and not in_user_service(cgroup, uid)):
        # Scope mode inherits cwd/environment and propagates the child exit code.
        # It keeps exec children under the same delegated user-service subtree as
        # Podman's containers; no global cgroup or container settings are changed.
        command = ["systemd-run", "--user", "--scope", "--quiet", *command]
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument("--harbor", default="harbor", help="Harbor executable")
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="-- followed by Harbor run options")
    args = parser.parse_args()
    arguments = args.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if any(a in {"-e", "--env"} or a.startswith("--env=") for a in arguments):
        parser.error("Select the environment with --engine, not a second Harbor --env option")
    harbor = shutil.which(args.harbor)
    if harbor is None:
        parser.error("Harbor executable not found; install Harbor or pass --harbor")
    info = ""
    if args.engine == "podman":
        try:
            info = subprocess.run([
                "podman", "info", "--format",
                "{{.Host.Security.Rootless}} {{.Host.CgroupManager}} {{.Host.CgroupsVersion}}",
            ], check=True, capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            parser.error("Podman preflight failed; verify podman info before starting a trial")
    cgroup_file = Path("/proc/self/cgroup")
    cgroup = cgroup_file.read_text() if cgroup_file.exists() else ""
    command = launch_command(harbor, args.engine, arguments, podman_info=info,
                             cgroup=cgroup, uid=os.getuid())
    if command[0] == "systemd-run":
        if shutil.which("systemd-run") is None:
            parser.error("Rootless systemd Podman needs systemd-run and an active user manager")
        print("Starting Harbor in a systemd user scope for rootless Podman exec.", flush=True)
    root = str(Path(__file__).resolve().parents[2])
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [root, os.environ.get("PYTHONPATH")]))
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
