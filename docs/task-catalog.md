# Task catalog

This release uses the final main-benchmark roster: **180 tasks on
30 maps**, with **20 locale tags**. These counts are computed from
[`tasks.json`](../eval/navigation/tasks.json) and agree with the selected maps in
[`finalpool-navigation-v1.json`](../eval/navigation/benchmarks/finalpool-navigation-v1.json).
A locale tag is a regional language variant, not a distinct language.

Each catalog row has `task_id`, `map_id`, `prompt`, `waypoints`, `eval_setting`, and
`metadata`. The ordered waypoint list has 2–12 entries, including the start. The
first entry sets the spawn, intermediate entries become required visits, and the
last entry is the final destination. The evaluator checks those visits and the
completion claim; the order in the catalog also specifies the natural-language
route. Task instructions retain their original language.

## Maps

| Map | Map ID | Tasks | Prompt locale |
| --- | --- | ---: | --- |
| [Buckingham Palace](../eval/navigation/maps/buckingham-palace/map.json) | `buckingham-palace` | 5 | en-GB |
| [Cape Town · City Centre / Bo-Kaap](../eval/navigation/maps/cape-town/map.json) | `cape-town` | 9 | en-ZA |
| [Chain Bridge](../eval/navigation/maps/chain-bridge/map.json) | `chain-bridge` | 1 | hu-HU |
| [Copacabana Waterfront](../eval/navigation/maps/copacabana-waterfront/map.json) | `copacabana-waterfront` | 3 | pt-BR |
| [Dublin Departments District](../eval/navigation/maps/dublin-departments-district/map.json) | `dublin-departments-district` | 7 | en-IE |
| [Entrup](../eval/navigation/maps/entrup/map.json) | `entrup` | 6 | de-DE |
| [Hagia Sophia](../eval/navigation/maps/hagia-sophia/map.json) | `hagia-sophia` | 3 | tr-TR |
| [Hofburg](../eval/navigation/maps/hofburg/map.json) | `hofburg` | 7 | de-AT |
| [Innopolis](../eval/navigation/maps/innopolis/map.json) | `innopolis` | 9 | ru-RU |
| [Memorial Hall Park](../eval/navigation/maps/memorial-hall-park/map.json) | `memorial-hall-park` | 1 | zh-Hant-TW |
| [Miljacka Riverside](../eval/navigation/maps/miljacka-riverside/map.json) | `miljacka-riverside` | 4 | bs-BA |
| [Mr Beast 1000$ Harbor City Shipyard](../eval/navigation/maps/mr-beast-1000-harbor-city/map.json) | `mr-beast-1000-harbor-city` | 11 | en-US |
| [Notre Dame](../eval/navigation/maps/notre-dame/map.json) | `notre-dame` | 4 | fr-FR |
| [911 Memories](../eval/navigation/maps/nyc-911-memorials/map.json) | `nyc-911-memorials` | 6 | en-US |
| [Ohrid](../eval/navigation/maps/ohrid/map.json) | `ohrid` | 5 | mk-MK |
| [Plaza de Mayo](../eval/navigation/maps/plaza-de-mayo/map.json) | `plaza-de-mayo` | 8 | es-AR |
| [Plaza Hotel](../eval/navigation/maps/plaza-hotel/map.json) | `plaza-hotel` | 6 | en-US |
| [Reichstag](../eval/navigation/maps/reichstag/map.json) | `reichstag` | 5 | de-DE |
| [RMS Queen Mary](../eval/navigation/maps/rms-queen-mary/map.json) | `rms-queen-mary` | 10 | en-GB |
| [RMS Titanic](../eval/navigation/maps/rms-titanic/map.json) | `rms-titanic` | 6 | en-GB |
| [Santa Lucia Hill](../eval/navigation/maps/santa-lucia-hill/map.json) | `santa-lucia-hill` | 5 | es-CL |
| [HK Shun Lee](../eval/navigation/maps/shun-lee/map.json) | `shun-lee` | 4 | zh-Hant-HK |
| [SoFi Stadium](../eval/navigation/maps/sofi-stadium/map.json) | `sofi-stadium` | 6 | en-US |
| [Sviyazhsk](../eval/navigation/maps/sviyazhsk/map.json) | `sviyazhsk` | 6 | ru-RU |
| [Torrey Mall](../eval/navigation/maps/torrey-mall/map.json) | `torrey-mall` | 5 | es-PE |
| [Ueno Park](../eval/navigation/maps/ueno-park/map.json) | `ueno-park` | 12 | ja-JP |
| [Versailles](../eval/navigation/maps/versailles/map.json) | `versailles` | 3 | fr-FR |
| [White House](../eval/navigation/maps/white-house/map.json) | `white-house` | 7 | en-US |
| [Würzburg](../eval/navigation/maps/wurzburg/map.json) | `wurzburg` | 10 | de-DE |
| [ALPS Zürich](../eval/navigation/maps/zurich/map.json) | `zurich` | 6 | de-CH |
| **Total** | **30 selected maps** | **180** | **20 locale variants** |

The source tree also retains manifests for `alcatraz`, `bismarck`, `dkm-tirpitz`,
`factory-collection`, and `grand-budapest-hotel`. They have no tasks in this selected
catalog and are not part of the 180-task count.

## Final roster

The published catalog matches the frozen 180-task main benchmark, including every
per-map count above. Linux task listing, full-benchmark selection, aggregation with
`--require-all`, and Harbor task export all read this active catalog. Route and
waypoint annotation files retain their broader source records; those annotations
do not add tasks to the benchmark.

The sorted task IDs (UTF-8, one per line, with a final newline) have SHA-256:

```text
d29ee013ba6cd62278d06c79351d4c13eb59559079f3e14d7561f2ef035c93d9
```

The initial source publication included 14 tasks outside the final roster. They
have been removed from the active catalog without renumbering retained task IDs:

- `buckingham-palace-001`
- `copacabana-waterfront-003`
- `copacabana-waterfront-005`
- `miljacka-riverside-003`
- `notre-dame-001`
- `notre-dame-003`
- `nyc-911-memorials-006`
- `rms-titanic-001`
- `shun-lee-003`
- `torrey-mall-005`
- `torrey-mall-007`
- `versailles-003`
- `white-house-001`
- `white-house-002`

The original-language prompts, routes, nine waypoint corrections, and evaluation
rules for the retained tasks are preserved. Previous run artifacts are unchanged.

## Assets and preparation

The [map release](https://github.com/hkust-nlp/MineOdyssey/releases/tag/navigation-maps-1.21.11-v1)
contains exactly these 30 maps (2.60 GB of ZIPs). The
[release manifest](../eval/navigation/releases/navigation-maps-1.21.11-v1.json)
records the download names, sizes, SHA-256 values, and extracted-world fingerprints.
Follow the [Linux download and import commands](linux-quickstart.md#3-download-and-prepare-maps)
for one map or all 30. GitHub repository access is required while the repository
is private.

Per-map source manifests retain original archive provenance, which can differ
from the prepared release ZIP. Redacted `example.invalid` provenance URLs are not
download links; the release manifest is the contract for the current importer.

The runtime copies worlds for individual tasks. Neither the original map archives
nor generated runtime caches are committed to this repository. Asset terms remain
those of their original providers.

## Inspect the roster

From the repository root, list IDs without starting a container:

```bash
python3 scripts/launch/navigation-linux.py tasks
```

Inspect one original-language task:

```bash
python3 - <<'PYTASK'
import json
from pathlib import Path

tasks = json.loads(Path("eval/navigation/tasks.json").read_text())["tasks"]
task = next(row for row in tasks if row["task_id"] == "innopolis-006")
print(json.dumps(task, ensure_ascii=False, indent=2))
PYTASK
```

Evaluation settings are versioned independently in the
[reference profile](../eval/navigation/settings/final-navigation-v1.json).
Nine waypoint positions were corrected in this release; see the
[release notes](anonymous-release.md). The active catalog is restricted to the final 180-task roster; retained task
texts and routes are unchanged.
