> Historical upstream guide: some inventory and arrival descriptions are stale.
> Use `eval/navigation/tasks.json` and `settings/final-navigation-v1.json` as authoritative.
> See [anonymous release notes](anonymous-release.md) for this package and asset limitations.

# Finalpool Navigation Evaluation

This document describes the fingerprint-bound Minecraft 1.21.11 navigation
benchmark on branch `finalpool-v2`. The benchmark contains 194 owner-approved
tasks across 30 maps. Membership in the tracked benchmark catalog is the task
admission decision; static references and manual validation receipts are not
required for formal runs.

## Invariants

- `downloads/HK-Shun.Lee.zip` and `downloads/ALPS-Zurich.zip` are the only map
  sources. ZIPs, extracted worlds, runtime templates, logs, recordings, and
  results stay outside Git.
- Both source worlds must report `Version.Name=1.21.11` and
  `DataVersion=4671`.
- The reviewed task and waypoint annotation source is pinned to
  `codex/nav-completion-gate` commit
  `0000000000000000000000000000000000000000`. Annotation provenance is
  independent of the map source: only the annotations are migrated from that
  branch, never its converted 1.21.1 worlds or generated full reference paths.
- Each map has two immutable, read-only layers. The source snapshot is an exact
  archive extraction. The prepared snapshot is a clone in which the locked
  legacy `datapacks/heights` directory is replaced once by the complete,
  hash-manifested 1.21.11 pack; no other world file may differ.
- Every task receives a reflink or ordinary copy of the prepared snapshot;
  hardlinks are rejected. Runtime copies remove only `session.lock`,
  `playerdata/`, `stats/`, and `advancements/`. They never rebuild or patch the
  data pack. Both immutable layers are fingerprinted before and after a run.
- Before a task starts, its start chunk is temporarily force-loaded and checked
  server-side. After teleport, AgentBridge must report the live player on the
  ground, healthy, stable for three consecutive samples, and still near the
  expected start position. A 30-second readiness timeout is an infrastructure
  error and the monitor and model are not started. Any temporary force-load is
  then removed; pre-existing world force-loads are preserved.
- The client mod inventory is exactly AgentBridge, BoccHUD, MaFgLib,
  LambDynamicLights, Xaero Minimap, and Xaero World Map. The pinned BoccHUD
  configuration exposes the compact coordinate, facing, rotation, and
  looked-at-block HUD used on the main branch. LambDynamicLights supplies
  client-only held-item lighting without modifying the world. Baritone is
  forbidden in the formal profile.
- The evaluator samples AgentBridge state independently. A model can only send
  `claim_done`; it cannot write a successful evaluator result.
- A waypoint is detected when one evaluator sample is strictly within 3.5
  blocks in 3D distance and within 1.5 blocks vertically. Required waypoints
  and the final destination are recorded in task order; `claim_done` completes
  the task only after the full route is recorded.

## Tracked Assets

The tracked benchmark lives under `eval/navigation/`:

- `maps/*/map.json` pins release asset metadata, archive SHA-256, version, the
  exact source fingerprint, the exact prepared fingerprint, the three-file
  legacy pack input lock, and the replacement pack manifest.
- `compatibility/heights-1.21.11/` is a complete Minecraft 1.21.11 data pack.
  Its `pack.mcmeta` targets data-pack format 94.1 exactly, and
  `data/minecraft/dimension_type/overworld.json` preserves
  `min_y=-64`, `height=512`, and `logical_height=512`.
  `compatibility/heights-1.21.11.files.sha256` locks every file in the pack.
  The tracked source directory is installed as `datapacks/heights`, retaining
  the pack identity already enabled by the original maps.
- `maps/*/waypoints.json` and `maps/*/routes.json` hold normalized map-local
  annotations. `tasks.json` at the navigation root is the single schema-v2 task
  catalog. Its first waypoint is the start, its last waypoint is the target,
  and every waypoint in between is an ordered required checkpoint. The loader
  checks every task against its source route and map waypoint catalog before
  exposing it to the runner.
- `references.json`, when present, is optional analysis metadata containing a
  static reachability result, reference length, route digest, and planner
  details. A reachable reference enables static SPL but is not an admission
  requirement. Full paths remain in the ignored snapshot cache.
- Legacy `validations/<task-id>.json` receipts may be retained as review
  evidence, but are not read by the formal admission or aggregation path.
- `profiles/minecraft-1.21.11.json` pins Minecraft, NeoForge, AgentBridge,
  BoccHUD, MaFgLib, LambDynamicLights, both Xaero artifacts, and the BoccHUD
  configuration by SHA-256.
- `settings/final-navigation-v1.json` is the single source of truth for runtime,
  completion, and agent context rules. Each task allows 500
  successful decision requests and has a 21600-second infrastructure watchdog.
  Auto-summary triggers after 100 assistant turns,
  200,000 total tokens, or 100 active images, whichever happens first; the
  legacy message-count trigger is disabled. LLM requests use a 600-second
  timeout with no SDK retry; three consecutive failed requests terminate the
  run as an infrastructure failure. The setting also pins the per-task
  `eval_setting` defaults. Bash actions have a 300-second limit; timeout is
  returned to the model with exit code 124 so execution can continue.

Each task may override these `eval_setting` fields; an empty object uses every
default:

```json
{
  "time": "noon",
  "weather": "clear",
  "six_view_enabled": false,
  "player_scale": 1.0,
  "third_person": false,
  "hud_enabled": false,
  "navigation_hints_enabled": false,
  "guideline": false,
  "resource_pack_and_shader": false
}
```

`time` accepts `noon` or `midnight`; `weather` accepts `clear` or `rain`.
Six-view capture, player scale, first/third-person perspective, eval HUD visibility,
and navigation-hint inclusion are applied before the first model observation.
`hud_enabled` controls only the Xaero minimap and BoccHUD coordinate/orientation
overlay. The vanilla HUD remains visible, the world map remains available, and
first-person waypoint markers always remain enabled. They use a 32-block
distance limit when `hud_enabled` is false and unlimited distance when it is
true. When `guideline` is true, task materialization activates the pinned Ground
Navigation and Baritone planner artifacts for that task only. After start
readiness, the evaluator sends the ordered checkpoint/target route and requires
an acknowledgement that guide-only mode is active and Baritone's agent-facing
controls are locked before the model starts. Baritone calculates walkable paths
but never supplies movement input; the model sees only the rendered ground
guide. `resource_pack_and_shader` remains a reserved boolean field with no
runtime behavior yet.

The current catalog contains 30 maps and 194 tasks. Every public intermediate
annotation is an ordered required waypoint. The evaluator only checks the next
required waypoint, then arms final arrival after all intermediates are recorded.
This also supports loop routes whose start and target waypoint IDs are equal.

### Navigation-v1 candidate annotations

The reviewed route portfolio is exposed through the shared 194-task benchmark.
Each included map has a tracked `map.json`, normalized `waypoints.json`, and
`routes.json`. The route loader
validates annotation provenance, route-point roles, segment order, and every
waypoint reference. The Chain Bridge catalog also retains the route-embedded
East Bridge Head point that was absent from the standalone upstream waypoint
list.

Quality states are preserved as route provenance rather than used as formal
admission gates: NYC, Hofburg, Plaza de Mayo,
Santa Lucía, and Ueno Park contain verified route suites. Ohrid has 13 normally
reached routes, one route reached with a normal door interaction, and one
conditional route whose final door still requires manual interaction.
Copacabana and Chain Bridge retain the stated strict-retest notes; Miljacka
Riverside has two of three routes verified; and Sviyazhsk validation remains in
progress. Full Pathfinder reachability dumps stay outside Git. Regardless of
those provenance labels, all 194 tasks in the benchmark catalog are explicitly
owner-approved formal tasks.

Würzburg is a separate manual in-game labeling package. Its tracked raw Xaero
export is the annotation source for 75 enabled normalized waypoints; one
disabled duplicate Oberbank marker is retained only in the exclusion metadata.
It currently has no candidate routes or formal tasks.

Mr Beast 1000$ Harbor City Shipyard is another manual in-game labeling
package. Its tracked raw Xaero export is the annotation source for 53 enabled
normalized waypoints covering dry docks, piers, workshops, warehouses, utility
buildings, and offices. It currently has no candidate routes or formal tasks.

## Prepare Source and Prepared Snapshots

Place the original ZIPs in `downloads/`, then run the single preparation and
preflight entrypoint:

```bash
uv run python scripts/snapshot/prepare-navigation-snapshot.py
```

With no `--map` arguments, the script reads every map from
`finalpool-navigation-v1`. Valid caches are reused; missing, incomplete, or
stale caches are rebuilt from their validated ZIPs. Preparation continues after
individual failures, then a full preflight verifies every selected source and
prepared snapshot. The command exits successfully only when every selected map
is ready. Use `--replace` to intentionally rebuild every selected cache.

Use `--download-missing` to fetch a missing private-release asset through an
authenticated `gh` session. Run only the full preflight without extracting:

```bash
uv run python scripts/snapshot/prepare-navigation-snapshot.py --verify-only
```

The ignored cache has both immutable layers:

```text
eval/snapshots/_cache/navigation/1.21.11/<map-id>/
├── world/                         # exact source extraction
├── source-receipt.json
├── world-files.sha256
├── fingerprint.json
└── prepared/
    ├── world/                     # source + locked heights pack replacement
    ├── prepared-receipt.json
    ├── world-files.sha256
    └── fingerprint.json
```

Preparation validates the ZIP and source layer first, then creates the prepared
layer. `--verify-only` hashes and verifies both layers without repairing them.
Use `--replace` only when intentionally rebuilding the two layers from the
original ZIP.

### Indoor candidate maps

The fourteen indoor candidates use the same map-package and snapshot interface.
All indoor and outdoor source archives are flat peers in `downloads/`; map type
is metadata (`environment` in `map.json`), not a filesystem hierarchy. Their
tracked metadata lives under `eval/navigation/maps/`.
The source worlds range from Minecraft 1.12.2 through 1.21.1, so each
`map.json` records both the original `source_version` and the execution version.

Prepare any candidate exactly like an outdoor map:

```bash
uv run python scripts/snapshot/prepare-navigation-snapshot.py --map alcatraz
uv run python scripts/snapshot/prepare-navigation-snapshot.py --map alcatraz --verify-only
```

Preparation clones the exact source extraction, launches the pinned Minecraft
1.21.11 / NeoForge 21.11.44 server once with `--forceUpgrade --eraseCache`,
waits for a complete server start, saves and stops it cleanly, normalizes only
wall-clock metadata, and freezes the result as the read-only prepared snapshot.
The source snapshot is never opened by Minecraft. Each prepared receipt binds
the source fingerprint, target profile, server arguments, upgrade log, and
result fingerprint. An Anvil file manifest still detects byte-level cache
drift, while the cross-machine binding fingerprint treats semantically
identical `level.dat` compound serialization as identical.

Eight indoor candidates also contain labeled `waypoints.json`: Buckingham
Palace, Hagia Sophia, Notre Dame, Reichstag, RMS Titanic, SoFi Stadium,
Versailles, and the White House. Their 234 waypoints are pinned to the exact
`lh-inter-maps` commit and source-file digest. They do not yet contain routes or
tasks. The other six indoor candidates still contain only `map.json`. Adding
any indoor map to a
formal benchmark requires
task definitions, references, and manual validation receipts. A successful
version upgrade proves that the world loads; it does not replace a visual review
for missing modded blocks or changed materials.

## Prepare the 1.21.11 Runtime

On a Linux host with Java 21 and Xvfb:

```bash
uv run python scripts/eval/prepare-navigation-runtime.py --replace
```

This verifies and installs the tracked, SHA-pinned AgentBridge and Ground
Navigation JARs, installs NeoForge 21.11.44, creates isolated server/client
templates, verifies the exact mod inventory, and runs health, state, input
pulse, look, close-GUI, and right-click smoke checks. A clean checkout does not
need Gradle unless either in-repository mod is being changed and rebuilt.
Formal runs accept only a `smoke_verified` runtime receipt.

### Host-native Mac review fleet

For visual map triage outside the formal benchmark, the Mac can run all 43
downloaded candidate worlds as independent localhost servers. The preparation
step securely extracts each ZIP once, verifies Minecraft 1.21.11 / DataVersion
4671, replaces recognized legacy height packs in the runtime copy, and records
the original reviewer position as the server spawn. It does not modify the
source ZIPs or the two immutable formal snapshots.

Each local server instance also records the exact source ZIP size and SHA-256.
Formal task clients load only the current task's ordered start, intermediate,
and target waypoints. A loop route whose start and target are the same location
uses one marker for that shared point. These waypoints render in first-person
view only within 32 blocks, while remaining visible on both the minimap and
world map. Xaero's waypoint menu remains available, but exact waypoint
coordinates are hidden.
An absent or stale instance receipt causes that map alone to be rebuilt before
launch. `--exclude-map <map-id>` may be repeated to create a smaller active
fleet; the selection is saved in `fleet.json` and reused by later commands.
Pass `--all-maps` to explicitly return to the complete release inventory.

```bash
uv run python scripts/eval/run-navigation-review-fleet.py prepare
uv run python scripts/eval/run-navigation-review-fleet.py install-client
uv run python scripts/eval/run-navigation-review-fleet.py start
uv run python scripts/eval/run-navigation-review-fleet.py status
```

`install-client` adds an isolated official Launcher profile named
`MCBots Review 1.21.11 (<N> maps)`, installs the pinned client mods, and writes
the active fleet entries to that profile's Multiplayer list. Each entry uses a unique
`<map-slug>.localhost` hostname so Xaero never merges caches merely because the
servers share `127.0.0.1`; the original 42 maps retain ports 25565 through
25606 in release-manifest order, and Factory Collection uses port 25607. The
profile tells Java to prefer IPv4 because these local servers bind to
`127.0.0.1`, while `.localhost` can otherwise resolve to IPv6 `::1` first. Stop
all servers cleanly when review is finished:

The Multiplayer entry name reports `Waypoint:YES/NO` and `Route:YES/NO` from
the current normalized map package. `Route:YES` accepts either candidate
`routes.json` or formal `tasks.json`. During client installation, every current
`waypoints.json` catalog is rendered into the corresponding host-specific
Xaero Minimap directory; maps without waypoints have stale generated waypoint
files removed. Xaero World Map displays the same Minimap waypoint catalog.

```bash
uv run python scripts/eval/run-navigation-review-fleet.py stop
```

The ignored runtime worlds live under
`eval/runtime/navigation-review-fleet/`; subsequent starts do not unzip them
again. This fleet is a convenience for candidate-map inspection and is not a
substitute for task-level `review` receipts.

### Unified downloadable 1.21.11 maps

The ready-to-run distribution is tracked by
`eval/navigation/releases/navigation-maps-1.21.11-v1.json`. It contains 43
assets: 28 outdoor maps, 14 indoor maps, and one mixed factory/railway map.
Every asset uses the filename
`navigation-1.21.11-<map-id>.zip`, contains a single `<map-id>/` world root,
and is bound to its archive SHA-256 and uncompressed world fingerprint. Every
embedded `level.dat` is verified as Minecraft 1.21.11 / DataVersion 4671.

This release is a distribution layer, not a replacement for provenance. The
original release asset or Git LFS source and its SHA remain recorded alongside
each release entry. Indoor maps are packaged from their immutable force-upgraded
prepared snapshots. Outdoor maps either use their verified prepared snapshot or
a clean source clone with the locked 1.21.11 heights data pack installed.

Download all maps, or only selected map IDs, with:

```bash
uv run python scripts/snapshot/download-navigation-map-release.py
uv run python scripts/snapshot/download-navigation-map-release.py \
  --map shun-lee --map buckingham-palace
```

Downloads go to `downloads/navigation-maps-1.21.11-v1/`. Existing assets are
fully revalidated before reuse; a bad size, SHA-256, ZIP CRC, path layout, or
world root fails closed.

Import the ready-to-run release worlds used by the formal benchmark with:

```bash
uv run python scripts/snapshot/import-navigation-map-release.py
uv run python scripts/snapshot/import-navigation-map-release.py --verify-only
```

The importer selects the 30 maps in `finalpool-navigation-v1` by default. It
does not rebuild, upgrade, or otherwise mutate a world. Each release ZIP is
validated against the tracked release manifest, extracted into the ignored
snapshot cache, fingerprinted again, and frozen read-only. A per-map
`release-receipt.json` binds the extracted world to the release manifest,
archive digest, original source fingerprint, and tracked preparation digest.
`verify_snapshot()` recognizes this release cache directly, while retaining
the legacy source/prepared verifier for caches built from original archives.

Release imports contain only the execution-ready prepared layer:

```text
eval/snapshots/_cache/navigation/1.21.11/<map-id>/prepared/
├── world/
├── release-receipt.json
├── world-files.sha256
└── fingerprint.json
```

Use `--replace` only to intentionally re-extract already imported release
worlds. Normal reruns verify and reuse a valid cache.

Formal navigation tasks use a stronger boundary: every task clones a separate
client directory and deletes the complete `game/xaero` tree before writing its
single-task waypoint profile. World-map tiles and waypoints therefore cannot
carry across tasks even though formal task servers use ephemeral localhost
ports.

### Apple Silicon Mac container

On an Apple Silicon Mac running macOS 26, use the dedicated CPU image instead
of the NVIDIA/Podman images:

```bash
./scripts/containers/navigation-cpu-macos.sh install-runtime
./scripts/containers/navigation-cpu-macos.sh build
./scripts/containers/navigation-cpu-macos.sh start
./scripts/containers/navigation-cpu-macos.sh prepare-runtime
```

`install-runtime` downloads the pinned signed Apple `container` 1.2.0 package,
checks its SHA-256, and opens the system installer. The installer is the only
step that requires an administrator-password confirmation.

The image is native `linux/arm64`, uses Java 21, Xvfb, Mesa software rendering,
noVNC, and a source-built pinned `mcrcon`. The repository is bind-mounted at
`/workspace/mcbots`, while the Linux client/server template is isolated under
`eval/templates/_local/navigation-linux-arm64/`. It never reuses a template
prepared directly on macOS. PortableMC is forced to resolve the pinned LWJGL
3.3.3 Linux ARM64 natives instead of Mojang's x86-only default classifier.

After `prepare-runtime` passes its full smoke, start a manual review with:

```bash
./scripts/containers/navigation-cpu-macos.sh review slr-n01
```

Open
`http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale` while the task is
running. Screenshots still come from the Linux X display (`xwd`/ImageMagick);
macOS `screencapture` is not required.

Useful lifecycle commands are `status`, `shell`, `stop`, and `delete`. Runtime
CPU, memory, name, image, platform, and noVNC port can be overridden with the
`NAV_CONTAINER_*` variables printed by `--help`.

`--skip-smoke` is useful for preparing assets on a host that cannot launch a
client, but produces only a `prepared` receipt. Such a template is limited to
review/pilot materialization with `--allow-unverified-runtime`; it is never
formal-eligible.

The Minecraft 1.21.11 AgentBridge build can also be checked independently:

```bash
cd src/agentbridge
./gradlew -PagentbridgeProfile=1.21.11 clean build
```

## Rebuild Static References

```bash
uv run python scripts/eval/build-navigation-references.py
```

The planner reads the prepared snapshot's Anvil data without launching
Minecraft. The preparation changes only the data pack, so region data remains
byte-identical to the source snapshot. The planner models conservative walking,
steps, short drops, wooden doors, ladders, and vines. Full route cells are
written only to the ignored snapshot cache. A `planner_limit` is not an
`unreachable` verdict and must not be relabeled.

Reference generation is optional and can be run for any subset of the current
catalog when static path-length analysis or SPL is desired.

## Optional Manual Review

Launch one task with no model:

```bash
uv run python scripts/eval/run-navigation-benchmark.py \
  --mode review \
  --task slr-n01 \
  --vnc
```

Walk the full task in Adventure mode, check the prompt, start pose, target
marker, endpoint, route, and required waypoints, then stop the review runner.
Legacy receipts can still be recorded for audit notes, but they do not control
formal eligibility:

```bash
uv run python scripts/eval/record-navigation-validation.py \
  --task slr-n01 \
  --reviewer lockon \
  --status verified \
  --evidence "manual adventure walkthrough"
```

Use `--replace` only when intentionally superseding an existing optional review
receipt.

## Pilot and Formal Runs

A pilot is always marked non-formal:

```bash
uv run python scripts/eval/run-navigation-benchmark.py \
  --mode pilot \
  --task slr-n01 \
  --model-id example/model
```

A formal task must belong to the owner-approved 194-task benchmark and requires
a smoke-verified runtime:

```bash
uv run python scripts/eval/run-navigation-benchmark.py \
  --mode formal \
  --task slr-n01 \
  --model-id example/model \
  --model-parameters-json '{"temperature":0}'
```

Run the complete 194-task benchmark with:

```bash
uv run python scripts/eval/run-navigation-benchmark.py \
  --mode formal \
  --all \
  --run-id final-model-a \
  --model-id example/model \
  --model-parameters-json '{"temperature":0}'
```

`formal --all` selects every task in the benchmark catalog. It does not require
static references or manual validation receipts and never silently skips tasks.

### Parallel container fleet

On a Linux evaluation host, run one isolated OCI container per active task with
the fleet entrypoint. The defaults allocate 4 CPUs, 8 GiB of memory, and 2 GiB
of shared memory to each task container:

```bash
podman build \
  --file containers/Containerfile.navigation-cpu \
  --tag mcbots-navigation:1.21.11 \
  .
```

```bash
export MCBOTS_API_KEY='...'

uv run python scripts/eval/run-navigation-fleet.py \
  --mode formal \
  --run-id final-model-a \
  --parallelism 8 \
  --model-id provider/model \
  --base-url https://provider.example/v1 \
  --api-protocol responses \
  --model-parameters-file configs/model_parameters/gemini-3_7-flash_high.json
```

The model-parameters file is required and contains the exact sampling/model
request fields passed to the API (but not `model_id` or `api_protocol`, which
remain explicit fleet arguments). The repository includes this example:

```json
{
  "reasoning_effort": "high"
}
```

The task list and eval-setting files are optional. With neither argument, the
fleet runs all 194 benchmark tasks using the standard task-eval defaults. To
run an exact subset or override task settings, pass either or both files:

```bash
uv run python scripts/eval/run-navigation-fleet.py \
  --run-id smoke-subset \
  --model-id provider/model \
  --base-url https://provider.example/v1 \
  --model-parameters-file configs/model_parameters/gemini-3_7-flash_high.json \
  --task-list-file configs/task_lists/example_subset.json \
  --eval-setting-file configs/eval_settings/default.json
```

Task lists use a stable, ordered task-ID array:

```json
{
  "schema_version": 1,
  "task_ids": ["cape-town-001", "cape-town-002"]
}
```

Eval-setting files may contain all settings or only the fields being
overridden; omitted fields retain their standard defaults. The fleet validates
unknown tasks, duplicate task IDs, unknown eval fields, and invalid values
before starting workers. It records the resolved model parameters and eval
overrides in the run artifacts and the fleet summary.

`--parallelism` controls the number of simultaneously active task containers;
the host therefore needs roughly `parallelism * cpus-per-task` CPU capacity and
`parallelism * memory-per-task` memory. Override the per-task limits with
`--cpus-per-task`, `--memory-per-task`, and `--shm-size-per-task`. Podman is
preferred when both engines exist; select explicitly with
`--container-engine podman|docker`. The default image is
`mcbots-navigation:1.21.11`.

For NVIDIA rendering on Linux, install the NVIDIA driver and NVIDIA Container
Toolkit on the host, confirm `nvidia-ctk cdi list` exposes the desired devices,
and build the derived GPU image:

```bash
podman build \
  --file containers/Containerfile.navigation-cpu \
  --tag mcbots-navigation:1.21.11 \
  .

podman build \
  --file containers/Containerfile.navigation-gpu \
  --tag mcbots-navigation-gpu:1.21.11 \
  .
```

Pass only the host GPU indices that the fleet may use. Worker slots are spread
as evenly as possible and retain their GPU lease for all retries of a task. For
example, 30 concurrent workers over six selected GPUs creates exactly five
slots per GPU:

```bash
uv run python scripts/eval/run-navigation-fleet.py \
  --mode formal \
  --run-id final-model-gpu \
  --parallelism 30 \
  --gpu-devices 0,1,2,3,4,5 \
  --cpus-per-task 4 \
  --memory-per-task 8g \
  --container-engine podman \
  --model-id provider/model \
  --base-url https://provider.example/v1 \
  --api-protocol responses \
  --model-parameters-file configs/model_parameters/gemini-3_7-flash_high.json
```

When the division is not exact, earlier devices in the supplied list receive
one additional slot, so per-GPU slot counts differ by at most one. Before any
task starts, the fleet validates every selected index with host `nvidia-smi`,
starts a temporary CDI-bound Xorg container for each GPU, and rejects missing
OpenGL or software renderers such as llvmpipe. GPU mode currently requires
Podman CDI (`--device nvidia.com/gpu=<index>`); omitting `--gpu-devices` keeps
the existing CPU path. The fleet summary records the selected devices and the
exact slot count assigned to each one, while each task records its actual GPU
index in `runtime_allocation.gpu_device`.

Fleet resume is enabled by default. Reusing the same run ID skips every task
that already has a complete scored result, including a legitimate model task
failure. It deletes and restarts only incomplete attempts or attempts marked as
infrastructure errors, such as a worker, server, client, or agent crash. It
never resumes a model midway through a Minecraft world. The default one
immediate infrastructure retry can be changed with
`--infrastructure-retries`; use `--no-resume` to fail on any existing task
directory.

Result and ephemeral runtime roots can be placed on dedicated volumes:

```bash
uv run python scripts/eval/run-navigation-fleet.py \
  --mode formal --run-id final-model-a \
  --parallelism 8 --cpus-per-task 4 --memory-per-task 8g \
  --results-root /data/mcbots/results \
  --runtime-root /scratch/mcbots/runtime \
  --model-id provider/model \
  --base-url https://provider.example/v1 \
  --api-protocol chat_completions \
  --model-parameters-file configs/model_parameters/gemini-3_7-flash_high.json \
  --record-video
```

Video recording is disabled by default. `--record-video` records the agent
display in each task's `agent-record/` directory. Fleet logs and its final
summary are stored under `<results-root>/_fleet/<run-id>/`; task results retain
the standard `<results-root>/<run-id>/<task-id>/` layout. Prepared snapshots and
the smoke-verified runtime are preflighted before any worker starts unless
`--skip-preflight` is explicitly supplied.

Each task writes ignored artifacts below
`eval/results/navigation/<run-id>/<task-id>/`: run identity, sampled positions,
claims, completion, metrics, agent status, runtime readback, and supervisor
classification. It also writes `snapshot-after-run.json`; formal aggregation
requires both the source and prepared fingerprints plus the preparation digest
to remain unchanged. Server, client, AgentBridge, and agent crashes are
infrastructure errors rather than task failures.

## Aggregate

```bash
uv run python scripts/eval/aggregate-navigation-results.py \
  --run-id final-model-a \
  --require-all
```

For a custom fleet result root, pass the same location to aggregation:

```bash
uv run python scripts/eval/aggregate-navigation-results.py \
  --run-id final-model-a \
  --results-root /data/mcbots/results \
  --require-all
```

The aggregator reads formal results only. It rejects mixed profile or setting
digests, model parameters, stale map fingerprints, and mismatched runtime
version readbacks. It reports success rate, infrastructure errors, duration,
decision count, path length, progress, ordered checkpoint coverage, claims,
and static SPL when an optional reference length was available at run time.

## Final Admission Checklist

- The Shun Lee source snapshot readback is Minecraft 1.21.11 / DataVersion 4671.
- The prepared snapshot differs from its source only by the complete locked
  `datapacks/heights` replacement, and Minecraft enables that pack without a
  compatibility error.
- The runtime receipt is `smoke_verified` with Minecraft 1.21.11 and NeoForge
  21.11.44.
- The client has exactly the six pinned mods and no Baritone.
- The benchmark catalog loads exactly 194 owner-approved tasks across 30 maps.
- A no-model smoke has been completed on representative maps.
- `formal --all` completes and `aggregate-navigation-results.py --require-all`
  succeeds without identity mixing.
