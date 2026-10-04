Выйдите из дома на «Спортивная улица, 100» и встретьтесь с одногруппником в «Win-Win». Вместе идите на занятия в «Университет Иннополис», после них посетите тренировку в «Спортивный комплекс «Иннополис»» и завершите день на выставке в «ArtSpace».

## Objective

You control a Minecraft player using screenshots and visible UI.

Complete the user's navigation task. The game continues running while you think
and act. You will receive screenshots and command results.

The player is in Adventure mode. You may use visible surroundings and the world
map.

Never leave the map boundaries. Falling beyond the map boundary can create a
large elevation difference that makes it impossible to return to the map and
continue the task. Actively identify boundaries by looking for abrupt terrain
cutoffs, exposed map undersides, voids, or sheer drops. Whenever you approach a
possible boundary, stop and carefully inspect the ground and surrounding route
before moving. Keep a safe distance from the edge, choose a route away from it,
and never walk, run, or jump blindly toward a suspected boundary.

## Route Completion

Visit each required waypoint in the order stated by the user.

Focus on navigation; narrative details are context.

When you reach a required waypoint, remain within 2.5m (blocks) of it, with no
more than 1.5m (blocks) of vertical difference, for 3 seconds so that it can be
detected.

It is strongly recommended that you keep the waypoint marker visible and
confirm its displayed distance is 2.5m or less for the full 3 seconds.

Use the same procedure at the final destination.

After visiting all required waypoints in order and reaching the final
destination, use `nav claim-done`.

## Harbor interface

Start with `nav start`; initial startup can take several minutes.

Run the `mcapi`, `xdo`, and shell commands described below inside the game
workspace using `nav exec 'COMMAND' --timeout 120`. This command waits for the
result. The timeout defaults to 30 seconds and accepts values greater than zero
up to 120 seconds; larger values are rejected. Action-workspace files persist during this trial; that workspace is
separate from `/workspace` in your agent container.

- `nav screenshot /workspace/view.png`: save the current game view for your image-viewing tool.
- `nav read-image /tmp/crop.png /workspace/crop.png`: retrieve a PNG/JPEG from the action workspace for your image-viewing tool.
- `nav state`: inspect the current player state.
- `nav exec 'mcapi --help' --timeout 120`: inspect the game command interface.
- `nav claim-done`: submit the completion claim described above.
- `nav result`: read the outcome when the task has ended.

## Minecraft Coordinates

Minecraft positions use `(x, y, z)`.

- `x` and `z` are the horizontal world axes.
- `y` is the vertical axis.
- World axes are independent of the player's current facing direction.

## Minecraft State

```bash
mcapi state
```

Returns the current Minecraft client state, including position, block position,
rotation, motion, health, food level, game mode, ground state, and GUI state.

## Movement

```bash
mcapi press <INPUT> [duration_seconds]
```

Available movement inputs:

- `MOVE_FORWARD`
- `MOVE_BACK`
- `MOVE_LEFT`
- `MOVE_RIGHT`
- `JUMP`
- `SNEAK`
- `SPRINT`

The duration is optional. Movement directions are relative to the direction the
player is currently facing.

## Camera Control

```bash
mcapi look --yaw <degrees> --pitch <degrees> --mode <relative|absolute>
```

In relative mode:

- Positive yaw turns left.
- Negative yaw turns right.
- Positive pitch looks down.
- Negative pitch looks up.

To look at a world position:

```bash
mcapi look-at <x> <y> <z>
```

## Block Interaction

```bash
mcapi right-click-block <x> <y> <z>
```

Performs a targeted right-click on the specified block position.

## Keyboard and Map UI

`xdo` is an automatically display-bound wrapper for `xdotool`. It follows the
standard `xdotool` command syntax.

You may use `xdo` for arbitrary keyboard and UI input, including controlling
the player with keyboard input instead of `mcapi press`.

Relevant map controls:

- Open the Xaero world map: `xdo key m`
- Close the world map or another screen: `xdo key Escape`
