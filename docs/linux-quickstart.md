# Run on a Linux machine

Use the CPU container path below. No NVIDIA GPU, desktop session, cluster account,
pre-existing Minecraft installation, or server-specific filesystem layout is required.
The host needs Python 3 and a working Docker Engine or Podman installation; Java 21,
Python application dependencies, Xvfb, Mesa, screenshots and noVNC are installed in
the image. Rendering uses Mesa software rendering. Linux x86_64 is the validation
platform; ARM64 has not been verified for this release.

For GPU rendering, use the navigation fleet runner's `--gpu-devices` option and
the NVIDIA container image described in [GPU setup](navigation-eval.md#gpu-rendering).
The `navigation-linux.py` launcher used below selects CPU rendering.

Minimum host RAM and free-disk requirements have not been measured for this release.
Disk use includes container images, map archives, prepared snapshots, and per-trial
world copies and logs. Container resource limits describe configuration, not measured
usage or minimum host requirements.

The container engine must work for your current account. The launcher prints the
exact command it executes. If both engines exist it chooses Docker; select one
explicitly with `--engine podman` or `--engine docker` before the subcommand.

## 1. Build and check

Run from the extracted source directory:

```bash
python3 scripts/launch/navigation-linux.py build
python3 scripts/launch/navigation-linux.py doctor
```

The build needs internet access to Ubuntu, PyPI and GitHub. The doctor actually
checks Java, software OpenGL, an 800×600 screenshot and bubblewrap isolation. It
does not call a model or require a map. It must report `"status": "ok"`.

The launcher grants namespace/mount capability inside the outer container for the
inner command sandbox. It does not use a privileged container, host networking or
host GPU passthrough. Docker additionally disables its default AppArmor profile for
this container; a host that disallows namespace creation needs an administrator to
permit the sandbox primitives. Podman does not inherit host proxy variables: a proxy
bound to host loopback cannot be used at container loopback. If your network requires
a proxy, set its container-reachable address in your host proxy environment and
use `--forward-proxy` before the subcommand for Podman. Docker follows its own
engine proxy configuration.

## 2. Prepare the Minecraft runtime

```bash
python3 scripts/launch/navigation-linux.py prepare-runtime
```

This downloads the pinned Minecraft/NeoForge/mod artifacts, checks their hashes,
then launches a temporary server and CPU-rendered client to verify AgentBridge.
It does not mark an untested runtime as ready. The server preparation writes
`eula=true`; use it only if you accept Minecraft's EULA. Generated runtime data stays
under `eval/templates/_local/navigation-linux-cpu/` in your extracted directory.
Existing templates are not overwritten by preparation; subsequent task runs reuse
the verified template. Do not share this directory across CPU architectures.

## 3. Supply and prepare a map

```bash
python3 scripts/launch/navigation-linux.py tasks
python3 scripts/launch/navigation-linux.py prepare-map --map innopolis --downloads-dir /path/to/maps
```

For this example `/path/to/maps` must contain `navigation-1.21.11-innopolis.zip` with
the exact bytes/hash declared in `eval/navigation/maps/innopolis/map.json`. Each map
manifest supplies its own archive name; some use nested archive paths. This command
verifies and prepares only the selected map, not all 30 maps. Original archives are
mounted read-only. Prepared snapshots live under `eval/snapshots/_cache/navigation/`.

**The anonymous code archive does not contain the maps, and the anonymous map mirror
is not provisioned yet.** Obtain the approved original archives separately. Renaming
an arbitrary Minecraft world does not satisfy the fingerprint checks. Missing archives
produce a clear error before a container is launched. No original author-linked
repository or GitHub login is required by this local-file workflow.

## 4. Review without paying for model calls

```bash
python3 scripts/launch/navigation-linux.py run --task innopolis-006 --vnc
```

Open `http://127.0.0.1:6080/vnc.html` after the client starts. Review mode starts no
model. Stop the command with Ctrl+C when finished. For a remote machine, forward the
port with SSH; it is deliberately bound to loopback. Use `--vnc-port 6088` if needed.
No Minecraft, RCON, AgentBridge or Remote Bash port is published to the host.

## 5. Run a model

```bash
export MCBOTS_BASE_URL='https://YOUR-PROVIDER/v1'
read -rs -p 'API key: ' MCBOTS_API_KEY
export MCBOTS_API_KEY
python3 scripts/launch/navigation-linux.py run --mode pilot --task innopolis-006 \
  --model-id YOUR-MODEL --action-protocol tool_calls --vnc
unset MCBOTS_API_KEY
```

Use `--action-protocol xml` for providers requiring the original text action format.
Use `--api-protocol responses` only for providers implementing the Responses API.
The launcher takes credentials from the environment, not the historical model config
files. Credentials are not printed or placed in command-line arguments. Pilot/formal
mode requires an explicit model ID and key. Review mode requires neither.

Results and journals are under `eval/results/navigation/`; task runtime copies are
under `eval/runtime/navigation/`. Docker may create root-owned output files; rootless
Podman maps container-root output to the invoking user. The baseline task catalog,
completion rule and evaluation limits are unchanged by this launcher.

For command inspection without containers, credentials or downloads:

```bash
python3 scripts/launch/navigation-linux.py --engine docker --dry-run run --task innopolis-006 --vnc
```
