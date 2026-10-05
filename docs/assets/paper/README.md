# Paper figures

These three figures are copied from the existing exports used by the manuscript.
The README introduces the benchmark with the environment overview and interaction
framework, then shows terrain examples alongside the task description. Image bytes
are unchanged; the combined payload is about 3 MB. Click an image to inspect the
full-resolution file.

| Figure | Source figure version | Original export | README asset |
| --- | --- | --- | --- |
| Environment coverage and task examples | `overview-integrated-v67` | `overview-preview.jpg` | [Overview](environment-overview.jpg) |
| Agent interaction and independent verification | `framework-draft-v12` | `overview.png` | [Framework](agent-framework.png) |
| Terrain, spatial constraints, and interactions | `terrain-interactions-20260925` | `terrain-interactions-en.png` | [Terrain](terrain-interactions.png) |

[manifest.json](manifest.json) records image dimensions, sizes, and SHA-256 hashes.
The source manifests and full manuscript remain outside this code release.

## Environment overview

The overview shows 30 maps: 20 outdoor and 10 indoor. Its example tasks are
`white-house-009` and `ueno-park-005`; both belong to the final 180-task catalog.
Their original-language instructions and waypoint sequences match the published
task entries.

The atlas uses presentation-camera captures. White House floors are separated
vertically for display, and its architectural reference is AI-generated. The
Ueno Minecraft panel is a top-down review view. Google Maps and Street View provide
geographic references; satellite imagery is credited to NASA. These references and
drawn routes illustrate the tasks and are not agent inputs or measured agent
trajectories. The continent totals cover 29 geographically located environments;
RMS Titanic has no fixed land location and remains part of the 30-map atlas.

## Interaction framework

The diagram shows the reference agent, asynchronous action execution, continuous
Minecraft simulation, and independent position-based verification. The command,
observation timing, and visited/pending destination sequence are schematic. They
do not report a measured trajectory or a model score. The block character is a
diagram illustration; the Minecraft screenshot is an existing game observation.

## Terrain and interactions

The eight examples illustrate uneven terrain, water boundaries, multilevel spaces,
narrow corridors, stairs, ladders, gates, and button-controlled doors. The figure
combines existing game observations and presentation captures from the manuscript;
it is not a frequency estimate or evidence that every task requires every type of
interaction.

The copied figures retain their original visual content and credits. This addition
does not modify tasks, runtime behavior, evaluation rules, or experimental results.
