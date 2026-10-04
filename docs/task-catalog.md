# Task catalog

This source release preserves the selected finalpool-v2 roster: **194 tasks on
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
| [Buckingham Palace](../eval/navigation/maps/buckingham-palace/map.json) | `buckingham-palace` | 6 | en-GB |
| [Cape Town · City Centre / Bo-Kaap](../eval/navigation/maps/cape-town/map.json) | `cape-town` | 9 | en-ZA |
| [Chain Bridge](../eval/navigation/maps/chain-bridge/map.json) | `chain-bridge` | 1 | hu-HU |
| [Copacabana Waterfront](../eval/navigation/maps/copacabana-waterfront/map.json) | `copacabana-waterfront` | 5 | pt-BR |
| [Dublin Departments District](../eval/navigation/maps/dublin-departments-district/map.json) | `dublin-departments-district` | 7 | en-IE |
| [Entrup](../eval/navigation/maps/entrup/map.json) | `entrup` | 6 | de-DE |
| [Hagia Sophia](../eval/navigation/maps/hagia-sophia/map.json) | `hagia-sophia` | 3 | tr-TR |
| [Hofburg](../eval/navigation/maps/hofburg/map.json) | `hofburg` | 7 | de-AT |
| [Innopolis](../eval/navigation/maps/innopolis/map.json) | `innopolis` | 9 | ru-RU |
| [Memorial Hall Park](../eval/navigation/maps/memorial-hall-park/map.json) | `memorial-hall-park` | 1 | zh-Hant-TW |
| [Miljacka Riverside](../eval/navigation/maps/miljacka-riverside/map.json) | `miljacka-riverside` | 5 | bs-BA |
| [Mr Beast 1000$ Harbor City Shipyard](../eval/navigation/maps/mr-beast-1000-harbor-city/map.json) | `mr-beast-1000-harbor-city` | 11 | en-US |
| [Notre Dame](../eval/navigation/maps/notre-dame/map.json) | `notre-dame` | 6 | fr-FR |
| [911 Memories](../eval/navigation/maps/nyc-911-memorials/map.json) | `nyc-911-memorials` | 7 | en-US |
| [Ohrid](../eval/navigation/maps/ohrid/map.json) | `ohrid` | 5 | mk-MK |
| [Plaza de Mayo](../eval/navigation/maps/plaza-de-mayo/map.json) | `plaza-de-mayo` | 8 | es-AR |
| [Plaza Hotel](../eval/navigation/maps/plaza-hotel/map.json) | `plaza-hotel` | 6 | en-US |
| [Reichstag](../eval/navigation/maps/reichstag/map.json) | `reichstag` | 5 | de-DE |
| [RMS Queen Mary](../eval/navigation/maps/rms-queen-mary/map.json) | `rms-queen-mary` | 10 | en-GB |
| [RMS Titanic](../eval/navigation/maps/rms-titanic/map.json) | `rms-titanic` | 7 | en-GB |
| [Santa Lucia Hill](../eval/navigation/maps/santa-lucia-hill/map.json) | `santa-lucia-hill` | 5 | es-CL |
| [HK Shun Lee](../eval/navigation/maps/shun-lee/map.json) | `shun-lee` | 5 | zh-Hant-HK |
| [SoFi Stadium](../eval/navigation/maps/sofi-stadium/map.json) | `sofi-stadium` | 6 | en-US |
| [Sviyazhsk](../eval/navigation/maps/sviyazhsk/map.json) | `sviyazhsk` | 6 | ru-RU |
| [Torrey Mall](../eval/navigation/maps/torrey-mall/map.json) | `torrey-mall` | 7 | es-PE |
| [Ueno Park](../eval/navigation/maps/ueno-park/map.json) | `ueno-park` | 12 | ja-JP |
| [Versailles](../eval/navigation/maps/versailles/map.json) | `versailles` | 4 | fr-FR |
| [White House](../eval/navigation/maps/white-house/map.json) | `white-house` | 9 | en-US |
| [Würzburg](../eval/navigation/maps/wurzburg/map.json) | `wurzburg` | 10 | de-DE |
| [ALPS Zürich](../eval/navigation/maps/zurich/map.json) | `zurich` | 6 | de-CH |
| **Total** | **30 selected maps** | **194** | **20 locale variants** |

The source tree also retains manifests for `alcatraz`, `bismarck`, `dkm-tirpitz`,
`factory-collection`, and `grand-budapest-hotel`. They have no tasks in this selected
catalog and are not part of the 194-task count. A separately selected experimental
subset must be published with its own task IDs and must not silently replace this
roster.

## Assets and preparation

Each map manifest records the expected archive name, archive SHA-256, and source
and prepared-world fingerprints. Map archives are external inputs; public hosting
is not configured in this release. The redacted `example.invalid` source URLs are
not download links. Use a matching local archive with the
[Linux preparation command](linux-quickstart.md).

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
[release notes](anonymous-release.md). The catalog itself is unchanged.
