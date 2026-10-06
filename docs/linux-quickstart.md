# Run on a Linux machine

Use the CPU container path below. No NVIDIA GPU, desktop session, cluster account,
pre-existing Minecraft installation, or server-specific filesystem layout is required.
The host needs Python 3 and a working Docker Engine or Podman installation; Java 21,
Python application dependencies, Xvfb, Mesa, screenshots and noVNC are installed in
the image. Rendering uses Mesa software rendering. Linux x86_64 is the validation
platform; ARM64 has not been verified for this release.

For GPU rendering, use the navigation fleet runner's `--gpu-devices` option and
the NVIDIA container image described in [GPU setup](navigation-eval.md#gpu-rendering).
That path uses Podman with NVIDIA CDI and requires a host NVIDIA driver and
Container Toolkit. The `navigation-linux.py` launcher used below explicitly selects
CPU rendering, including on hosts with a GPU; it does not automatically switch to
GPU rendering. The current Harbor task template also selects CPU rendering.

Xvfb provides the CPU container's virtual display, so the host does not need an
open desktop session or a connected monitor. Rendering generates Minecraft images;
the agent separately calls the configured model API. Hardware requirements for
self-hosting a model depend on that model's deployment.

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
Single-task runs and the CPU batch runner use this same path and the image
`localhost/anonymous-navigation:linux-cpu`; no second preparation or image build
is needed when moving from the quickstart to a batch. To reuse an older custom
template, set `MCBOTS_NAV_RUNTIME_TEMPLATE_ROOT` to its absolute host path inside
the repository before preparation or execution. The fleet also accepts
`--runtime-template-root`. Keep using the same container engine.
Existing templates are not overwritten by preparation; subsequent task runs reuse
the verified template. Do not share this directory across CPU architectures.

## 3. Download and prepare maps

The [map release](https://github.com/mine-odyssey/MineOdyssey/releases/tag/navigation-maps-1.21.11-v1)
contains the 30 benchmark worlds as Minecraft 1.21.11 ZIPs. Install
[GitHub CLI (`gh`)](https://cli.github.com/) for the downloader. If the repository
is private, authenticate with `gh auth login` using an account with access.

```bash
python3 scripts/launch/navigation-linux.py tasks
python3 scripts/snapshot/download-navigation-map-release.py --map innopolis
python3 scripts/launch/navigation-linux.py prepare-map --map innopolis
```

Downloads go to `downloads/navigation-maps-1.21.11-v1/`. You can instead download
ZIPs manually from the release and pass `--downloads-dir /path/to/maps` to the
preparation command. Archive names follow `navigation-1.21.11-<map-id>.zip`.
The [release manifest](../eval/navigation/releases/navigation-maps-1.21.11-v1.json)
is the download/import contract; per-map `source` records also retain older
archive provenance and are not the download list for this workflow.

To download and import all 30 maps (2.60 GB of archives):

```bash
python3 scripts/snapshot/download-navigation-map-release.py
python3 scripts/launch/navigation-linux.py prepare-maps
```

Preparation mounts archives read-only, validates size, SHA-256, ZIP integrity and
world layout, and verifies the extracted world's fingerprint and Minecraft version.
It imports the ready-to-run worlds without rebuilding or upgrading them. Prepared
snapshots live under `eval/snapshots/_cache/navigation/`; every task uses a separate
world copy. Valid archives and snapshots are reverified and reused on subsequent
runs. Download/import do not call a model. Missing archives fail before launching
a container and report the required filename and download command.

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
python3 scripts/launch/navigation-linux.py run --mode formal --run-id quickstart-001 \
  --task innopolis-006 \
  --model-id YOUR-MODEL --action-protocol tool_calls --vnc
unset MCBOTS_API_KEY
```

Use `--action-protocol xml` for providers requiring the original text action format.
Use `--api-protocol responses` only for providers implementing the Responses API.
The launcher takes credentials from the environment, not the historical model config
files. Credentials are not printed or placed in command-line arguments. Pilot/formal
mode requires an explicit model ID and key. Review mode requires neither.

Use `--mode pilot --run-id pilot-001` for an API integration check without formal
scoring. Formal runs require the smoke-verified runtime from step 2; pilot and
review runs are excluded from formal aggregation. Choose a new run ID when
repeating a single task because existing task results are not overwritten.

## 6. Find and aggregate results

The command above writes results and journals under
`eval/results/navigation/quickstart-001/innopolis-006/`. Read `completion.json` for
`success`, `terminal_reason`, and `infrastructure_error`; infrastructure errors
are not scored task failures. Task runtime copies are under
`eval/runtime/navigation/`. Docker may create root-owned output files; rootless
Podman maps container-root output to the invoking user. The baseline task catalog,
completion rule and evaluation limits are unchanged by this launcher.

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/)
on the host for analysis and batch scheduling:

```bash
uv sync --frozen
uv run --frozen python scripts/eval/aggregate-navigation-results.py --run-id quickstart-001
```

The summary is written to `eval/results/navigation/quickstart-001/aggregate.json`.
Only add `--require-all` for a full 180-task run. For batch execution, first import
all 30 maps in step 3, then follow [the full-benchmark example](../README.md#6-run-all-180-tasks).
The batch runner reuses the quickstart image and runtime; the host needs neither
Java nor a separate Minecraft installation. Its defaults limit each task to
4 CPUs and 8 GiB RAM, with one task active at a time.

For command inspection without containers, credentials or downloads:

```bash
python3 scripts/launch/navigation-linux.py --engine docker --dry-run run --task innopolis-006 --vnc
```
