"""Narrow compatibility for Harbor 0.23's Compose machine-to-machine execs."""
from harbor.environments.docker.docker import DockerEnvironment


def disable_exec_tty(environment) -> bool:
    """Disable PTYs only on this trial's Docker/Podman Compose exec commands.

    Harbor 0.23 has no public tty option on exec/service_exec. Its Compose call
    omits -T, so podman-compose requests a terminal even though stdin is DEVNULL.
    Concurrent RPCs can then fail in crun before nav starts. Preserve Harbor's
    command construction, service selection, environment and timeout handling.
    This instance-scoped adapter also covers trusted artifact collection; no
    installed Harbor code or other trial instance is modified.
    """
    if not isinstance(environment, DockerEnvironment):
        return False
    if getattr(environment, "_navigation_exec_without_tty", False):
        return True
    original = environment._run_docker_compose_command

    async def run(command, *args, **kwargs):
        command = list(command)
        if command and command[0] == "exec" and "-T" not in command[1:]:
            command.insert(1, "-T")
        return await original(command, *args, **kwargs)

    environment._run_docker_compose_command = run
    environment._navigation_exec_without_tty = True
    return True
