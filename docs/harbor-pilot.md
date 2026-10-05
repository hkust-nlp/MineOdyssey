# Harbor navigation: shared original Agent and catalog task export


**Current baseline:** the actual historical formal GLM runs, checked against their
run receipts and model-visible journals. The default is native `tool_calls`, 500
assistant decision steps, a 21600-second infrastructure watchdog, 100-turn
summarization and `xaero-coordinate-filter-v2`. The active catalog is the final
180-task main benchmark, shared with the original runner; the nine requested
waypoint corrections are retained. The initial-submission XML prompt remains an
explicit legacy option; earlier XML Harbor trials are diagnostic evidence and
are not relabeled as formal-profile runs. Old waypoint revisions likewise remain
explicit when comparing trajectories.

The default adapter runs the original `python -m agent.main` inside Harbor's
`main` container, using `/opt/mcbots-venv/bin/python` and the repository's `uv.lock`.
The host only launches/supervises the container process and collects trusted results.
The container-local bridge transports the existing Remote Bash/claim/event interfaces
over the shared Unix socket; it does not construct another model loop. Minecraft
and the original evaluator remain in the networkless `world` service, and final
verification runs in a separate container. No original Agent/evaluator code changes
are required for this deployment. This supersedes the October 2 host-Agent layout;
the three historical GLM runs below tested that earlier layout.
The historical `eval.harbor_agents.vision:NavigationVisionAgent` entry point is an
alias for `eval.harbor_agents.original:OriginalNavigationAgent`.

## Prompt and actual-run contract

The native system prompt matches the actual historical GLM journal byte for byte:
`e46a9f5101652881fc270a29dc706fc5ae0fde1ea94d502dde0288fea00930ed`.
`tests/fixtures/navigation_formal_glm.json` pins the independent historical policy,
model parameters and native function schema; the source run is identified by hash.
The shared `agent/navigation_prompt.py` still tests the legacy XML prompt against
its initial-submission hashes, separately from the current native baseline.

Harbor instruction.md is generated from the same Objective, Route Completion and
game-control text, preserving the original-language task. Navigation system guidance
remains English as in the original run. The 2.5-block/1.5-block/3-second advice is
unchanged; internal evaluator thresholds are not inserted as extra model hints.
Generic agents receive CLI transport instructions. OriginalNavigationAgent instead
receives the full native system prompt and original task, without that CLI suffix.
Regenerate with `python -m eval.harbor_agents.instructions`; `--check` and task
export reject stale text.

Coordinate locking is enforced in the game, not by a prompt. Xaero map coordinates
are hidden; waypoint editors, settings screens and shared waypoint coordinate chat
are blocked. The main map and original `mcapi state` player position remain usable.
Startup checks the actual AgentBridge enabled flag and policy version and fails if
they differ. The container-written parity receipt includes this runtime readback.

## What is shared with the original runner

The shared Agent and Environment own the navigation prompt, native action protocol
(or explicitly configured legacy XML), one-action rule, asynchronous execution,
`observe_after_sec`, interruption, JPEG observations, frame filtering, model calls,
provider compatibility, failure counters, summaries and message journals.

The adapter reads effective settings from the world run: SDK retries 0, maximum
consecutive request failures 3, request timeout 600 seconds, exec timeout 300,
100-image summary threshold, 100-turn summary threshold, 200000-token summary
threshold and 500 successful navigation decisions. Failed requests and summary
requests retain the original counting semantics. Only the original task prompt is
passed to the Agent; the generic Harbor CLI instructions are not appended.

The settings also retain event-only observations, disabled observation toggles,
no panorama, the original corrected task, arrival radius 3.5/vertical 1.5, three
claims, death/boundary rules and a 21600-second infrastructure watchdog. The original prompt's
2.5-block/3-second arrival advice remains unchanged. It is advice, not a new
scoring rule. Harbor's outer agent timeout is 22800 seconds to allow startup and
cleanup around the independent 21600-second watchdog.

## Linux setup and run

Build the CPU base image using [Linux setup](linux-quickstart.md). Maps and runtime
caches are private build inputs, excluded from public source packages. Preparation
writes `eula=true`; accept the Minecraft EULA before preparing/running the runtime.
The world needs about 12 GB RAM/four CPU cores; budget at least 16 GB host RAM.

Install Harbor on the host. The CPU base image already contains the locked Agent
dependencies; exporting also creates `environment/agent-source.tar.gz` containing
only the unchanged Agent, its container bridge and journal finalizer, without the
evaluator, task answers or maps:

```bash
uv tool install 'harbor==0.23.0'
python3 scripts/launch/navigation-linux.py build
python3 scripts/eval/export-harbor-navigation.py \
  --output /tmp/innopolis-006 \
  --map-archive /path/to/navigation-1.21.11-innopolis.zip
export MCBOTS_HARBOR_API_MODELS_FILE=/path/to/private/api_models.local.json
export MCBOTS_HARBOR_MODEL_KEY=glm-5.3-flash
PYTHONPATH=. python3 scripts/eval/run-harbor-navigation.py --engine docker -- \
  -p /tmp/innopolis-006 \
  -a eval.harbor_agents.original:OriginalNavigationAgent -m glm-5.3-flash -n 1
```

The selected external configuration supplies `modelname`, `api_key`, `base_url`
and `model_params`. `MCBOTS_HARBOR_MODEL_KEY` selects the JSON entry; Harbor
`-m` must equal that entry's `modelname`, which need not equal its entry name. `api_protocol` and `action_protocol` may be specified; their
defaults are `chat_completions` and `tool_calls`. The launcher uploads only the
selected model configuration to a private 0600 file under `/run/navigation-agent`
in `main`; the runner reads and deletes it before starting the Agent. Credentials
never enter images, command arguments, collected logs or `world`. The original
Agent receives its API key in its container process environment, as expected.
The container runner passes only a small environment allowlist to its child; unrelated
host variables are not copied. Explicit optional navigation tuning is preserved,
but path-valued overrides refer to container paths, not arbitrary host mounts.

The Agent interpreter is fixed inside the image; `MCBOTS_HARBOR_AGENT_PYTHON` is no
longer used. Image build/start checks verify dependencies against `uv.lock`, and the
host checks the complete Agent Python source inventory plus bridge/lock hashes
before delivering credentials or starting Minecraft. The standard profile has no
video or panorama; unsupported settings are rejected explicitly.

For Podman build the base image in Podman and use the launcher below with
`--engine podman`. The launcher checks the host and, for rootless systemd cgroup v2,
starts Harbor in a systemd user scope. This avoids cross-subtree cgroup migration
failures in older crun when launched from a login session. It preserves the working
directory, environment and all Harbor run options; Docker needs no systemd wrapper. Harbor 0.23.0 can
misdetect podman-compose 1.6 as Compose V2; prefix only the Harbor command with
`PODMAN_COMPOSE_PROVIDER=/bin/false` if `podman compose ls` fails. This changes no
host configuration. Its missing `compose cp` can emit warnings; Harbor falls back
to tar transport. Docker and an uncached Harbor runtime build remain untested here.

An existing fingerprint-verified local cache can accelerate a private export:

```bash
python3 scripts/eval/export-harbor-navigation.py \
  --output /tmp/innopolis-local \
  --snapshot /path/to/snapshot-cache/1.21.11/innopolis \
  --runtime /path/to/navigation-linux-cpu/1.21.11
```

The exporter verifies world fingerprints and original archive identity. Rebinding
the anonymized metadata digest does not change map contents or skip verification.

Use `--task TASK_ID` to select another task from the active 180-task catalog. Retired
task IDs are rejected before export. The exporter
generates that task's original-language instruction and bakes its identity into
both the world gateway and the separate verifier. Docker build arguments select
the matching map; a completion from another task is rejected. The shared Innopolis
template's previous trial results are not copied as validation of the new task.

For example, export several task directories under one dataset directory, supplying
each map's own verified snapshot or pinned archive:

```bash
python3 scripts/eval/export-harbor-navigation.py --task wurzburg-007 \
  --output /tmp/harbor-rollouts/wurzburg-007 \
  --snapshot /path/to/snapshot-cache/1.21.11/wurzburg \
  --runtime /path/to/navigation-linux-cpu/1.21.11
PYTHONPATH=. python3 scripts/eval/run-harbor-navigation.py --engine podman -- \
  -p /tmp/harbor-rollouts \
  -a eval.harbor_agents.original:OriginalNavigationAgent -m glm-5.3-flash \
  -n 3 --max-retries 0
```

Each trial retains the formal 500-step budget and 21600-second watchdog and its own game, action
workspace, feedback journal and trusted verifier. `--max-retries 0` disables whole
Harbor trial replay; it does not disable the original Agent's model-request retry
loop. Credentials and runtime/map archives remain private.

## Transport, isolation and results

World has `network_mode: none`, with only loopback networking. Main has a separate
network for generic Harbor agents and a read-only Unix-socket mount. World receives
SYS_ADMIN/NET_ADMIN for nested Bubblewrap; action processes drop capabilities and
have no external network. No host ports, host engine socket or evaluator source
are exposed to main. Separate bridge networks proved insufficient on the tested
Podman host; the world therefore has no external network interface.

A loopback proxy inside `main` forwards only `/exec`, `/exec_async`, task status and stop
requests directly through the Unix socket, without a host Compose exec for each
action or observation. Shell commands still execute inside the original
Bubblewrap sandbox. The original JPEG screenshot command, timeout handling and
input leases use these same routes. Public evaluator messages are copied to the
original NavigationClaimClient's event journal, with an offset; claim IDs and
responses pass through unchanged. World coordinates outside the original public
state/feedback channels are not sent as additional hints.

The host supervisor sends failure causes through trusted `service_exec` in world;
this operation is not an endpoint on the agent socket. `llm_failure_limit`,
`step_limit`, crashes and normal stops remain distinct. The verifier refuses
to create a reward for infrastructure failures. A model/provider failure must not
be counted as a scored navigation failure.

The navigation launcher selects `eval.harbor_agents.verifier:NavigationVerifier`
through Harbor's supported `--verifier` option. It runs the ordinary isolated
verifier, then interprets a missing reward only when its collected completion is
terminal, belongs to this task and explicitly marks an infrastructure failure.
Such trials raise `NavigationInfrastructureError` with the original reason, e.g.
`wall_clock_watchdog` or `llm_failure_limit`, and retain no navigation reward.
Missing/malformed evidence still raises the original missing-reward error.
Direct `harbor run` invocations must add
`--verifier eval.harbor_agents.verifier:NavigationVerifier` for this reporting.
Explicit `--verifier` / `--verifier-import-path` choices are preserved by the launcher.

Harbor stops main before collecting evidence directly from world, then grades in
a fresh container. Completion, positions, metrics, supervisor state, public events
and world run metadata are collected. The Agent writes original messages.jsonl, finalized messages.json, screenshots,
frame-filter telemetry, Agent status and parity receipts to `/logs/agent` in `main`;
Harbor collects this per-trial log directory. Receipts bind source hashes,
dependencies, settings, model parameters and the actual runner/Agent namespace IDs.
All run artifacts remain private and are excluded from the anonymous source ZIP.

`nav read-image` remains available to other Harbor agents for bounded PNG/JPEG
retrieval. The original baseline Agent retains its original action schema and
does not receive an extra read_image tool. Action `/tmp` persists across calls in
this source version; this is a shared runtime fix, not a host-only capability.

## Historical verification before containerizing the Agent

`tests/harbor_original_smoke.py` uses a scripted local model provider with a real
Minecraft/Harbor trial. It asserts the exact original formal system prompt and native tool
protocol, injects a 500, then exercises async execution, interruption, persistent
files and false completion claims. The continuous-failure variant returns 500
three times and requires `llm_failure_limit` with no reward. These are infrastructure
tests, not successful model navigation or oracle routes.

The first aligned GLM trial produced six successful responses and no recorded
provider failures, then stopped with an adapter transport error. It was recorded
as an infrastructure failure without a reward. Its old diagnostics did not retain
the underlying transport error, so the root cause is still unconfirmed.

The adapter now records bounded, credential-redacted transport diagnostics and
retries read-only status/event requests at most three times. It never automatically
replays action or claim requests after uncertain delivery. This does not change
the original Agent's model-request retry policy. A sustained real-world scripted
replay of the GLM map/zoom actions completed 12 provider calls (one injected 500,
11 successful responses), all three false claims and separate verification without
a transport or Harbor exception. This targeted pass does not establish that the
intermittent failure is resolved. The run receipt also hashes the host adapter.

The subsequent multi-task GLM rollout captured `crun: Runtime did not set up
terminal` (exit 255) on all three trials. Harbor 0.23's Compose exec omits `-T`,
which causes podman-compose to request a PTY for these noninteractive JSON RPCs.
`eval/harbor_agents/transport.py` now adds `-T` to Compose exec commands on the
current trial's environment instance, preserving Harbor's command, environment,
timeout and service handling. It does not modify the installed Harbor package.
The run receipt records `container_exec_tty=false` and the compatibility module's
hash. Unit checks cover RPCs, sidecar collection, repeated setup and isolation
from other environment instances. New live rollouts validate this change; the
first trial's older, incomplete diagnostics do not prove it had the same cause.

The no-PTY rerun eliminated that terminal-allocation error but exposed a separate
`cgroup.procs: Permission denied` (exit 126) with crun 1.27. The host process was
in a login-session cgroup while containers were in the systemd user-service
subtree. The new `scripts/eval/run-harbor-navigation.py` launcher runs Harbor in a
user scope for this backend; it does not disable cgroups or weaken container
isolation. See the matching [Podman upstream issue](https://github.com/podman-container-tools/podman/issues/14851).
The run receipt records whether the host is in its user-service subtree. A short
8-way, 200-exec user-scope stress test passed with zero errors; the corresponding
login-session control also passed, so this probe does not establish deterministic
reproduction of the intermittent failure. A real-world scripted injection of one
undelivered action terminated as `agent_crash`, infrastructure error true, in
6 seconds without a reward. The separate verifier returned the expected
RewardFileNotFoundError. Those runs used the earlier XML/default profile and are retained as diagnostics.

An exhausted action RPC failure now alerts the supervisor and terminates the
trial through the trusted infrastructure-failure channel. Returning HTTP 502 to
the model alone previously allowed a transport-affected run to end with a normal
navigation zero. Historical raw rewards are retained, but those diagnostic trials
are excluded from clean navigation comparisons and rerun after the fix. Uncertain
actions and claims are still never replayed; model-request retries are unchanged.

In that diagnostic batch, Hofburg 003 naturally terminated out of bounds with
reward 0 before maintenance. Innopolis 008 and Würzburg 007 were stopped through
the trusted channel for observed transport faults and have no navigation reward.
Those records are retained separately from the subsequent no-PTY rerun.

```bash
PYTHONPATH=. python3 scripts/eval/run-harbor-navigation.py --engine podman -- \
  -p /tmp/innopolis-local \
  -a tests.harbor_original_smoke:OriginalSmokeAgent -n 1 --max-retries 0
PYTHONPATH=. python3 scripts/eval/run-harbor-navigation.py --engine podman -- \
  -p /tmp/innopolis-local \
  -a tests.harbor_original_smoke:OriginalFailureSmokeAgent -n 1 --max-retries 0
PYTHONPATH=. python3 scripts/eval/run-harbor-navigation.py --engine podman -- \
  -p /tmp/innopolis-local \
  -a tests.harbor_original_smoke:OriginalTransportStressAgent -n 1 --max-retries 0
```

The earlier isolation attack test remains at `tests/harbor_isolation_smoke.py`.
It poisons main's reward, completion, Python and background processes and expects
trusted reward 0. See task `validation.json` for receipts and limits, and the
[historical parity audit](harbor-parity-audit.md) for why the simplified model loop
was replaced. This adaptation does not grant formal-run eligibility or establish
that GLM has solved the navigation task.


## Remaining integration limits after actual-trajectory review

The task layout runs on Harbor 0.23.0, but this is still a diagnostic integration.
The following distinctions must remain explicit when publishing or aggregating it:

- **Historical results are versioned.** Earlier XML/default trials used a different
  prompt, budget and observation policy. Current native runs require their own
  receipts and cannot inherit those earlier validation claims. Matching policy
  does not guarantee identical model trajectories or runtime latency.
- **Generic agents have a different action interface.** `nav exec` is synchronous.
  Its default timeout is 30 seconds and its maximum is 120 seconds, matching the
  unchanged Remote Bash `/exec` endpoint. The CLI and gateway reject larger
  values; they no longer advertise 300 seconds and silently execute for 120.
  Original-Agent asynchronous actions retain the formal 300-second timeout.
  Original asynchronous observation, interruption and feedback policy are retained
  by the container-local original-Agent bridge and raw RPC interface; switching to an arbitrary
  Harbor agent does not preserve that control policy automatically.
- **Installation is not self-contained.** Task images require a separately built
  `localhost/anonymous-navigation:linux-cpu` base and externally supplied maps.
  The original Agent and its locked Python environment are included in `main`; the
  host retains the launcher/source needed for export and integrity checks. Fresh
  Docker builds remain untested here.
- **Backend requirements are substantial.** Compose must support the separate
  world service and verifier. World needs 12 GB / four CPUs and the documented
  nested-sandbox capabilities; the main task's 2 GB / two CPUs are not the total.
  Backends that cannot run these sidecars/capabilities are not validated.
- **The new layout still has a transport boundary.** Routine actions, screenshots
  and feedback now use the direct container-local socket. Compose exec remains for
  lifecycle operations and trusted collection. No performance improvement or exact
  trajectory equivalence is claimed without measurement.
- **Agent resources now belong to `main`.** The Agent shares its two-CPU/2-GB limit;
  world retains its separate four-CPU/12-GB limit. There is no original-Agent child
  process or extra original-Agent virtualenv on the host. Container termination
  contains the process lifetime; the adapter additionally requests graceful cleanup.

Two additional issues from this review have been repaired. A trusted host-only
model-metadata write now replaces the review placeholder in collected run.json,
while preserving `world_profile_model_parameters` and marking the metadata source.
Only selected nonsecret model settings cross this channel; an unreported generic
agent is labeled unreported instead of Gemini. A real-world failure-injection
trial verified the recorded scripted model and unscored infrastructure outcome.
The exporter also normalizes the gzip header, including its timestamp/name, so
identical inputs produce identical archive bytes and stable Docker build inputs.
See task validation.json for the current regression and live-validation status.

## Formal-profile validation before containerizing the Agent

The selected coordinate-lock implementation is rebuilt from this anonymous source;
its bundled JAR hash is pinned in the runtime profile. A supplied runtime archive
must match that profile; old receipts are rejected. The new native smoke uses the
real Agent, probes the map and blocked waypoint UI, injects a first-request HTTP
500, interrupts an action, reads a file across actions and exhausts three false
claims. Run it with `tests.harbor_original_smoke:FormalCoordinateSmokeAgent`.
The ordinary failure and transport-failure smoke variants remain available.

Historical GLM settings: `max_tokens=32000`, `reasoning_effort=max`,
`thinking={"type":"enabled"}`, Chat Completions and native tools. Provider keys
and URLs are private host inputs. The 6-hour watchdog is an infrastructure end,
not a scored failure. The 500-step limit produces `step_limit`; failed model
requests and summary requests retain the original counter semantics.

Current validation: 176 Python and 6 Java tests passed. The native real-world smoke
completed 12 requests (one injected 500, eleven successful replies), blocked two
waypoint-screen attempts, and produced the expected trusted reward 0 with no trial
exception. Three GLM reruns have matched effective settings and actual prompt
hashes. Hofburg 003 ended out of bounds (reward 0), Innopolis 008 ended on death
(reward 0), and Würzburg 007 reached the six-hour watchdog (unscored). No successful
navigation is claimed. The subsequent Harbor boundary fixes preserve the Agent,
Remote Bash and evaluator core; only CLI bounds and verifier error reporting change.
See validation.json for separate unit, completion-replay and live-smoke evidence.

The October 2 boundary regression passed 68 Python checks and replayed all three
original GLM completions through Harbor's actual reward parser, with container
execution mocked. Both scored zeros remain zero; the watchdog remains unscored
and now reports `wall_clock_watchdog` in `NavigationInfrastructureError`.
A separate live Podman/Minecraft smoke rejects `--timeout 300` before execution,
runs a command with `--timeout 120`, and checks a real short command timeout.
Three scripted provider failures then produce `llm_failure_limit` through the
isolated verifier, with the explicit infrastructure exception and no reward.
This is an adapter regression, not an additional GLM navigation run. Earlier raw
GLM results are retained unchanged; the corrected reporting applies to new runs.

## Container-local Agent deployment (2026-10-03)

`eval/harbor_agents/original.py` is now only the Harbor lifecycle adapter;
`eval/harbor_agents/container.py` runs inside `main` and starts the unchanged Agent.
The latter owns only transport and process supervision, preserving the original
model loop, prompts, asynchronous commands, observations, retries and summaries.
The standard entry point and model-selection variables are unchanged.

A separate scripted provider also runs inside `main` for the integration smokes.
Tests bind the Agent PID to the container's PID/mount/network namespaces, reject
access to a host-only sentinel, verify deletion of the temporary credential file,
and exercise real Minecraft commands, observations, interruption and retry/terminal
handling. These scripted checks must not be described as fresh GLM navigation runs.
See `validation.json` for completed results; historical GLM artifacts are unchanged.
