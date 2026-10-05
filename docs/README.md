# Documentation

## Getting started

- [Project overview](../README.md): Installation, branch selection, model runs, evaluation, results, model-selection examples, and validation scope.
- [Paper figures](assets/paper/README.md): Environment overview, agent framework, and terrain examples embedded in the README, with source versions and presentation notes.
- [Linux quickstart](linux-quickstart.md): CPU container setup, graphics and sandbox checks, map preparation, credentials, and one-task execution.
- [Task catalog](task-catalog.md): All 30 selected maps, 180 tasks, locale tags, source manifests, and external asset requirements.
- [Release notes](anonymous-release.md): Selected runtime fixes, nine waypoint corrections, formal settings, anonymization, and validation limits.

Harbor documentation and adapters are on the
[`harbor` branch](https://github.com/mine-odyssey/MineOdyssey/tree/harbor).

## Development and retained operational notes

The following guides cover lower-level or historical workflows. Use the root
README and Linux/Harbor setup above for this release; tracked schemas and settings
take precedence over historical examples.

- [Navigation evaluation](navigation-eval.md): Runtime preparation, manual checks, and formal aggregation.
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
