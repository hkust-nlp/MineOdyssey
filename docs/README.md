# Documentation

Canonical repository: [hkust-nlp/MineOdyssey](https://github.com/hkust-nlp/MineOdyssey).

## Getting started

- [Project overview](../README.md): Benchmark scope and agentic spatial intelligence, installation, 30-map downloads, branch selection, named formal model runs, full-benchmark execution, result aggregation, model-selection examples, and validation scope.
- [Paper figures](assets/paper/README.md): Environment overview, agent framework, and terrain examples matched to the September 29 manuscript package, with source versions and presentation notes.
- [Linux quickstart](linux-quickstart.md): CPU container setup, CPU/GPU rendering scope and virtual display, resource-measurement status, graphics and sandbox checks, verified downloads and imports for one or all 30 maps, credentials, named formal runs, result aggregation, and reuse of the same runtime for batches.
- [Task catalog](task-catalog.md): All 30 selected maps, 180 tasks, locale tags, source manifests, and the matching map release shared by Linux and Harbor exports.
- [Release notes](anonymous-release.md): Selected runtime fixes, nine waypoint corrections, formal settings, anonymization, and validation limits.

## Harbor

- [Harbor setup and runtime](harbor-pilot.md): Task export, Podman setup, configured resource limits, container-local original Agent, credentials, isolated verification, and version-specific live checks.
- [Parity audit](harbor-parity-audit.md): Historical differences and the evidence used to align Harbor with the original runner.
- [Task template](../eval/harbor/innopolis-006/README.md): Concrete task layout, the pinned release map download, and runtime inputs.

## Development and retained operational notes

The following guides cover lower-level or historical workflows. Use the root
README and Linux/Harbor setup above for this release; tracked schemas and settings
take precedence over historical examples.

- [Navigation evaluation](navigation-eval.md): Current single-task and batch commands, shared runtime defaults, GPU setup, release-aware aggregation, and labeled legacy preparation notes.
- [Script layout](scripts-layout.md): Script categories and entrypoints.
- [API examples](../agent/examples/README.md): Bot-side API examples.
- [Common operations](FastQA.md): Earlier bootstrap, task, and model workflows.
- [Single-task evaluation](eval/run-single-task.md): Earlier task-running workflow.
- [Networking](networking-and-env.md): Earlier network and environment notes.
- [Self-reward grader](self-reward-grader-quirks.md): Provider-specific grader diagnostics.

## Maintaining documentation

Keep focused guides under `docs/`. When adding, renaming, or removing a guide,
update this index and the documentation map in `AGENTS.md` in the same change.
Shared documentation belongs on `main` first; Harbor-specific guides belong on
`harbor`.
