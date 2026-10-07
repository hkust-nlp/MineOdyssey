# Innopolis 006 — Harbor navigation

Original Russian navigation task with the corrected sports-center waypoint.
Harbor 0.23.0 format, Linux CPU rendering, and evaluator-owned completion rules.
Use the dedicated original-Agent adapter for the historical formal GLM policy.
Generic Harbor agents use the CLI in instruction.md and own their control loops.

## Build and run

Install Docker Engine + Compose (or Podman + podman-compose) and Harbor 0.23.0.
Export adds environment/world/source.tar.gz and environment/agent-source.tar.gz.
The [Compose template](environment/docker-compose.yaml) sets `cpus: 4` and
`mem_limit: 12g` for `world`; [task.toml](task.toml) configures `main` and the
separate verifier. These are container resource settings. Minimum host RAM and
free-disk requirements have not been measured for this release.

```bash
mkdir -p /tmp/navigation-source
tar -xzf environment/world/source.tar.gz -C /tmp/navigation-source
docker build -f /tmp/navigation-source/containers/Containerfile.navigation-cpu \
  -t localhost/anonymous-navigation:linux-cpu /tmp/navigation-source
cp /path/to/navigation-1.21.11-innopolis.zip environment/world/assets/
```

The map asset SHA-256 is
`d27480af8b288979d6e695d733a01753784c50ee2a9c474f838d8e95a1c652b8`.
Download the map ZIP from the
[MineOdyssey map release](https://github.com/hkust-nlp/MineOdyssey/releases/tag/navigation-maps-1.21.11-v1).
Generated runtime caches remain local. Runtime preparation writes eula=true;
accept the Minecraft EULA before preparing/running the runtime. The build verifies
the map and downloads pinned dependencies. Cached runtimes must match the current
profile including the rebuilt AgentBridge JAR; stale runtime receipts are rejected.

Install Harbor on the host and select the model with MCBOTS_HARBOR_API_MODELS_FILE
and MCBOTS_HARBOR_MODEL_KEY, then use
`eval.harbor_agents.original:OriginalNavigationAgent`. This launches the unchanged
Agent inside `main` with `/opt/mcbots-venv/bin/python`, whose dependencies are pinned
by uv.lock. No separate host Agent interpreter is needed. A private 0600 model
configuration is uploaded to main and deleted after reading; credentials are not
placed in the image, command arguments, logs or world. The main image contains the
original Agent and its bridge, without evaluator source or map/task answers.
Use scripts/eval/run-harbor-navigation.py --engine docker -- or --engine podman --
followed by the Harbor run options. The launcher selects the navigation verifier;
for rootless Podman it also supplies the delegated systemd scope.
Direct Harbor commands need --verifier eval.harbor_agents.verifier:NavigationVerifier
to report infrastructure reasons instead of a generic missing-reward error.
PODMAN_COMPOSE_PROVIDER=/bin/false selects direct podman-compose when Harbor 0.23
misdetects version 1.6. See docs/harbor-pilot.md in the full source for commands.

## Current policy

The native system prompt and function schema match actual historical formal GLM
runs. Task-language text and English system guidance are preserved. The original
Agent provides asynchronous exec, interruption, observations, summaries and retry
handling. Limits are 500 successful decisions, a 21600-second infrastructure
watchdog, and a 22800-second outer Harbor timeout. Summaries use the original
100-image, 100-turn and 200000-token thresholds. SDK retries are zero; the outer
Agent retries transient failures and stops after three consecutive failures.

The coordinate-filter-v2 game guard hides Xaero map coordinates, blocks waypoint
edit/settings screens and filters shared waypoint-coordinate chat. Startup checks
its actual enabled flag and version. The map and original player-state API remain
available. Arrival checks remain 3.5 blocks in 3D and 1.5 vertically with one-second
sampling; model guidance remains 2.5 blocks, 1.5 vertically and three seconds.
Ordered visits and three claim attempts are unchanged. Reward 1 requires an
accepted claim. Infrastructure failures are unscored.

## Isolation and evidence

World has no external network; main has a read-only Unix-socket mount. World has
SYS_ADMIN/NET_ADMIN for nested Bubblewrap, while action processes drop capabilities.
The sandbox hides evaluator/source files, has no external network, and preserves
its own /tmp. nav read-image permits bounded PNG/JPEG reads inside that sandbox.
OriginalNavigationAgent retains its original tool set; this CLI is for generic agents.
Generic nav exec defaults to 30 seconds and accepts at most 120 seconds, matching
the original synchronous Remote Bash endpoint; larger values are rejected.
The original Agent's asynchronous actions retain their 300-second timeout.

Harbor stops main and collects evidence from world before running a fresh verifier.
Agent-written completion/reward files are never trusted. Host-only model metadata
records the actual model without credentials; unreported generic models remain
unspecified. Receipts bind prompt, settings, dependencies, source and game readback.
Raw runs, screenshots, credentials, maps and runtime archives remain private.

See validation.json for current tests and live results. Earlier XML/default runs
are retained as diagnostic evidence and are not reclassified as formal-profile
runs. The source includes scripted retry/failure, coordinate-lock and isolation
smokes; these do not establish model navigation success. Docker and uncached
Harbor builds have not yet been tested on this host.

Historical host-Agent validation: 176 Python and 6 Java tests passed. The native real-world smoke
completed 12 requests (one injected 500, eleven successful replies), blocked two
waypoint-screen attempts, and produced the expected trusted reward 0 with no trial
exception. Three GLM reruns have matched effective settings and actual prompt
hashes. Hofburg 003 ended out of bounds (reward 0), Innopolis 008 ended on death
(reward 0), and Würzburg 007 reached the six-hour watchdog (unscored).
These completed trials used the original Agent through Harbor; none was a
successful navigation. See validation.json for the separate adapter checks.
The subsequent boundary fix passed 68 Python checks, replayed the three trusted
completions and passed a real Podman/Minecraft smoke with a scripted failing
provider. It verifies the 120-second synchronous CLI contract and explicit
unscored infrastructure reasons without changing the original Agent or evaluator.

The 2026-10-03 deployment moves the original Agent into main. Its model loop and
all shared runtime/evaluator files remain unchanged. Container-local operations
use the Unix socket directly. See validation.json for the new scripted container
checks; the three historical GLM trials above are not fresh container-Agent runs.
