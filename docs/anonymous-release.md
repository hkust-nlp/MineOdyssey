# Source release notes

## Included changes

- Nine waypoint corrections with their original coordinate records and regression tests.
- Command/action IDs remain attached to late results; a timeout or stop targets the
  originating command. Rejected/failed commands emit terminal feedback rather than
  leaving the agent waiting. XML and native tool histories both retain correlation.
- Summary transitions preserve staged command output and evaluator feedback and wait
  for the current command's terminal result before freezing summary input.
- Messages append to `messages.jsonl`; the navigation task wrapper finalizes an atomic
  `messages.json` on exit. The viewer reads live journals. For standalone agent use,
  after stopping the process run `python scripts/analysis/finalize_agent_messages.py
  --record-dir PATH` to generate the legacy JSON file.
- Native `minecraft_action` calls, Gemini signature transport, opaque provider state
  replay, and Responses stateless fallback. The navigation launcher defaults
  to native tools, matching historical formal GLM runs. XML remains an
  explicit legacy option.
- Optional shared request gates and bounded 429 retries are available through the
  agent's `MCBOTS_LLM_GATE_*` and `MCBOTS_LLM_429_*` environment variables. They remain
  disabled by default. The new fleet autoscaling/launcher configuration is excluded.
- Optional network-isolated Remote Bash relay with per-action key ownership and
  cleanup. The navigation wrapper accepts `MCBOTS_NAV_REMOTE_BASH_NETWORK_DISABLED=true`.
  The input lease guarantees apply to `mcapi` calls through that relay, not arbitrary
  xdotool key presses or direct HTTP outside it. The default network policy is unchanged.
- NBT modified UTF-8 decoding supports encoded NUL and supplementary characters.

## Excluded changes

No local credentials or unrelated experiment configurations were merged. The baseline
task roster is retained. The subsequently requested formal-run alignment updates
the limits to 500 steps and a 21600-second watchdog, the summary threshold to 100
turns, and the runtime's pinned Java artifact to the rebuilt coordinate-filter-v2
implementation. Fixed-camera/no-map experiments, reference-navigation
extensions, model-specific image caps, task removals, extra waypoint edits in older
workspaces and fleet autoscaling are excluded.

## Anonymization

This repository starts from a clean source snapshot with fresh anonymous
commit metadata. Original development history and remotes are not imported.
The source-only ZIPs contain no Git metadata. The actual API configuration,
old agent recordings, route-review media, generated snapshots/templates/runtime state,
local game data and cluster bootstrap helpers are outside this package. Known author
names, user home/cluster paths, repository URLs and repeated credential values were
removed or replaced. Do not publish the separate private exclusion directory.

`anonymous/source`, `https://example.invalid/anonymous-source`, a 40-zero Git revision,
and release ID `1` are explicit redaction sentinels, not real provenance claims or
working download endpoints. Raw annotation content and archive/world SHA-256 checks
are preserved; the original attribution mapping remains in the private source.
Third-party dependency URLs, licenses, build pins and map/place names are retained.

An anonymous map-asset mirror is still needed before this can be offered as an
end-to-end downloadable benchmark. Do not substitute owner-linked release URLs.

## Validation limits

Unit/regression tests and syntax checks exercise the selected changes. The Linux
CPU image builds successfully; live Minecraft server/client preparation and the
new image's Mesa graphics, screenshot and Bubblewrap checks passed. The coordinate-lock
AgentBridge artifact was rebuilt from the included source, with six Java tests passing.
Third-party binary visual content review has not been performed.
Automated identity/credential
scans do not prove that public code or content hashes cannot be correlated with a
previously published repository.

Verification: 183 selected-fix regression tests and 10 Linux launcher tests passed.
The separated base passes all 253 shared unit tests in the original Agent
environment with Harbor uninstalled. Adapter-specific validation is maintained
on `harbor`.
The historical September 29 privacy scan checked 387
source-package files and 63 embedded JAR entries with no remaining matches for
the known author identifiers, original credential values, or common secret formats.
The scanner also confirmed the baseline task catalog/settings/profiles were retained
and exactly nine waypoint positions changed. The current scan checks the authorized
formal settings and rebuilt mod hash separately while retaining the nine-point and
unchanged-catalog checks. This is a bounded automated check.

## Portable Linux entrypoint

`python3 scripts/launch/navigation-linux.py` provides build, doctor, tasks,
prepare-runtime, prepare-map and run commands. The CPU image now uses generic Linux
labels/paths and explicitly installs xauth. The launcher keeps host paths in argv
boundaries, publishes only optional loopback noVNC, rejects missing map files and
credentials early, and disables inherited Podman host proxy settings. Build-context
ignore files exclude credentials and generated data. See the Linux guide for the
separate map-archive prerequisite and runtime validation status.

## Branch scope

This `main` branch contains the shared updates and original
Linux runner. Harbor tasks, adapters and their checks are maintained separately
on `harbor`; that branch uses the same runtime and evaluation
code. See the root README for the branch update workflow.

## Publication audit (2026-10-04)

The Harbor quickstart uses the provider's model ID for `-m`; the configuration key
is selected separately through `MCBOTS_HARBOR_MODEL_KEY`. These values may differ.
Original source revision identifiers are redacted in prompt provenance metadata,
the historical audit, and nested map provenance. Prompt bytes, archive hashes,
world fingerprints, and independent SHA-256 assertions are unchanged. The publication history is rebuilt from the checked source snapshots
so the superseded commits are not retained on either release branch.
