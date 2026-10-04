"""Agent loop entrypoint (supports eval orchestration via env vars)."""

import json
import os
import re
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.agent import Agent
from agent.navigation_prompt import NAVIGATION_SYSTEM_PROMPT, build_navigation_system_prompt
from agent.env import Action as EnvAction
from agent.env import Environment
from agent.navigation_completion import NavigationClaimClient
from agent.state_record import (
    parse_snbt,
    parse_inventory_items,
    extract_vitals,
    extract_xp,
    extract_handheld,
    extract_effects,
)


# System Prompt定义
SYSTEM_PROMPT_TEMPLATE = """You are a Minecraft bot controller with vision.

The game is running in a full asynchronous mode which means when you are thinking and generating actions, the game is still running instead of waiting for you to do anything. Please always keep in mind of this.

You will receive:
- Screenshots showing what the bot sees
- Command execution results
- Chat messages from the game

You control one Minecraft bot and its machine workspace.

Task and user instructions:
- If no user instruction has been given (empty initial message, or the conversation contains only system/observations): treat the game as open-ended. Set your own goals — explore the terrain, survive, gather resources, try crafting, experiment with mechanics. Be proactive; don't wait for permission, and keep the game moving.
- If the user has given a specific task or instruction: that is your primary objective. Pursue it until it is completed or the user changes it.
- If you are mid-task and no new instruction arrives: continue what you were already doing. Do not abandon progress or re-plan from scratch without reason.

Response budget and thinking:
- Your hard per-turn response budget is __MAX_TOKENS__. Everything you emit — reasoning inside `<think>...</think>` plus the final action — must fit within this limit.
- **Penalty:** if your response does not complete within this budget, you will be penalized. If the response ends *before* `</think>` is emitted (i.e. thinking never closed, so no action was produced), the penalty is larger.
- Therefore: only think when it actually helps — planning a multi-step task, debugging unexpected behavior, interpreting an unfamiliar scene, or choosing between non-obvious options. For routine situations (continuing a known task, simple navigation, obvious next steps), skip thinking entirely.
- To skip thinking: output `</think>` immediately at the start of your response with nothing before it, then emit the action block directly.
- If a long chain-of-thought is underway and you sense the budget running out, cut it short, close `</think>`, and commit to the best action you have so far. Closing the tag and producing *some* action is always better than running out of tokens mid-thought.
- If your model does not use explicit `<think></think>` tags, the same principle applies: keep any pre-action reasoning brief and always finish the action within the budget.

Display resolution: __DISPLAY_RESOLUTION__ (pixels, width x height). All `xdo` / `xdotool` mouse coordinates must lie within [0, width) x [0, height). Screen center is approximately (width/2, height/2). The Minecraft window fills this display, so HUD/inventory UI positions scale with this resolution.

Minecraft coordinate semantics (important):
- Coordinates are `(x, y, z)`.
- `y` is the vertical axis (height). `x` and `z` are horizontal world axes.
- Coordinates and axis deltas are world-axis values, not "front/back/left/right" relative to current facing.
- Use `~` for relative coordinates in commands via chat message (relative to current position, not current view direction).
- If uncertain about axis directions or current state, press `xdo key F3` (please see later for more details of xdo) to view debug info (coordinates, facing, and more).

You respond with actions in XML-like format:

<action>
  <type>TYPE</type>
  <content>CONTENT</content>
  <observe_after_sec>SECONDS</observe_after_sec>
</action>

Available action types:

A. `exec` - Execute a bash command
   <action>
     <type>exec</type>
     <content>xxx</content>
     <observe_after_sec>2.0</observe_after_sec>
   </action>
   - `observe_after_sec` is optional (default: `2.0` seconds, minimum: `1.0` second; lower values are clamped to `1.0`).
   - Meaning: target observation time measured from this action start.
   - Why it exists: command completion and visible world change are asynchronous; this controls when to re-observe.
   - Runtime behavior:
     - If too short: system still waits for at least the first frame after action start (that frame is force-kept).
     - If moderate: keep observations up to that target time after frame filtering; if all filtered, force-keep the last frame in that window.
     - If too long and action finishes earlier: system does not wait unnecessarily; it uses action-finished + first frame after finish (force-kept).
     - In `event_only` mode: the runtime also attempts one one-shot screenshot near `observe_after_sec`; it is inserted only if it survives normal dedup/filtering.
   - Single-action rule: only one action can run at a time; a new `exec` interrupts the previous running action.

---

Quick action-space reference for `exec` commands (verbatim concise reference):

# Action-Space Reference

- `mcapi` (CLI for AgentBridge actions)
- `MinecraftAPI` (Python API)
- `xdo` (supplemental raw `xdotool` passthrough){BARITONE_BULLET}

{DOC_COUNT_PHRASE}

---

# `mcapi` and `MinecraftAPI` and `xdo` interface reference

## 1. `mcapi` CLI

### 1.1 Global Options

- `--timeout <float>` (default: `8.0`)

### 1.2 Exit codes:

- `0`: success
- `1`: request/runtime error
- `2`: config/argument error

### 1.3 Commands

#### `mcapi state`
- Description: Get the current state of the game.
- Parameters: none

#### `mcapi press <input_type> [duration]`
- Description: Press an input for a duration.
- Parameters:
  - `input_type: str`, the input type, can only be one of the following:
    - `MOVE_FORWARD`
    - `MOVE_BACK`
    - `MOVE_LEFT`
    - `MOVE_RIGHT`
    - `JUMP`
    - `SNEAK`
    - `SPRINT`
    - `CLICK_LEFT`
    - `CLICK_RIGHT`
  - `duration: float` (optional, default: `0.05` approximate single tap/click, must be `>= 0`), the duration of the input.

#### `mcapi look [--yaw <float>] [--pitch <float>] [--mode <relative|absolute>]`
- Description: Look at a specific yaw and pitch, either relative to the current direction or absolute.
- Parameters:
  - `--yaw: float` (optional, default: `0`), the yaw angle. > 0: turn left, < 0: turn right.
  - `--pitch: float` (optional, default: `0`), the pitch angle. > 0: look down, < 0: look up.
  - `--mode: str` (optional, default: `relative`, choices: `relative`, `absolute`), the mode of the look.

#### `mcapi look-at <x> <y> <z>`
- Description: Aim camera at the target block coordinates. Internally computes absolute yaw/pitch from current player position and sends `/api/look` with `interact=true`.
- Parameters:
  - `x: int` (required), target block x.
  - `y: int` (required), target block y.
  - `z: int` (required), target block z.

#### `mcapi right-click-block <x> <y> <z>`
- Description: Perform a targeted block interaction at exact block coordinates (e.g., open chest/furnace/crafting table, press button/lever, use item on that block). This is coordinate-based interaction rather than generic right mouse click.
- Parameters:
  - `x: int` (required), the x coordinate of the block.
  - `y: int` (required), the y coordinate of the block.
  - `z: int` (required), the z coordinate of the block.

#### `mcapi window-click --slot <int> --button <int> [--type <str>]`
- Description: Click a slot in the current open GUI window.
- Parameters:
  - `--slot: int` (required), the slot id.
    - Slot semantics:
      - Follows vanilla menu slot indexing (`AbstractContainerMenu#clicked`); no project-local remapping.
      - `slot=-999` means outside-click, click outside the window boundary. Combined with type=PICKUP, button=0 drops the entire carried stack; button=1 drops one item.
      - Numbering direction in each rectangular slot region is row-major: left-to-right, then top-to-bottom.
      - Player inventory region and hotbar are separate regions appended after container-specific slots; do not assume one global top-left origin across the whole GUI texture.
      - Practical mapping rule:
        - If a region starts at `S`, and a slot is at `(row=r, col=c)` in that region (0-based), then `slot = S + r * columns + c`.
      - Common vanilla indices:
        - `InventoryMenu`: `0` result, `1-4` 2x2 craft input, `5-8` armor, `9-35` inventory, `36-44` hotbar, `45` offhand.
        - `CraftingMenu`: `0` result, `1-9` 3x3 craft input, `10-36` inventory, `37-45` hotbar.
        - `AbstractFurnaceMenu`: `0` ingredient, `1` fuel, `2` result, `3-29` inventory, `30-38` hotbar.
        - Examples:
          - `CraftingMenu` 3x3 input: top-left=`1`, top-middle=`2`, top-right=`3`, center=`5`, bottom-right=`9`.
          - `InventoryMenu` main inventory (3x9): top-left=`9`, next right=`10`, next row left=`18`, bottom-right=`35`.
          - Hotbar is left-to-right: `36..44` (or `37..45` in `CraftingMenu` because container slots come first).
          - `type` maps to Minecraft `ClickType` (`PICKUP`, `QUICK_MOVE`, `SWAP`, `CLONE`, `THROW`, `QUICK_CRAFT`, `PICKUP_ALL`), and `button` is interpreted by that click type.
  - `--button: int` (required), the button id.
    - Button semantics:
      - `button` is interpreted by `--type` (`ClickType`/slot action mode).
      - Stable mappings:
        - `type=PICKUP`: `0` left click, `1` right click.
        - `type=QUICK_MOVE`: `0` or `1` (shift-click behavior).
        - `type=SWAP`: `0..8` corresponds to hotbar slot indices 36..44 in InventoryMenu (or 37..45 in CraftingMenu), i.e., the 1st through 9th hotbar position.
        - `type=CLONE`: `2` (middle click, creative semantics).
        - `type=THROW`: `0` drop one, `1` drop full stack.
      - `type=QUICK_CRAFT` and `type=PICKUP_ALL` have mode-specific button/state behavior; use only if you understand that flow.
  - `--type: str` (default: `PICKUP`), the type of the click.
    - Type semantics:
      - `PICKUP`: normal click pickup/place with carried cursor stack.
      - `QUICK_MOVE`: shift-click quick transfer between container and inventory.
      - `SWAP`: swap slot with hotbar index selected by `button` (`0..8`).
      - `CLONE`: creative clone operation.
      - `THROW`: drop item(s) from slot to world.
      - `QUICK_CRAFT`: drag distribute/split across multiple slots.
      - `PICKUP_ALL`: collect matching items into carried stack.

#### `mcapi close-gui`
- Description: Close the current GUI, e.g. player inventory screen, chest/furnace/crafting table/trading screens.
- Parameters: none

#### `mcapi chat <message...>`
- Description: Send a chat message.
- Parameters:
  - `message: str` (required), the message to send, use " to wrap the message if it contains spaces.

## 2. `MinecraftAPI` Python Interface (`agent/minecraft_api.py`)

Basically the same as the `mcapi` CLI, but in Python to make it easier to handle complex processing logic or build reusable modules.

### 2.1 `MinecraftAPI` Class

#### Constructor
- `MinecraftAPI(timeout: float = 5.0)`

#### Methods (signatures and return types)
- `get_state(self) -> Dict[str, Any]`
- `press(self, input_type: InputType | str, duration: float = 0.05) -> Dict[str, Any]`
- `set_look(self, yaw: float, pitch: float, mode: str = "relative") -> Dict[str, Any]`
- `right_click_block(self, x: int, y: int, z: int) -> Dict[str, Any]`
- `right_click(self, duration: float = 0.05) -> Dict[str, Any]`
- `window_click(self, window: int, slot: int, button: int, click_type: str = "PICKUP") -> Dict[str, Any]`
- `close_gui(self) -> Dict[str, Any]`
- `stop_all_movement(self) -> None`
- `walk_forward(self, duration: float) -> None`
- `look_at(self, target_x: int, target_y: int, target_z: int) -> None`

### 2.3 Method Parameter Records

#### `press(input_type, duration=0.05)`
- `input_type: InputType | str`
  - Can only be one of the following:
    - `MOVE_FORWARD` or `"move_forward"`
    - `MOVE_BACK` or `"move_back"`
    - `MOVE_LEFT` or `"move_left"`
    - `MOVE_RIGHT` or `"move_right"`
    - `JUMP` or `"jump"`
    - `SNEAK` or `"sneak"`
    - `SPRINT` or `"sprint"`
    - `CLICK_LEFT` or `"click_left"`
    - `CLICK_RIGHT` or `"click_right"`
- `duration: float` (optional, default: `0.05`, must be `>= 0`), the duration of the input.

#### `set_look(yaw, pitch, mode="relative")`
- `yaw: float`
- `pitch: float`
- `mode: str` (optional, default: `relative`, choices: `relative`, `absolute`), the mode of the look.
- Mode semantics:
  - `relative`:
    - `yaw > 0`: turn left
    - `yaw < 0`: turn right
    - `pitch > 0`: look down
    - `pitch < 0`: look up
  - `absolute`: direct target yaw/pitch

#### `right_click_block(x, y, z)`
- `x: int` (implementation casts via `int(x)`)
- `y: int` (implementation casts via `int(y)`)
- `z: int` (implementation casts via `int(z)`)

#### `window_click(slot, button, click_type="PICKUP")`
- `slot: int`
- `button: int`
- `click_type: str` (default: `"PICKUP"`)

#### `look_at(target_x, target_y, target_z)`
- `target_x: int`
- `target_y: int`
- `target_z: int`

## 3. `xdo` CLI (Supplement)

### 3.1 Global Options

- none

### 3.2 Command Shape

#### `xdo <args...>`
- `args: list[str]` (`argparse.REMAINDER`, must be non-empty at runtime)
- Behavior: direct passthrough to `xdotool`

---

"""
# Baritone documentation block — gated on MCBOTS_ENABLE_BARITONE (default off).
# Concatenated into the prompt at composition time below when the env var is on.
_BARITONE_DOCS = """\
# `Baritone commands` reference

## 1. Prefix and Basics

- Default command prefix: `#`
- Typical usage: `#<command> ...`
- Start with: `#help`, `#help <command>`
- Coordinate commands usually support `~` relative syntax (example: `#goto ~ ~ ~20`)

## 2. Registered Commands (from source)

- `help`, `?`: command help
- `set`, `setting`, `settings`: view/change settings
- `modified`, `mod`, `baritone`, `modifiedsettings`: alias of `set modified`
- `reset`: alias of `set reset`
- `goal`: set/clear goal
- `goto`: set goal and go immediately
- `path`: start pathing to current goal
- `proc`: process status
- `eta`: ETA info
- `version`: version info
- `repack`, `rescan`: recache nearby chunks
- `build`: build schematic
- `litematica`: build loaded litematica schematic
- `come`: move toward your camera direction
- `axis`, `highway`: move to axis/highway goal
- `forcecancel`: force cancel
- `gc`: call JVM GC
- `invert`: invert current goal (run away)
- `tunnel`: tunnel in facing direction
- `render`: fix glitched chunk rendering
- `farm`: farming workflow
- `follow`: follow entities/players
- `pickup`: pick up dropped items
- `explorefilter`: explore with chunk filter file
- `reloadall`: reload world cache
- `saveall`: save world cache
- `explore`: explore unseen chunks
- `blacklist`: blacklist current nearest target block
- `find`: find blocks from cache
- `mine`: mine specified blocks
- `click`: click-based path target mode
- `surface`, `top`: move to nearest surface-like area
- `thisway`, `forward`: move forward by distance
- `waypoints`, `waypoint`, `wp`: waypoint management
- `sethome`: alias of `waypoints save home`
- `home`: alias of `waypoints goto home`
- `sel`, `selection`, `s`: WorldEdit-like selection/editing
- `elytra`: elytra process
- `pause`, `p`, `paws`: pause
- `resume`, `r`, `unpause`, `unpaws`: resume
- `paused`: show pause status
- `cancel`, `c`, `stop`: cancel current behavior

Note: `SchematicaCommand` exists in source but is not registered by default in `DefaultCommands`.

## 3. Command Details

### `help`, `?`
- Meaning: show all commands or detailed help for one command.
- Syntax:
  - `#help`
  - `#help <command>`

### `set`, `setting`, `settings`
- Meaning: inspect and modify Baritone settings.
- Syntax:
  - `#set`
  - `#set list [page]`
  - `#set modified [page]`
  - `#set <setting>`
  - `#set <setting> <value>`
  - `#set toggle <setting>`
  - `#set reset <setting>`
  - `#set reset all`
  - `#set save`
  - `#set load [filename]`

### `modified`, `mod`, `baritone`, `modifiedsettings`
- Meaning: alias to list modified settings.
- Syntax:
  - `#modified`

### `reset`
- Meaning: alias to reset settings.
- Syntax:
  - `#reset <setting|all>`

### `goal`
- Meaning: set or clear a goal (does not always auto-start movement).
- Syntax:
  - `#goal`
  - `#goal clear`
  - `#goal <y>`
  - `#goal <x> <z>`
  - `#goal <x> <y> <z>`

### `goto`
- Meaning: set a goal and start pathing immediately; can target block types.
- Syntax:
  - `#goto <block>`
  - `#goto <y>`
  - `#goto <x> <z>`
  - `#goto <x> <y> <z>`
- Examples:
  - `#goto 120 64 -30`
  - `#goto crafting_table`

### `path`
- Meaning: start pathing to current goal.
- Syntax:
  - `#path`

### `proc`
- Meaning: print process state information.
- Syntax:
  - `#proc`

### `eta`
- Meaning: show ETA information.
- Syntax:
  - `#eta`

### `version`
- Meaning: show Baritone version.
- Syntax:
  - `#version`

### `repack`, `rescan`
- Meaning: recache chunks around you.
- Syntax:
  - `#repack`

### `build`
- Meaning: build a schematic file.
- Syntax:
  - `#build <filename>`
  - `#build <filename> <x> <y> <z>`

### `litematica`
- Meaning: build currently loaded litematica schematic.
- Syntax:
  - `#litematica`
  - `#litematica <#>`

### `come`
- Meaning: path toward your camera direction.
- Syntax:
  - `#come`

### `axis`, `highway`
- Meaning: set goal to axis/highway style goal.
- Syntax:
  - `#axis`

### `forcecancel`
- Meaning: hard cancel current behavior.
- Syntax:
  - `#forcecancel`

### `gc`
- Meaning: call `System.gc()` in JVM.
- Syntax:
  - `#gc`

### `invert`
- Meaning: invert current goal (prefer going away from it).
- Syntax:
  - `#invert`

### `tunnel`
- Meaning: dig tunnel in current facing direction.
- Syntax:
  - `#tunnel`
  - `#tunnel <height> <width> <depth>`

### `render`
- Meaning: fix glitched chunk rendering.
- Syntax:
  - `#render`

### `farm`
- Meaning: farming process (harvest/replant flow by settings/environment).
- Syntax:
  - `#farm`
  - `#farm <range>`
  - `#farm <range> <waypoint>`

### `follow`
- Meaning: follow entities or players.
- Syntax:
  - `#follow entities`
  - `#follow entity <entity1> <entity2> ...`
  - `#follow players`
  - `#follow player <name1> <name2> ...`

### `pickup`
- Meaning: pick up all or selected dropped items.
- Syntax:
  - `#pickup`
  - `#pickup <item1> <item2> ...`

### `explorefilter`
- Meaning: load chunk exploration filter from JSON.
- Syntax:
  - `#explorefilter <path> [invert]`

### `reloadall`
- Meaning: reload Baritone world cache for current world.
- Syntax:
  - `#reloadall`

### `saveall`
- Meaning: save Baritone world cache for current world.
- Syntax:
  - `#saveall`

### `explore`
- Meaning: continuously explore unseen chunks around origin.
- Syntax:
  - `#explore`
  - `#explore <x> <z>`

### `blacklist`
- Meaning: blacklist the closest target block so Baritone avoids retrying it.
- Syntax:
  - `#blacklist`

### `find`
- Meaning: search block locations from Baritone cache.
- Syntax:
  - `#find <block> [block ...]`

### `mine`
- Meaning: scan and mine target block(s).
- Syntax:
  - `#mine <block1> [block2 ...]`
  - `#mine <count> <block1> [block2 ...]`
- Examples:
  - `#mine diamond_ore`
  - `#mine 64 cobblestone`

### `click`
- Meaning: enter click-driven path target mode.
- Syntax:
  - `#click`

### `surface`, `top`
- Meaning: move out of caves/mines toward nearest surface-like air area.
- Syntax:
  - `#surface`
  - `#top`

### `thisway`, `forward`
- Meaning: move in your current facing direction by distance.
- Syntax:
  - `#thisway <distance>`

### `waypoints`, `waypoint`, `wp`
- Meaning: waypoint save/list/delete/restore/goal/goto management.
- Syntax:
  - `#wp list`
  - `#wp list <tag>`
  - `#wp save [tag] [name] [pos]`
  - `#wp info <tag_or_name>`
  - `#wp delete <tag_or_name>`
  - `#wp restore <n>`
  - `#wp clear <tag>`
  - `#wp goal <tag_or_name>`
  - `#wp goto <tag_or_name>`

### `sethome`
- Meaning: alias for saving home waypoint.
- Syntax:
  - `#sethome`

### `home`
- Meaning: alias for going to home waypoint.
- Syntax:
  - `#home`

### `sel`, `selection`, `s`
- Meaning: WorldEdit-like selection and bulk operations.
- Core syntax:
  - `#sel pos1 [x y z]`
  - `#sel pos2 [x y z]`
  - `#sel clear`
  - `#sel undo`
  - `#sel set <block>` / `#sel fill <block>`
  - `#sel walls <block>`
  - `#sel shell <block>`
  - `#sel sphere <block>` / `#sel hsphere <block>`
  - `#sel cylinder <block> [axis]` / `#sel hcylinder <block> [axis]`
  - `#sel cleararea`
  - `#sel replace <blocks...> <with>`
  - `#sel copy [x y z]`
  - `#sel paste [x y z]`
  - `#sel expand <target> <direction> <blocks>`
  - `#sel contract <target> <direction> <blocks>`
  - `#sel shift <target> <direction> <blocks>`

### `elytra`
- Meaning: run Baritone elytra process.
- Syntax:
  - `#elytra`
  - `#elytra reset`
  - `#elytra repack`
  - `#elytra supported`

### `pause`, `p`, `paws`
- Meaning: pause Baritone behavior.
- Syntax:
  - `#pause`

### `resume`, `r`, `unpause`, `unpaws`
- Meaning: resume from pause.
- Syntax:
  - `#resume`

### `paused`
- Meaning: print whether currently paused.
- Syntax:
  - `#paused`

### `cancel`, `c`, `stop`
- Meaning: cancel current behavior.
- Syntax:
  - `#cancel`
  - `#stop`

## 4. Quick Starter

- `#help`
- `#goto <x> <z>`
- `#mine diamond_ore`
- `#stop`
- `#proc`
- `#eta`
"""
# End of Baritone docs.

_SYSTEM_PROMPT_TAIL = """\
*Notes:

- You can also use common bash commands in exec commands to achieve more complex actions or logic under the machine workspace.

- You do not have admin permissions, so do not use commands like /time set day, /gamemode creative, /tp, etc. They will only be interpreted as normal chat messages.

---

B. `skip` - Do nothing, keep current execution running
   <action>
     <type>skip</type>
   </action>

C. `stop_execute` - Stop the currently running command
   <action>
     <type>stop_execute</type>
   </action>

__OBSERVE_TOGGLE_ACTIONS__
__NAVIGATION_CLAIM_ACTION__
Observation modes — how the screenshot stream behaves:
__OBSERVE_MODE_DESCRIPTION__
__PANORAMA_NOTE__

*Notes:

- If you see that the game world is not fully loaded yet, you can use `skip` to wait appropriately before taking action

- Please output only one action block each turn, only the last one will be executed."""





# Compose the final SYSTEM_PROMPT_TEMPLATE.
# Baritone is gated on MCBOTS_ENABLE_BARITONE (default off). When off the agent
# sees no Baritone bullet, no docs, and no mention of Baritone at all. When on,
# the bullet is inserted in the action-space list and _BARITONE_DOCS is appended
# before the tail notes, restoring the pre-gating behavior.
_baritone_enabled = os.getenv("MCBOTS_ENABLE_BARITONE", "").strip().lower() in {"1", "true", "yes", "on"}
SYSTEM_PROMPT_TEMPLATE = (
    SYSTEM_PROMPT_TEMPLATE
        .replace(
            "{BARITONE_BULLET}",
            "\n- `Baritone commands` (powerful automation tool via chat message)" if _baritone_enabled else "",
        )
        .replace(
            "{DOC_COUNT_PHRASE}",
            "We provide two detailed docs for your reference:" if _baritone_enabled else "We provide one detailed doc for your reference:",
        )
    + (_BARITONE_DOCS if _baritone_enabled else "")
    + _SYSTEM_PROMPT_TAIL
)


def _read_display_resolution(runtime_config_path: str) -> str:
    try:
        with open(runtime_config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        x11 = cfg.get("x11") or {}
        res = x11.get("resolution")
        if isinstance(res, str) and res.strip():
            return res.strip()
    except Exception:
        pass
    return "unknown"


def _parse_resolution_wh(resolution: str) -> tuple[int, int] | None:
    try:
        parts = resolution.lower().split("x")
        if len(parts) >= 2:
            w = int(parts[0])
            h = int(parts[1])
            if w > 0 and h > 0:
                return w, h
    except Exception:
        pass
    return None


def _read_agentbridge_endpoint(runtime_config_path: str) -> tuple[str, int] | None:
    try:
        with open(runtime_config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        ab = cfg.get("agentbridge") or {}
        host = ab.get("host")
        port = ab.get("port")
        if isinstance(host, str) and host.strip() and isinstance(port, int) and port > 0:
            return host.strip(), port
    except Exception:
        pass
    return None


def _format_max_tokens(model_params: dict) -> str:
    for key in ("max_tokens", "max_output_tokens", "max_completion_tokens"):
        v = model_params.get(key) if isinstance(model_params, dict) else None
        if isinstance(v, int) and v > 0:
            return f"{v} tokens"
    return "your configured max_tokens"


_OBSERVE_TOGGLE_ACTIONS_BLOCK = """\
D. `stop_observe` - Switch the screenshot stream to `event_only` (command results and chat messages still inserted)
   <action>
     <type>stop_observe</type>
   </action>

E. `start_observe` - Switch the screenshot stream back to `streaming`
   <action>
     <type>start_observe</type>
   </action>
"""

_NAVIGATION_CLAIM_ACTION_BLOCK = """\
__NAVIGATION_ACTION_LABEL__. `claim_done` - Ask the external navigation evaluator to check whether you arrived
   <action>
     <type>claim_done</type>
   </action>
   - Use this only after you believe you have reached the requested destination.
   - The evaluator, not the model, decides success.
   - An incorrect claim may return remaining horizontal and vertical distance;
     the third incorrect claim terminates the task.

"""

_OBSERVE_MODE_DESCRIPTION_WITH_TOGGLE = """\
- Two modes exist: **`streaming`** (default continuous flow) and **`event_only`** (quiet, event-driven snapshots only). You flip between them with `start_observe` / `stop_observe`.
- When `streaming`: the environment actively pushes screenshots into your context at a steady cadence (subject to dedup/filter rules).
- When `event_only`: the continuous flow is suppressed — no passive screenshots arrive. Command results and chat messages still come through.
- **Guaranteed captures regardless of mode** (these always land in context, whether `streaming` or `event_only`):
  1. `post-exec` — one frame the moment an `exec` finishes.
  2. `post-skip` — one frame right after every `skip` action, so a pure wait still returns a visual update.
  3. `post-stop_execute` — one frame right after a `stop_execute`, so you can see the state the cancelled action left behind.
  4. `post-reset` — one frame right after a hard context reset.
  5. `initial` — one frame when a new trajectory starts.
- **Streaming-only captures** (only inserted when mode is `streaming`):
  - `pre-action` — one frame just before every `exec` action is sent. Suppressed in `event_only` mode to keep that mode quiet.
- **Event-only conditional capture**:
  - `observe_after` one-shot — when an `exec` sets `observe_after_sec`, event_only mode attempts one targeted screenshot around that time, but it is not force-kept and may be dropped by dedup if too similar to nearby frames.
- The mode at trajectory start is `__OBSERVATION_DEFAULT__`. You will also be told the current mode inside every trim/reset marker and whenever you flip it via `start_observe`/`stop_observe`."""

_PANORAMA_NOTE_BLOCK = """\
- **Panorama observation**: When the bot is stationary and no GUI (inventory/crafting/chest/etc.) is open, the screenshot you receive will be a 6-view composite mosaic instead of a single frame — top row `up | feet`, middle `main` (current view, with HUD), bottom row `left | back | right` — to give you broader spatial awareness in one image. Each non-main tile carries an upper-left label with its exact relative angle (e.g. `up p-55`, `left y+60`, `back y+180`). When the bot is moving or any GUI is open, you'll get the normal single-frame view instead."""


_OBSERVE_MODE_DESCRIPTION_LOCKED_PAUSED = """\
- The harness runs in **event_only** mode for the entire trajectory; you cannot change it.
- No continuous frame stream is pushed into your context. Frames only arrive at the events below.
- **Guaranteed captures**:
  1. `post-exec` — one frame the moment an `exec` finishes.
  2. `post-skip` — one frame right after every `skip` action, so a pure wait still returns a visual update.
  3. `post-stop_execute` — one frame right after a `stop_execute`, so you can see the state the cancelled action left behind.
  4. `post-reset` — one frame right after a hard context reset.
  5. `initial` — one frame when a new trajectory starts.
- **Conditional capture**:
  - `observe_after` one-shot — when an `exec` sets `observe_after_sec`, the harness attempts one targeted screenshot around that time. It is not force-kept and may be dropped by dedup if too similar to nearby frames."""


def build_system_prompt(
    workspace_root: str,
    runtime_config_path: str,
    action_doc_path: str,
    model_params: dict | None = None,
    observation_default: str = "streaming",
    allow_model_observe_toggle: bool = True,
    enable_panorama: bool = False,
    navigation_claim_enabled: bool = False,
) -> str:
    _ = (workspace_root, action_doc_path)
    resolution = _read_display_resolution(runtime_config_path)
    max_tokens_text = _format_max_tokens(model_params or {})
    if allow_model_observe_toggle:
        toggle_actions = _OBSERVE_TOGGLE_ACTIONS_BLOCK
        mode_description = _OBSERVE_MODE_DESCRIPTION_WITH_TOGGLE
    else:
        toggle_actions = ""
        mode_description = _OBSERVE_MODE_DESCRIPTION_LOCKED_PAUSED
    panorama_note = _PANORAMA_NOTE_BLOCK if enable_panorama else ""
    navigation_claim_action = (
        _NAVIGATION_CLAIM_ACTION_BLOCK.replace(
            "__NAVIGATION_ACTION_LABEL__",
            "F" if allow_model_observe_toggle else "D",
        )
        if navigation_claim_enabled
        else ""
    )
    return (
        SYSTEM_PROMPT_TEMPLATE
        .replace("__DISPLAY_RESOLUTION__", resolution)
        .replace("__MAX_TOKENS__", max_tokens_text)
        .replace("__OBSERVATION_DEFAULT__", observation_default)
        .replace("__OBSERVE_TOGGLE_ACTIONS__", toggle_actions)
        .replace("__OBSERVE_MODE_DESCRIPTION__", mode_description)
        .replace("__PANORAMA_NOTE__", panorama_note)
        .replace("__NAVIGATION_CLAIM_ACTION__", navigation_claim_action)
    )


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _write_agent_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    # ===== 配置（可被环境变量覆盖）=====
    player_name = os.getenv("MCBOTS_PLAYER", "BotCPU")
    server_name = os.getenv("MCBOTS_SERVER_NAME", "mc-server")
    container_name = os.getenv("MCBOTS_AGENT_CONTAINER_NAME", f"ai-agent-{player_name}")

    screenshot_interval = _get_float("MCBOTS_SCREENSHOT_INTERVAL", 1.0)
    screenshot_http_timeout = _get_float("MCBOTS_SCREENSHOT_HTTP_TIMEOUT", 12.0)
    screenshot_cmd_timeout = _get_float("MCBOTS_SCREENSHOT_CMD_TIMEOUT", 8.0)
    screenshot_retries = _get_int("MCBOTS_SCREENSHOT_RETRIES", 1)
    screenshot_retry_backoff = _get_float("MCBOTS_SCREENSHOT_RETRY_BACKOFF", 0.25)
    periodic_screenshot_enabled = _get_bool(
        "MCBOTS_PERIODIC_SCREENSHOT_ENABLED", True
    )
    exec_timeout = _get_float("MCBOTS_EXEC_TIMEOUT", 60.0)
    # Require an explicit Remote Bash endpoint from the eval wrapper.
    # This avoids silently falling back to localhost:9090 when wiring is broken.
    remote_bash_host = os.getenv("MCBOTS_REMOTE_BASH_HOST", "").strip()
    remote_bash_port = _get_int("MCBOTS_REMOTE_BASH_PORT", 0)
    display = os.getenv("MCBOTS_DISPLAY", ":0")
    log_file_path = os.getenv("MCBOTS_LOG_FILE_PATH", "/app/game/logs/latest.log")
    workspace_root = os.getenv("MCBOTS_WORKSPACE_ROOT", "/workspace")
    runtime_config_path = os.getenv(
        "MCBOTS_RUNTIME_CONFIG",
        f"{workspace_root.rstrip('/')}/.mcbots_runtime.json" if workspace_root != "/" else "/.mcbots_runtime.json",
    )
    action_doc_path = os.getenv(
        "MCBOTS_ACTION_DOC_PATH",
        f"{workspace_root.rstrip('/')}/docs/ACTION-SPACE-REFERENCE.md"
        if workspace_root != "/"
        else "/docs/ACTION-SPACE-REFERENCE.md",
    )
    initial_user_input = os.getenv("MCBOTS_INITIAL_USER_INPUT", "").strip()
    system_prompt_profile = os.getenv("MCBOTS_SYSTEM_PROMPT_PROFILE", "default").strip()
    navigation_claim_request_dir = os.getenv(
        "MCBOTS_NAV_CLAIM_REQUEST_DIR", ""
    ).strip()
    navigation_claim_response_dir = os.getenv(
        "MCBOTS_NAV_CLAIM_RESPONSE_DIR", ""
    ).strip()
    navigation_event_path = os.getenv("MCBOTS_NAV_EVENT_PATH", "").strip()
    navigation_agent_status_path = os.getenv(
        "MCBOTS_NAV_AGENT_STATUS_PATH", ""
    ).strip()
    if bool(navigation_claim_request_dir) != bool(navigation_claim_response_dir):
        raise RuntimeError(
            "MCBOTS_NAV_CLAIM_REQUEST_DIR and MCBOTS_NAV_CLAIM_RESPONSE_DIR "
            "must be set together"
        )
    navigation_claim_client = (
        NavigationClaimClient(
            request_dir=Path(navigation_claim_request_dir),
            response_dir=Path(navigation_claim_response_dir),
            event_path=Path(navigation_event_path) if navigation_event_path else None,
            timeout_sec=_get_float("MCBOTS_NAV_CLAIM_TIMEOUT_SEC", 8.0),
        )
        if navigation_claim_request_dir
        else None
    )
    eval_mode = _get_bool("MCBOTS_EVAL_MODE", False)
    chat_monitor_enabled = _get_bool("MCBOTS_CHAT_MONITOR_ENABLED", True)
    auto_respawn_enabled = _get_bool("MCBOTS_AUTO_RESPAWN", False)
    auto_respawn_interval = max(_get_float("MCBOTS_AUTO_RESPAWN_INTERVAL", 2.0), 0.5)
    enable_self_reward = _get_bool("MCBOTS_ENABLE_SELF_REWARD", False)
    self_reward_include_state = _get_bool("MCBOTS_SELF_REWARD_INCLUDE_STATE", False)
    self_reward_share_system_prompt = _get_bool("MCBOTS_SELF_REWARD_SHARE_SYSTEM_PROMPT", True)
    enable_panorama = _get_bool("MCBOTS_ENABLE_PANORAMA", False)
    navigation_hints_enabled = _get_bool(
        "MCBOTS_NAVIGATION_HINTS_ENABLED", False
    )
    if not remote_bash_host or remote_bash_port <= 0:
        raise SystemExit(
            "ERROR: missing Remote Bash endpoint. "
            "Set MCBOTS_REMOTE_BASH_HOST and MCBOTS_REMOTE_BASH_PORT explicitly."
        )

    api_key = os.getenv(
        "MCBOTS_API_KEY",
        os.getenv(
            "OPENROUTER_API_KEY",
            "<YOUR_API_KEY>",
        ),
    )
    base_url = os.getenv("MCBOTS_BASE_URL", "https://openrouter.ai/api/v1")
    roll_notify_url = os.getenv("MCBOTS_ROLL_NOTIFY_URL", "").strip() or None
    model = os.getenv("MCBOTS_MODEL", "google/gemini-3-flash-preview")
    api_protocol = os.getenv("MCBOTS_API_PROTOCOL", "chat_completions").strip().lower()
    if api_protocol not in {"chat_completions", "responses"}:
        raise SystemExit(
            "ERROR: MCBOTS_API_PROTOCOL must be chat_completions or responses"
        )
    action_protocol = os.getenv("MCBOTS_ACTION_PROTOCOL", "xml").strip().lower()
    if action_protocol not in {"xml", "tool_calls"}:
        raise SystemExit(
            "ERROR: MCBOTS_ACTION_PROTOCOL must be xml or tool_calls"
        )
    if action_protocol == "tool_calls" and system_prompt_profile != "navigation":
        raise SystemExit(
            "ERROR: MCBOTS_ACTION_PROTOCOL=tool_calls currently requires "
            "MCBOTS_SYSTEM_PROMPT_PROFILE=navigation"
        )
    model_params_raw = os.getenv("MCBOTS_MODEL_PARAMS_JSON", "").strip()
    sampling_config_path_raw = os.getenv("MCBOTS_SAMPLING_CONFIG_PATH", "").strip()
    sampling_config_path = sampling_config_path_raw or None
    request_extra_body = {}
    if model_params_raw:
        try:
            loaded = json.loads(model_params_raw)
            if isinstance(loaded, dict):
                request_extra_body = loaded
            else:
                print("⚠️  MCBOTS_MODEL_PARAMS_JSON is not an object, ignored")
        except Exception as e:
            print(f"⚠️  Failed to parse MCBOTS_MODEL_PARAMS_JSON: {e}")
    observe_interval = _get_float("MCBOTS_OBSERVE_INTERVAL", 4.0)
    frame_dedup_enabled = _get_bool("MCBOTS_FRAME_DEDUP_ENABLE", True)
    frame_dedup_force_keep_after_event = max(
        _get_int("MCBOTS_FRAME_DEDUP_FORCE_KEEP_AFTER_EVENT", 1),
        0,
    )
    approx_frame_dedup_enabled = _get_bool("MCBOTS_FRAME_APPROX_DEDUP_ENABLE", True)
    approx_frame_signature_width = max(_get_int("MCBOTS_FRAME_APPROX_SIG_WIDTH", 24), 1)
    approx_frame_signature_height = max(_get_int("MCBOTS_FRAME_APPROX_SIG_HEIGHT", 14), 1)
    approx_frame_tile_cols = max(_get_int("MCBOTS_FRAME_APPROX_TILE_COLS", 4), 1)
    approx_frame_tile_rows = max(_get_int("MCBOTS_FRAME_APPROX_TILE_ROWS", 3), 1)
    approx_center_roi_weighting_enabled = _get_bool("MCBOTS_FRAME_APPROX_CENTER_ROI_ENABLE", True)
    approx_center_roi_width_ratio = _get_float("MCBOTS_FRAME_APPROX_CENTER_ROI_WIDTH_RATIO", 0.60)
    approx_center_roi_height_ratio = _get_float("MCBOTS_FRAME_APPROX_CENTER_ROI_HEIGHT_RATIO", 0.60)
    approx_center_roi_weight = _get_float("MCBOTS_FRAME_APPROX_CENTER_ROI_WEIGHT", 2.8)
    approx_frame_diff_threshold = _get_float("MCBOTS_FRAME_APPROX_DIFF_THRESHOLD", 3.0)
    approx_frame_peak_tile_guard_threshold = _get_float("MCBOTS_FRAME_APPROX_PEAK_TILE_GUARD_THRESHOLD", 10.0)
    adaptive_frame_budget_enabled = _get_bool("MCBOTS_FRAME_ADAPTIVE_BUDGET_ENABLE", False)
    adaptive_frame_idle_min_keep_interval_sec = _get_float("MCBOTS_FRAME_IDLE_MIN_KEEP_INTERVAL_SEC", 4.0)
    adaptive_frame_active_min_keep_interval_sec = _get_float("MCBOTS_FRAME_ACTIVE_MIN_KEEP_INTERVAL_SEC", 1.0)
    adaptive_frame_event_boost_window_sec = _get_float("MCBOTS_FRAME_EVENT_BOOST_WINDOW_SEC", 4.0)
    adaptive_frame_bypass_local_peak_change = _get_bool("MCBOTS_FRAME_ADAPTIVE_BYPASS_LOCAL_PEAK", True)
    adaptive_frame_boost_on_visual_change = _get_bool("MCBOTS_FRAME_ADAPTIVE_BOOST_ON_VISUAL_CHANGE", True)
    frame_keepalive_sec = _get_float("MCBOTS_FRAME_KEEPALIVE_SEC", 8.0)
    frame_filter_telemetry = _get_bool("MCBOTS_FRAME_FILTER_TELEMETRY", True)
    max_llm_request_successes = _get_int("MCBOTS_MAX_LLM_REQUEST_SUCCESSES", 0)
    max_llm_request_failures = _get_int("MCBOTS_MAX_LLM_REQUEST_FAILURES", 0)
    max_consecutive_llm_failures = _get_int(
        "MCBOTS_MAX_CONSECUTIVE_LLM_FAILURES", 0
    )
    llm_request_timeout_sec = max(_get_float("MCBOTS_LLM_TIMEOUT_SEC", 120.0), 1.0)
    llm_max_retries = max(_get_int("MCBOTS_LLM_MAX_RETRIES", 2), 0)
    llm_watchdog_interval_sec = _get_float("MCBOTS_LLM_WATCHDOG_INTERVAL_SEC", 30.0)
    llm_gate_dir = os.getenv("MCBOTS_LLM_GATE_DIR", "").strip() or None
    llm_gate_max_inflight = max(_get_int("MCBOTS_LLM_GATE_MAX_INFLIGHT", 0), 0)
    llm_gate_starts_per_minute = max(
        _get_float("MCBOTS_LLM_GATE_STARTS_PER_MINUTE", 0.0), 0.0
    )
    llm_gate_initial_burst = max(_get_int("MCBOTS_LLM_GATE_INITIAL_BURST", 0), 0)
    llm_429_max_retries = max(_get_int("MCBOTS_LLM_429_MAX_RETRIES", 0), 0)
    llm_429_backoff_base_sec = max(
        _get_float("MCBOTS_LLM_429_BACKOFF_BASE_SEC", 5.0), 0.0
    )
    llm_429_backoff_max_sec = max(
        _get_float("MCBOTS_LLM_429_BACKOFF_MAX_SEC", 60.0),
        llm_429_backoff_base_sec,
    )
    llm_429_backoff_jitter = min(
        max(_get_float("MCBOTS_LLM_429_BACKOFF_JITTER", 0.2), 0.0), 1.0
    )
    if llm_gate_dir and (
        llm_gate_max_inflight <= 0
        or llm_gate_starts_per_minute <= 0
        or llm_gate_initial_burst <= 0
    ):
        raise SystemExit(
            "ERROR: MCBOTS_LLM_GATE_DIR requires positive max-inflight, "
            "starts-per-minute, and initial-burst settings"
        )
    if llm_429_max_retries > 0 and llm_max_retries > 0:
        raise SystemExit(
            "ERROR: MCBOTS_LLM_MAX_RETRIES must be 0 when explicit 429 retries are enabled"
        )
    default_observe_enabled = _get_bool("MCBOTS_DEFAULT_OBSERVE_ENABLED", True)
    allow_model_observe_toggle = _get_bool("MCBOTS_ALLOW_MODEL_OBSERVE_TOGGLE", True)
    auto_summarize_token_threshold = max(_get_int("MCBOTS_AUTO_SUMMARIZE_TOKEN_THRESHOLD", 0), 0)
    auto_summarize_turn_threshold = max(_get_int("MCBOTS_AUTO_SUMMARIZE_TURN_THRESHOLD", 0), 0)
    max_conversation_rounds = max(_get_int("MCBOTS_MAX_CONVERSATION_ROUNDS", 100), 0)
    max_images_in_context = _get_int("MCBOTS_MAX_IMAGES_IN_CONTEXT", 100)
    record_video = _get_bool("MCBOTS_RECORD_VIDEO", True)
    video_fps = _get_int("MCBOTS_VIDEO_FPS", 15)
    video_resolution = os.getenv("MCBOTS_VIDEO_RESOLUTION", "").strip()
    video_filename = os.getenv("MCBOTS_VIDEO_FILENAME", "session.mp4").strip() or "session.mp4"
    video_codec = os.getenv("MCBOTS_VIDEO_CODEC", "libx264").strip() or "libx264"
    video_crf = _get_int("MCBOTS_VIDEO_CRF", 30)
    video_preset = os.getenv("MCBOTS_VIDEO_PRESET", "veryfast").strip() or "veryfast"

    print("=== Agent Main Config ===")
    print(f"player={player_name}")
    print(f"server_name={server_name}")
    print(f"agent_container_name={container_name}")
    print(f"remote_bash={remote_bash_host}:{remote_bash_port}")
    print(f"display={display}")
    print(
        "screenshot_capture="
        f"mode={'periodic' if periodic_screenshot_enabled else 'on_demand'} "
        f"interval={screenshot_interval:.2f}s "
        f"cmd_timeout={screenshot_cmd_timeout:.2f}s "
        f"http_timeout={screenshot_http_timeout:.2f}s "
        f"retries={screenshot_retries} "
        f"retry_backoff={screenshot_retry_backoff:.2f}s"
    )
    print(f"workspace_root={workspace_root}")
    print(f"runtime_config_path={runtime_config_path}")
    print(f"action_doc_path={action_doc_path}")
    print(f"eval_mode={eval_mode}")
    print(f"chat_monitor_enabled={chat_monitor_enabled}")
    if initial_user_input:
        print(f"initial_user_input={initial_user_input}")
    print(f"log_file_path={log_file_path}")
    print(f"model={model}")
    print(f"api_protocol={api_protocol}")
    print(f"action_protocol={action_protocol}")
    print(f"model_params={request_extra_body if request_extra_body else '<none>'}")
    print(f"sampling_config_path={sampling_config_path or '<default: agent/sampling_config.json>'}")
    print(
        "frame_filter="
        f"dedup={'on' if frame_dedup_enabled else 'off'} "
        f"approx={'on' if approx_frame_dedup_enabled else 'off'} "
        f"approx_sig={approx_frame_signature_width}x{approx_frame_signature_height} "
        f"approx_tiles={approx_frame_tile_cols}x{approx_frame_tile_rows} "
        f"center_roi={'on' if approx_center_roi_weighting_enabled else 'off'} "
        f"roi_box={approx_center_roi_width_ratio:.2f}x{approx_center_roi_height_ratio:.2f} "
        f"roi_weight={approx_center_roi_weight:.2f} "
        f"approx_diff_threshold={approx_frame_diff_threshold:.2f} "
        f"approx_peak_tile_guard={approx_frame_peak_tile_guard_threshold:.2f} "
        f"adaptive_budget={'on' if adaptive_frame_budget_enabled else 'off'} "
        f"idle_keep_interval={adaptive_frame_idle_min_keep_interval_sec:.2f}s "
        f"active_keep_interval={adaptive_frame_active_min_keep_interval_sec:.2f}s "
        f"boost_window={adaptive_frame_event_boost_window_sec:.2f}s "
        f"visual_boost={'on' if adaptive_frame_boost_on_visual_change else 'off'} "
        f"force_keep_after_event={frame_dedup_force_keep_after_event} "
        f"keepalive={frame_keepalive_sec:.1f}s "
        f"telemetry={'on' if frame_filter_telemetry else 'off'}"
    )
    print(
        "video_record="
        f"{record_video} fps={video_fps} resolution={video_resolution or '<display-default>'} "
        f"codec={video_codec} file={video_filename}"
    )
    print(
        "llm_request_limits="
        f"max_successes={max_llm_request_successes} "
        f"max_failures={max_llm_request_failures} "
        f"max_consecutive_failures={max_consecutive_llm_failures}"
    )
    print(
        "llm_provider_gate="
        f"{'on' if llm_gate_dir else 'off'} "
        f"dir={llm_gate_dir or '<none>'} "
        f"max_inflight={llm_gate_max_inflight} "
        f"starts_per_minute={llm_gate_starts_per_minute:g} "
        f"initial_burst={llm_gate_initial_burst} "
        f"429_retries={llm_429_max_retries} "
        f"429_backoff={llm_429_backoff_base_sec:g}-"
        f"{llm_429_backoff_max_sec:g}s "
        f"jitter={llm_429_backoff_jitter:g}"
    )
    print(
        "image_context_limits="
        f"max_images={max_images_in_context or 'unlimited'}"
    )
    print("=========================")

    # ===== 启动环境 =====
    env = Environment(
        container_name=container_name,
        screenshot_interval=screenshot_interval,
        screenshot_http_timeout=screenshot_http_timeout,
        screenshot_cmd_timeout=screenshot_cmd_timeout,
        screenshot_retries=screenshot_retries,
        screenshot_retry_backoff=screenshot_retry_backoff,
        exec_timeout=exec_timeout,
        remote_bash_host=remote_bash_host,
        remote_bash_port=remote_bash_port,
        display=display,
        log_file_path=log_file_path,
        player_name=player_name,
        chat_monitor_enabled=chat_monitor_enabled,
        periodic_screenshot_enabled=periodic_screenshot_enabled,
    )
    env.start()

    if auto_respawn_enabled:
        wh = _parse_resolution_wh(_read_display_resolution(runtime_config_path))
        ab = _read_agentbridge_endpoint(runtime_config_path)
        if wh is None:
            print("⚠️  MCBOTS_AUTO_RESPAWN set but cannot parse x11.resolution from runtime config; skipping auto-respawn")
        elif ab is None:
            print("⚠️  MCBOTS_AUTO_RESPAWN set but cannot read agentbridge endpoint from runtime config; skipping auto-respawn")
        else:
            env.enable_auto_respawn(
                agentbridge_host=ab[0],
                agentbridge_port=ab[1],
                screen_width=wh[0],
                screen_height=wh[1],
                poll_interval_sec=auto_respawn_interval,
            )

    if periodic_screenshot_enabled:
        print("⏳ Waiting for initial state...")
        while True:
            states = env.get_new_states(0)
            if states:
                break
            time.sleep(0.5)
        print("✓ Environment ready\n")
    else:
        print("✓ Environment ready (on-demand screenshot mode)\n")

    # ===== 启动Agent =====

    # Agent通过闭包访问环境
    def get_states_fn(after_index: int):
        return env.get_new_states(after_index)

    def capture_now_fn():
        return env.capture_screenshot_now()

    def send_action_fn(action):
        # 只发送环境关心的动作类型
        if action.type in ["exec", "skip", "stop_execute"]:
            env_action = EnvAction(
                type=action.type,
                content=action.content,
                action_id=action.action_id,
                target_action_id=action.target_action_id,
            )
            env.send_action(env_action)

    # 可选: self-reward 评分时附带当前 game state 快照
    # 包含: AgentBridge /api/state (位置/血量/食物/...) + RCON inventory (背包内容)
    state_snapshot_provider = None
    if self_reward_include_state:
        ab = _read_agentbridge_endpoint(runtime_config_path)
        if ab is None:
            print("⚠️  MCBOTS_SELF_REWARD_INCLUDE_STATE set but cannot read agentbridge endpoint; state snapshot disabled")
        else:
            _ab_host, _ab_port = ab
            _state_url = f"http://{_ab_host}:{_ab_port}/api/state"
            _rcon_host = os.getenv("MCBOTS_RCON_HOST", "127.0.0.1")
            _rcon_port = int(os.getenv("MCBOTS_RCON_PORT", "50001"))
            _rcon_password = os.getenv("MCBOTS_RCON_PASSWORD", "minecraft")
            _player = player_name
            import urllib.request as _urllib_request
            import subprocess as _subprocess

            _BAD_RCON_MARKERS = ("No entity was found", "Unknown or incomplete command", "Incorrect argument")
            _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

            def _rcon(cmd: str, timeout: float = 3.0):
                try:
                    proc = _subprocess.run(
                        ["mcrcon", "-H", _rcon_host, "-P", str(_rcon_port),
                         "-p", _rcon_password, cmd],
                        capture_output=True, text=True, timeout=timeout,
                    )
                except Exception:
                    return None
                if proc.returncode != 0:
                    return None
                out = _ANSI_RE.sub("", (proc.stdout or "").strip())
                if not out or any(b in out for b in _BAD_RCON_MARKERS):
                    return None
                return out

            _BLOCK_ID_RE = re.compile(r'(?:block|id)\s*[:=]\s*"?(minecraft:[a-z0-9_]+)"?')

            def _take_snapshot():
                snap: dict = {}
                # 1) AgentBridge state — fast path for pos/rotation/motion/on_ground/gamemode
                try:
                    with _urllib_request.urlopen(_state_url, timeout=3.0) as r:
                        body = r.read().decode("utf-8", errors="replace")
                    parsed = json.loads(body)
                    data = parsed.get("data") if isinstance(parsed, dict) else None
                    if isinstance(data, dict):
                        for k in ("position", "rotation", "motion", "on_ground", "gamemode", "container_id"):
                            if k in data:
                                snap[("pos" if k == "position" else k)] = data[k]
                except Exception:
                    pass
                # 2) Full bot NBT — single rcon call yields vitals/xp/inventory/effects/handheld/dimension
                nbt_raw = _rcon(f"data get entity @a[name={_player},limit=1]")
                if nbt_raw:
                    try:
                        nbt = parse_snbt(nbt_raw)
                        if isinstance(nbt, dict):
                            v = extract_vitals(nbt)
                            if v:
                                snap["vitals"] = v
                            x = extract_xp(nbt)
                            if x:
                                snap["xp"] = x
                            dim = nbt.get("Dimension")
                            if isinstance(dim, str):
                                snap["dimension"] = dim
                            inv_items = nbt.get("Inventory") or []
                            inv, dur = parse_inventory_items(inv_items)
                            snap["inventory"] = inv
                            if dur:
                                snap["inventory_durability"] = dur
                            sel = nbt.get("SelectedItemSlot", 0)
                            hh = extract_handheld(inv_items, sel)
                            if hh is not None:
                                snap["handheld"] = hh
                            eff = extract_effects(nbt)
                            if eff:
                                snap["active_effects"] = eff
                    except Exception as e:
                        # Don't log on hot path; first failure usually one-off (e.g. world state mid-load)
                        snap["_nbt_parse_error"] = str(e)[:80]
                # 3) Three nearby blocks — parse minecraft:id out of the NBT text response
                nb: dict = {}
                for label, offset in (("feet", "~ ~ ~"), ("below", "~ ~-1 ~"), ("head", "~ ~1 ~")):
                    out = _rcon(f"execute as @a[name={_player}] at @s run data get block {offset}")
                    if out:
                        m = _BLOCK_ID_RE.search(out)
                        if m:
                            nb[label] = m.group(1)
                if nb:
                    snap["nearby_blocks"] = nb
                # 4) Nearby entity presence flags (boolean — "any within radius?")
                ne: dict = {}
                for label, query in (
                    ("hostile_within_20", "type=#minecraft:monster,distance=..20"),
                    ("items_within_20", "type=item,distance=..20"),
                ):
                    out = _rcon(f"execute as @a[name={_player}] at @s if entity @e[{query},limit=1]")
                    ne[label] = bool(out and "passed" in out.lower())
                if ne:
                    snap["nearby_entities"] = ne
                return snap
            # state_snapshot_provider() 返回当前一刻的 raw 状态 (dict)。
            # Agent 内部维护 per-window state record (initial + per-turn deltas + final),
            # 并在合适的钩子点 (initial trajectory snapshot / post-reset snapshot / post-exec snapshot)
            # 主动调用本 provider 拿样本。grader 看到的是 record JSON,不是单个 snapshot。
            print(f"✓ Self-reward state snapshot enabled (state={_state_url}, rcon={_rcon_host}:{_rcon_port}, player={_player})")
            def state_snapshot_provider():
                return _take_snapshot()

    agent = Agent(
        env_getter=get_states_fn,
        action_sender=send_action_fn,
        capture_now_getter=capture_now_fn,
        runtime_state_getter=env.fetch_runtime_state,
        panorama_capturer=env.capture_panorama,
        enable_panorama=enable_panorama,
        api_key=api_key,
        base_url=base_url,
        model=model,
        api_protocol=api_protocol,
        action_protocol=action_protocol,
        servername=server_name,
        agentname=container_name,
        observe_interval=observe_interval,
        system_prompt=(
            build_navigation_system_prompt(
                hints_enabled=navigation_hints_enabled,
                panorama_enabled=enable_panorama,
                action_protocol=action_protocol,
            )
            if system_prompt_profile == "navigation"
            else build_system_prompt(
                workspace_root,
                runtime_config_path,
                action_doc_path,
                request_extra_body,
                observation_default=("streaming" if default_observe_enabled else "event_only"),
                allow_model_observe_toggle=allow_model_observe_toggle,
                enable_panorama=enable_panorama,
                navigation_claim_enabled=navigation_claim_client is not None,
            )
        ),
        request_extra_body=request_extra_body,
        initial_user_message=initial_user_input,
        eval_mode=eval_mode,
        display=display,
        enable_video_recording=record_video,
        video_fps=video_fps,
        video_resolution=video_resolution,
        video_filename=video_filename,
        video_codec=video_codec,
        video_crf=video_crf,
        video_preset=video_preset,
        enable_frame_dedup=frame_dedup_enabled,
        frame_dedup_force_keep_after_event=frame_dedup_force_keep_after_event,
        enable_approx_frame_dedup=approx_frame_dedup_enabled,
        approx_frame_diff_threshold=approx_frame_diff_threshold,
        approx_frame_peak_tile_guard_threshold=approx_frame_peak_tile_guard_threshold,
        approx_frame_signature_width=approx_frame_signature_width,
        approx_frame_signature_height=approx_frame_signature_height,
        approx_frame_tile_cols=approx_frame_tile_cols,
        approx_frame_tile_rows=approx_frame_tile_rows,
        enable_approx_center_roi_weighting=approx_center_roi_weighting_enabled,
        approx_center_roi_width_ratio=approx_center_roi_width_ratio,
        approx_center_roi_height_ratio=approx_center_roi_height_ratio,
        approx_center_roi_weight=approx_center_roi_weight,
        enable_adaptive_frame_budget=adaptive_frame_budget_enabled,
        adaptive_frame_idle_min_keep_interval_sec=adaptive_frame_idle_min_keep_interval_sec,
        adaptive_frame_active_min_keep_interval_sec=adaptive_frame_active_min_keep_interval_sec,
        adaptive_frame_event_boost_window_sec=adaptive_frame_event_boost_window_sec,
        adaptive_frame_bypass_local_peak_change=adaptive_frame_bypass_local_peak_change,
        adaptive_frame_boost_on_visual_change=adaptive_frame_boost_on_visual_change,
        frame_keepalive_sec=frame_keepalive_sec,
        enable_frame_filter_telemetry=frame_filter_telemetry,
        max_llm_request_successes=max_llm_request_successes,
        max_llm_request_failures=max_llm_request_failures,
        max_consecutive_llm_failures=max_consecutive_llm_failures,
        max_images_in_context=max_images_in_context,
        sampling_config_path=sampling_config_path,
        roll_notify_url=roll_notify_url,
        llm_request_timeout_sec=llm_request_timeout_sec,
        llm_max_retries=llm_max_retries,
        llm_watchdog_interval_sec=llm_watchdog_interval_sec,
        llm_gate_dir=llm_gate_dir,
        llm_gate_max_inflight=llm_gate_max_inflight,
        llm_gate_starts_per_minute=llm_gate_starts_per_minute,
        llm_gate_initial_burst=llm_gate_initial_burst,
        llm_429_max_retries=llm_429_max_retries,
        llm_429_backoff_base_sec=llm_429_backoff_base_sec,
        llm_429_backoff_max_sec=llm_429_backoff_max_sec,
        llm_429_backoff_jitter=llm_429_backoff_jitter,
        default_observe_enabled=default_observe_enabled,
        allow_model_observe_toggle=allow_model_observe_toggle,
        auto_summarize_token_threshold=auto_summarize_token_threshold,
        auto_summarize_turn_threshold=auto_summarize_turn_threshold,
        auto_summary_profile=system_prompt_profile,
        max_conversation_rounds=max_conversation_rounds,
        enable_self_reward=enable_self_reward,
        self_reward_share_system_prompt=self_reward_share_system_prompt,
        state_snapshot_provider=state_snapshot_provider,
        navigation_claim_client=navigation_claim_client,
    )

    shutdown_triggered = False

    def _handle_signal(signum, _frame):
        nonlocal shutdown_triggered
        if shutdown_triggered:
            return
        shutdown_triggered = True
        print(f"\n⚠️  Received signal {signum}, stopping...")
        agent.stop_reason = f"signal_{signum}"
        agent.stop()
        env.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    agent_failure: BaseException | None = None
    try:
        agent.start()
        agent.wait()
    except BaseException as error:
        agent_failure = error
        raise
    finally:
        agent.stop()
        env.stop()
        if navigation_agent_status_path:
            _write_agent_status(
                Path(navigation_agent_status_path),
                {
                    "schema_version": 1,
                    "status": "crashed" if agent_failure is not None else "stopped",
                    "stop_reason": (
                        f"exception:{type(agent_failure).__name__}"
                        if agent_failure is not None
                        else (agent.stop_reason or "agent_stopped")
                    ),
                    "llm_request_total": agent.llm_request_total,
                    "llm_request_success": agent.llm_request_success,
                    "llm_request_failed": agent.llm_request_failed,
                    "consecutive_llm_failures": agent.consecutive_llm_failures,
                    "recorded_at_utc": datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
        print("\n✓ Stopped")


if __name__ == "__main__":
    main()
