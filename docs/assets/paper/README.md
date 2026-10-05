# Paper figures

These three figures use the PNG exports corresponding to the figures in the
September 29 manuscript source package (`paper-arxiv.tex`). Each corresponding PDF
was checked byte-for-byte against the source package. The PNG exports are copied
unchanged and total about 2.3 MB. Click an image to inspect the full-resolution file.

| Figure | Manuscript figure | Original PNG export | README asset |
| --- | --- | --- | --- |
| Environment coverage and task examples | `task-overview4.pdf` | `task-overview4-refined.png` | [Overview](environment-overview.png) |
| Agent interaction and independent verification | `agent_loop_overview.pdf` | `agent_loop_overview-refined.png` | [Framework](agent-framework.png) |
| Terrain, spatial constraints, and interactions | `terrain-interactions-en.pdf` | `terrain-interactions-en-compact.png` | [Terrain](terrain-interactions.png) |

[manifest.json](manifest.json) records the manuscript version, PDF hashes, PNG
export names, image dimensions, sizes, and SHA-256 hashes. The exports come from
`overview-refinement-20260926`; they replace the earlier drafts selected from
`current_main.tex`. The full manuscript remains outside this code release.

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
