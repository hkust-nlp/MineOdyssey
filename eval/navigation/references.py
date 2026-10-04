"""Conservative static navigation references over an unmodified Anvil world.

This evaluator-side planner reads chunk palettes directly. It deliberately
models only ordinary walking, one-block steps, short drops, manually openable
wooden doors, and ladder/vine movement. Unknown mechanics fail closed.
"""

from __future__ import annotations

import gzip
import hashlib
import heapq
import json
import math
import struct
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .snapshots import NbtReader

PLANNER_VERSION = "static-anvil-astar-v1"

@dataclass(frozen=True)
class BlockState:
    name: str
    properties: tuple[tuple[str, str], ...] = ()

    def property(self, key: str) -> Optional[str]:
        for prop_key, value in self.properties:
            if prop_key == key:
                return value
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "properties": dict(self.properties)}


AIR = BlockState("minecraft:air")
Position = tuple[int, int, int]


class AnvilWorld:
    def __init__(self, world_dir: Path, *, max_cached_chunks: int = 128):
        self.world_dir = world_dir
        self.region_dir = world_dir / "region"
        self.max_cached_chunks = max(1, int(max_cached_chunks))
        self.chunk_cache: OrderedDict[tuple[int, int], dict[tuple[int, int, int], BlockState]] = OrderedDict()

    def _region_file(self, cx: int, cz: int) -> Path:
        rx = math.floor(cx / 32)
        rz = math.floor(cz / 32)
        return self.region_dir / f"r.{rx}.{rz}.mca"

    def _read_chunk_nbt(self, cx: int, cz: int) -> dict[str, Any] | None:
        path = self._region_file(cx, cz)
        if not path.is_file():
            return None
        idx = (cx % 32) + (cz % 32) * 32
        with path.open("rb") as f:
            f.seek(idx * 4)
            loc = f.read(4)
            if len(loc) != 4:
                return None
            offset = (loc[0] << 16) | (loc[1] << 8) | loc[2]
            sectors = loc[3]
            if offset == 0 or sectors == 0:
                return None
            f.seek(offset * 4096)
            length_raw = f.read(4)
            if len(length_raw) != 4:
                return None
            length = struct.unpack(">I", length_raw)[0]
            comp_type_raw = f.read(1)
            if not comp_type_raw or length <= 1:
                return None
            payload = f.read(length - 1)
        comp_type = comp_type_raw[0]
        if comp_type == 1:
            data = gzip.decompress(payload)
        elif comp_type == 2:
            data = zlib.decompress(payload)
        elif comp_type == 3:
            data = payload
        else:
            raise ValueError(f"unsupported compression {comp_type} in {path}")
        return NbtReader(data).root()

    @staticmethod
    def _decode_section(section: dict[str, Any]) -> dict[tuple[int, int, int], BlockState]:
        y_section = int(section.get("Y", 0))
        modern_block_states = section.get("block_states")
        if isinstance(modern_block_states, dict):
            palette = (
                modern_block_states.get("palette")
                or modern_block_states.get("Palette")
            )
            data = modern_block_states.get("data") or modern_block_states.get("Data")
        else:
            # Java chunks before the flattened section-storage compound keep
            # the palette directly on the section and ``BlockStates`` is the
            # packed long array.  Treating that array as a compound silently
            # decoded every legacy section as air.
            palette = section.get("Palette")
            data = section.get("BlockStates")
        if not palette:
            return {}
        states: list[BlockState] = []
        for entry in palette:
            if isinstance(entry, dict):
                raw_properties = entry.get("Properties") or entry.get("properties") or {}
                properties = (
                    tuple(sorted((str(key), str(value)) for key, value in raw_properties.items()))
                    if isinstance(raw_properties, dict)
                    else ()
                )
                states.append(
                    BlockState(
                        str(entry.get("Name", entry.get("name", "minecraft:air"))),
                        properties,
                    )
                )
            else:
                states.append(AIR)

        out: dict[tuple[int, int, int], BlockState] = {}
        if not data:
            state = states[0] if states else AIR
            if state != AIR:
                for i in range(4096):
                    x = i & 15
                    z = (i >> 4) & 15
                    y = (i >> 8) & 15
                    out[(x, y_section * 16 + y, z)] = state
            return out

        bits = max(4, (len(states) - 1).bit_length())
        mask = (1 << bits) - 1
        values_per_long = 64 // bits
        unsigned_data = [(int(v) & ((1 << 64) - 1)) for v in data]
        for i in range(4096):
            word_i = i // values_per_long
            if word_i >= len(unsigned_data):
                break
            shift = (i % values_per_long) * bits
            palette_i = (unsigned_data[word_i] >> shift) & mask
            if palette_i >= len(states):
                palette_i = 0
            state = states[palette_i]
            if state != AIR:
                x = i & 15
                z = (i >> 4) & 15
                y = (i >> 8) & 15
                out[(x, y_section * 16 + y, z)] = state
        return out

    def _load_chunk(self, cx: int, cz: int) -> dict[tuple[int, int, int], BlockState]:
        key = (cx, cz)
        if key in self.chunk_cache:
            self.chunk_cache.move_to_end(key)
            return self.chunk_cache[key]
        root = self._read_chunk_nbt(cx, cz)
        blocks: dict[tuple[int, int, int], BlockState] = {}
        if root is not None:
            body = root.get("Level") if isinstance(root.get("Level"), dict) else root
            sections = body.get("sections") or body.get("Sections") or []
            for section in sections:
                if not isinstance(section, dict):
                    continue
                for (lx, y, lz), state in self._decode_section(section).items():
                    blocks[(lx, y, lz)] = state
        self.chunk_cache[key] = blocks
        self.chunk_cache.move_to_end(key)
        while len(self.chunk_cache) > self.max_cached_chunks:
            self.chunk_cache.popitem(last=False)
        return blocks

    def block(self, x: int, y: int, z: int) -> BlockState:
        cx = math.floor(x / 16)
        cz = math.floor(z / 16)
        lx = x & 15
        lz = z & 15
        return self._load_chunk(cx, cz).get((lx, y, lz), AIR)


PASSABLE_EXACT = {
    "minecraft:air",
    "minecraft:cave_air",
    "minecraft:void_air",
    "minecraft:ladder",
    "minecraft:vine",
    "minecraft:short_grass",
    "minecraft:tall_grass",
    "minecraft:fern",
    "minecraft:large_fern",
    "minecraft:torch",
    "minecraft:wall_torch",
    "minecraft:soul_torch",
    "minecraft:soul_wall_torch",
    "minecraft:redstone_torch",
    "minecraft:redstone_wall_torch",
    "minecraft:lever",
    "minecraft:stone_button",
    "minecraft:oak_button",
    "minecraft:tripwire",
    "minecraft:tripwire_hook",
}

# Vanilla bush/flower/crop blocks with an empty entity-collision shape.  Keep
# this list explicit: a suffix rule such as ``_flower`` would both miss poppy
# and incorrectly classify the solid chorus flower as passable support-space.
NON_COLLIDING_PLANT_EXACT = {
    "minecraft:allium",
    "minecraft:attached_melon_stem",
    "minecraft:attached_pumpkin_stem",
    "minecraft:azure_bluet",
    "minecraft:beetroots",
    "minecraft:blue_orchid",
    "minecraft:carrots",
    "minecraft:cave_vines",
    "minecraft:cave_vines_plant",
    "minecraft:cornflower",
    "minecraft:crimson_fungus",
    "minecraft:crimson_roots",
    "minecraft:dandelion",
    "minecraft:dead_bush",
    "minecraft:glow_lichen",
    "minecraft:hanging_roots",
    "minecraft:kelp",
    "minecraft:kelp_plant",
    "minecraft:lilac",
    "minecraft:lily_of_the_valley",
    "minecraft:melon_stem",
    "minecraft:nether_sprouts",
    "minecraft:nether_wart",
    "minecraft:oxeye_daisy",
    "minecraft:peony",
    "minecraft:pink_petals",
    "minecraft:pitcher_crop",
    "minecraft:pitcher_plant",
    "minecraft:poppy",
    "minecraft:potatoes",
    "minecraft:pumpkin_stem",
    "minecraft:rose_bush",
    "minecraft:sculk_vein",
    "minecraft:seagrass",
    "minecraft:spore_blossom",
    "minecraft:sugar_cane",
    "minecraft:sunflower",
    "minecraft:tall_seagrass",
    "minecraft:torchflower",
    "minecraft:torchflower_crop",
    "minecraft:twisting_vines",
    "minecraft:twisting_vines_plant",
    "minecraft:warped_fungus",
    "minecraft:warped_roots",
    "minecraft:weeping_vines",
    "minecraft:weeping_vines_plant",
    "minecraft:wheat",
    "minecraft:wither_rose",
}

PASSABLE_SUFFIXES = (
    "_door",
    "_sign",
    "_wall_sign",
    "_hanging_sign",
    "_wall_hanging_sign",
    "_pressure_plate",
    "_carpet",
    "_rail",
    "_sapling",
    "_tulip",
    "_mushroom",
)

CLIMBABLE_EXACT = {"minecraft:ladder", "minecraft:vine"}

# These blocks have no collision surface that can support a player's feet.
# They intentionally remain non-passable in the ordinary walking graph: the
# planner does not model swimming or wading.  Keeping the support predicate
# separate prevents a ``not passable == solid`` shortcut from treating a water
# or lava source below an air cell as dry ground.
NON_SUPPORT_EXACT = {
    "minecraft:water",
    "minecraft:lava",
    "minecraft:bubble_column",
}

LADDER_FRONT_OFFSETS = {
    "north": (0, -1),
    "south": (0, 1),
    "west": (-1, 0),
    "east": (1, 0),
}

TALL_SUPPORT_SUFFIXES = (
    "_fence",
    "_fence_gate",
    "_wall",
)


def is_door(state: BlockState) -> bool:
    return state.name.endswith("_door")


def is_iron_door(state: BlockState) -> bool:
    return state.name == "minecraft:iron_door"


def standable_column_has_door(world: AnvilWorld, position: Position) -> bool:
    """Return whether a feet/head column contains either half of a door."""

    x, y, z = position
    return is_door(world.block(x, y, z)) or is_door(world.block(x, y + 1, z))


def has_tall_support_collision(state: BlockState) -> bool:
    """Return whether a block's top is above the planner's integer foot cell.

    Fences, fence gates, and walls have a 1.5-block collision height.  The
    integer-cell movement model cannot represent standing on that surface, so
    treating them as ordinary one-block support creates false step-up edges.
    """

    return state.name.endswith(TALL_SUPPORT_SUFFIXES)


def has_ordinary_support_collision(state: BlockState, *, allow_doors: bool) -> bool:
    """Return whether ``state`` provides an ordinary feet-support surface."""

    return (
        state.name not in NON_SUPPORT_EXACT
        and not is_passable(state, allow_doors=allow_doors)
        and not has_tall_support_collision(state)
    )


def support_collision_top_surface_y(
    world: AnvilWorld,
    position: Position,
    *,
    allow_doors: bool,
) -> Optional[float]:
    """Return the physical top Y of an ordinary standable cell's support.

    Planner feet cells are integer-valued, but a bottom slab supports the
    player half a block below that integer Y.  Keeping the physical surface Y
    explicit prevents an integer ``+1`` edge from hiding a 1.5-block jump
    from a bottom slab onto a full block.

    Climbable cells do not require an ordinary support block and therefore do
    not have a support surface in this model.
    """

    x, y, z = position
    if is_climbable(world.block(x, y, z)):
        return None
    support_y = y - 1
    support = world.block(x, support_y, z)
    if not has_ordinary_support_collision(support, allow_doors=allow_doors):
        return None
    collision_top_offset = 1.0
    if support.name.endswith("_slab") and support.property("type") == "bottom":
        collision_top_offset = 0.5
    return float(support_y) + collision_top_offset


def _horizontal_upward_support_surface_is_reachable(
    world: AnvilWorld,
    source: Position,
    target: Position,
    *,
    allow_doors: bool,
) -> bool:
    """Reject horizontal step-ups whose physical support rise exceeds one."""

    if target[1] <= source[1]:
        return True
    source_surface_y = support_collision_top_surface_y(
        world,
        source,
        allow_doors=allow_doors,
    )
    target_surface_y = support_collision_top_surface_y(
        world,
        target,
        allow_doors=allow_doors,
    )
    # Ladder/vine cells use the separate climbable movement model rather than
    # an ordinary support surface, so retain their existing graph behavior.
    if source_surface_y is None or target_surface_y is None:
        return True
    return target_surface_y - source_surface_y <= 1.0 + 1e-9


def is_climbable(state: BlockState) -> bool:
    return state.name in CLIMBABLE_EXACT


def horizontal_ladder_entry_is_from_front(
    world: AnvilWorld,
    source: Position,
    target: Position,
) -> bool:
    """Require same-level ladder entry from its facing-side front cell.

    A ladder has a full-height, 3/16-block collision plate on the wall side
    opposite its facing.  Side entry can therefore be blocked exactly at the
    target cell boundary when the player's AABB overlaps that plate.  Entering
    from the facing-side cell approaches the plate head-on and leaves the
    player's center inside the ladder cell, which is the executable geometry
    represented by a horizontal graph edge.

    Non-ladder targets are unaffected.  An unknown ladder facing fails closed.
    """

    target_state = world.block(*target)
    if target_state.name != "minecraft:ladder":
        return True
    offset = LADDER_FRONT_OFFSETS.get(target_state.property("facing") or "")
    if offset is None or source[1] != target[1]:
        return False
    return source == (
        target[0] + offset[0],
        target[1],
        target[2] + offset[1],
    )


def is_passable(state: BlockState, *, allow_doors: bool) -> bool:
    if state.name in PASSABLE_EXACT or state.name in NON_COLLIDING_PLANT_EXACT:
        return True
    if is_door(state):
        if not allow_doors:
            return False
        # Wooden doors can be opened directly by the player.  A closed iron
        # door needs a separately modelled activation mechanism; conservatively
        # block it here until the route graph can certify such an interaction.
        return not is_iron_door(state) or state.property("open") == "true"
    if state.name.endswith(PASSABLE_SUFFIXES):
        return True
    return False


def is_standable(world: AnvilWorld, p: Position, *, allow_doors: bool) -> bool:
    x, y, z = p
    foot = world.block(x, y, z)
    head = world.block(x, y + 1, z)
    below = world.block(x, y - 1, z)
    if is_climbable(foot) and is_passable(head, allow_doors=allow_doors):
        return True
    return (
        is_passable(foot, allow_doors=allow_doors)
        and is_passable(head, allow_doors=allow_doors)
        and has_ordinary_support_collision(below, allow_doors=allow_doors)
    )


def nearest_standable(
    world: AnvilWorld,
    p: Position,
    *,
    radius: int,
    allow_doors: bool,
) -> Optional[Position]:
    px, py, pz = p
    candidates: list[tuple[int, int, int, Position]] = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                q = (px + dx, py + dy, pz + dz)
                score = abs(dx) + abs(dy) + abs(dz)
                candidates.append((score, abs(dy), abs(dx) + abs(dz), q))
    for *_score, q in sorted(candidates):
        if is_standable(world, q, allow_doors=allow_doors):
            return q
    return None


def _inside_bbox(p: Position, bbox: tuple[int, int, int, int, int, int]) -> bool:
    min_x, max_x, min_y, max_y, min_z, max_z = bbox
    return min_x <= p[0] <= max_x and min_y <= p[1] <= max_y and min_z <= p[2] <= max_z


def _horizontal_descent_swept_column_is_clear(
    world: AnvilWorld,
    source: Position,
    target: Position,
    *,
    allow_doors: bool,
) -> bool:
    """Require target-column clearance while entering a lower foot cell.

    A lower target can be independently standable while still being
    impossible to enter from the source elevation.  The player first crosses
    the horizontal cell boundary with its feet at ``source.y`` and only then
    falls.  Consequently the target column must be passable for the complete
    vertical sweep from landing feet through the source-height head block.
    Without this check a low ceiling can clamp the player exactly at the cell
    boundary.
    """

    if target[1] >= source[1]:
        return True
    target_x, target_y, target_z = target
    source_head_y = source[1] + 1
    return all(
        is_passable(
            world.block(target_x, block_y, target_z),
            allow_doors=allow_doors,
        )
        for block_y in range(target_y, source_head_y + 1)
    )


def _resolve_horizontal_step(
    world: AnvilWorld,
    p: Position,
    dx: int,
    dz: int,
    bbox: tuple[int, int, int, int, int, int],
    *,
    allow_doors: bool,
    max_drop: int,
) -> Optional[Position]:
    min_x, max_x, min_y, max_y, min_z, max_z = bbox
    x, y, z = p
    nx, nz = x + dx, z + dz
    if not (min_x <= nx <= max_x and min_z <= nz <= max_z):
        return None
    same_level = (nx, y, nz)
    source_or_target_is_climbable = is_climbable(
        world.block(*p)
    ) or is_climbable(world.block(*same_level))
    if (
        min_y <= y <= max_y
        and source_or_target_is_climbable
        and is_standable(world, same_level, allow_doors=allow_doors)
        and horizontal_ladder_entry_is_from_front(world, p, same_level)
    ):
        # Enter and leave climbable columns at the source foot height when
        # that cell is available.  Choosing the generic +1 candidate first
        # creates a fictitious simultaneous horizontal-and-up ladder move;
        # the controller crosses the cell boundary at the source height and
        # only a subsequent ladder_vertical edge can climb.
        return same_level
    for dy in range(1, -max_drop - 1, -1):
        q = (nx, y + dy, nz)
        if not (min_y <= q[1] <= max_y):
            continue
        if not is_standable(world, q, allow_doors=allow_doors):
            continue
        if dy != 0 and is_climbable(world.block(*q)):
            # Never encode vertical motion as part of horizontal ladder
            # entry.  A ladder whose source-height cell is unavailable must
            # be approached through another same-height cell instead.
            continue
        if not horizontal_ladder_entry_is_from_front(world, p, q):
            continue
        if not _horizontal_upward_support_surface_is_reachable(
            world,
            p,
            q,
            allow_doors=allow_doors,
        ):
            continue
        if not _horizontal_descent_swept_column_is_clear(
            world,
            p,
            q,
            allow_doors=allow_doors,
        ):
            continue
        return q
    return None


def neighbors(
    world: AnvilWorld,
    p: Position,
    bbox: tuple[int, int, int, int, int, int],
    *,
    allow_doors: bool,
    max_drop: int,
) -> Iterable[tuple[Position, float, str]]:
    foot = world.block(*p)
    if is_climbable(foot):
        for dy in (1, -1):
            q = (p[0], p[1] + dy, p[2])
            if _inside_bbox(q, bbox) and is_standable(world, q, allow_doors=allow_doors):
                yield q, 1.0, "ladder_vertical"

    cardinal: dict[tuple[int, int], Position] = {}
    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        q = _resolve_horizontal_step(
            world,
            p,
            dx,
            dz,
            bbox,
            allow_doors=allow_doors,
            max_drop=max_drop,
        )
        if q is None:
            continue
        cardinal[(dx, dz)] = q
        delta_y = q[1] - p[1]
        cost = math.sqrt(1.0 + float(delta_y * delta_y))
        yield q, cost, "walk" if delta_y == 0 else "step"

    # A diagonal is legal only when both orthogonal side cells are standable at
    # the destination elevation, preventing paths that clip a wall corner.
    for dx, dz in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
        side_x = cardinal.get((dx, 0))
        side_z = cardinal.get((0, dz))
        if side_x is None or side_z is None:
            continue
        q = _resolve_horizontal_step(
            world,
            p,
            dx,
            dz,
            bbox,
            allow_doors=allow_doors,
            max_drop=max_drop,
        )
        if q is None or side_x[1] != q[1] or side_z[1] != q[1]:
            continue
        # A diagonal whose source, target, or either orthogonal clearance
        # column contains a door is not executable by the route-local door
        # controller.  In a double doorway the path scanner would otherwise
        # record only the path door, leaving the side door closed; even an
        # opened leaf needs an axis-aligned swept-AABB passage.  Preserve the
        # corresponding cardinal edges so search can route through/around the
        # doorway as an explicit dogleg.
        if any(
            standable_column_has_door(world, position)
            for position in (p, q, side_x, side_z)
        ):
            continue
        delta_y = q[1] - p[1]
        yield q, math.sqrt(2.0 + float(delta_y * delta_y)), "diagonal"


@dataclass
class SearchResult:
    path: Optional[list[Position]]
    cost: Optional[float]
    expanded_nodes: int
    limit_hit: bool
    boundary_lower_bound: Optional[float]
    certified_within_bbox: bool
    movement_types: dict[Position, str]


def _reconstruct_path(previous: dict[Position, Optional[Position]], goal: Position) -> list[Position]:
    path: list[Position] = []
    current: Optional[Position] = goal
    while current is not None:
        path.append(current)
        current = previous[current]
    return list(reversed(path))


def _is_exit_frontier(
    p: Position,
    bbox: tuple[int, int, int, int, int, int],
    *,
    max_drop: int,
) -> bool:
    """Return whether a cell can be the inside endpoint of a bbox exit.

    Horizontal moves and upward/ladder moves advance by one cell, so their
    inside endpoint lies on an exact bbox face.  A horizontal move may drop
    several blocks, however, and can jump below ``min_y`` without visiting the
    exact lower face.  The lower frontier therefore includes the narrow band
    from which a maximum-length drop can leave the bbox.
    """
    min_x, max_x, min_y, max_y, min_z, max_z = bbox
    lower_exit_ceiling = min_y + max(0, max_drop - 1)
    return (
        p[0] in {min_x, max_x}
        or p[2] in {min_z, max_z}
        or p[1] == max_y
        or p[1] <= lower_exit_ceiling
    )


def _distance_to_goal_set(p: Position, goals: set[Position]) -> float:
    return min(math.dist(p, goal) for goal in goals)


def _octile_vertical_distance(p: Position, goal: Position) -> float:
    """Obstacle-free lower bound under the planner's exact edge costs.

    Octile distance is the shortest horizontal cardinal/diagonal grid length.
    Combining it with net vertical displacement using an L2 norm remains a
    lower bound because every planner edge costs its 3D Euclidean cell
    displacement.  This includes multi-block directed drops and unit ladder
    moves.
    """
    dx = abs(p[0] - goal[0])
    dz = abs(p[2] - goal[2])
    diagonal_steps = min(dx, dz)
    cardinal_steps = max(dx, dz) - diagonal_steps
    horizontal = diagonal_steps * math.sqrt(2.0) + cardinal_steps
    return math.hypot(horizontal, abs(p[1] - goal[1]))


def _multi_goal_lower_bound(p: Position, goals: set[Position]) -> float:
    return min(_octile_vertical_distance(p, goal) for goal in goals)


SEARCH_HEURISTICS = {
    "dijkstra": "zero",
    "astar": "multi_goal_octile_vertical_l2",
}


def search_static_route(
    world: AnvilWorld,
    start: Position,
    goals: set[Position],
    bbox: tuple[int, int, int, int, int, int],
    *,
    allow_doors: bool,
    max_drop: int,
    max_nodes: int,
    search_algorithm: str,
) -> SearchResult:
    if search_algorithm not in SEARCH_HEURISTICS:
        raise ValueError(f"unsupported search algorithm: {search_algorithm}")
    if not goals:
        raise ValueError("search requires at least one goal")

    def heuristic(position: Position) -> float:
        if search_algorithm == "dijkstra":
            return 0.0
        return _multi_goal_lower_bound(position, goals)

    # Store g separately from f.  A cell may be reinserted after a better g is
    # found; the g equality check discards stale heap records without closing
    # the cell permanently, so reopening remains correct.
    frontier: list[tuple[float, float, Position]] = [(heuristic(start), 0.0, start)]
    distances: dict[Position, float] = {start: 0.0}
    settled_distances: dict[Position, float] = {}
    previous: dict[Position, Optional[Position]] = {start: None}
    movement_types: dict[Position, str] = {}
    best_goal: Optional[Position] = None
    best_goal_cost: Optional[float] = None
    expanded_nodes = 0
    limit_hit = False
    frontier_lower_bound = float("inf")

    while frontier:
        priority, cost, p = heapq.heappop(frontier)
        if cost != distances.get(p):
            continue
        if best_goal_cost is not None and priority > best_goal_cost + 1e-9:
            frontier_lower_bound = priority
            break
        expanded_nodes += 1
        if expanded_nodes > max_nodes:
            limit_hit = True
            break
        settled_distances[p] = cost
        if p in goals and best_goal is None:
            best_goal = p
            best_goal_cost = cost
        for q, edge_cost, movement_type in neighbors(
            world,
            p,
            bbox,
            allow_doors=allow_doors,
            max_drop=max_drop,
        ):
            candidate_cost = cost + edge_cost
            if candidate_cost + 1e-9 < distances.get(q, float("inf")):
                candidate_priority = candidate_cost + heuristic(q)
                if best_goal_cost is not None and candidate_priority > best_goal_cost + 1e-9:
                    continue
                distances[q] = candidate_cost
                previous[q] = p
                movement_types[q] = movement_type
                heapq.heappush(frontier, (candidate_priority, candidate_cost, q))

    if best_goal is None or best_goal_cost is None:
        return SearchResult(None, None, expanded_nodes, limit_hit, None, False, movement_types)

    boundary_heuristic = (
        _distance_to_goal_set if search_algorithm == "dijkstra" else _multi_goal_lower_bound
    )
    boundary_candidates: list[float] = []
    for position, distance in settled_distances.items():
        if not _is_exit_frontier(position, bbox, max_drop=max_drop):
            continue
        candidate = distance + boundary_heuristic(position, goals)
        if candidate <= best_goal_cost + 1e-9:
            boundary_candidates.append(candidate)
    # With consistent A*, all records through f <= L* are settled before this
    # point.  The first remaining fresh f is a conservative lower bound for
    # every not-yet-settled route to an exit frontier.  This lets A* certify
    # without expanding the full Dijkstra g <= L* ball.
    if search_algorithm == "astar" and not math.isinf(frontier_lower_bound):
        boundary_candidates.append(frontier_lower_bound)
    boundary_lower_bound = min(boundary_candidates) if boundary_candidates else float("inf")
    path = _reconstruct_path(previous, best_goal)
    path_touches_boundary = any(
        _is_exit_frontier(position, bbox, max_drop=max_drop) for position in path
    )
    certified = (
        not limit_hit
        and not path_touches_boundary
        and boundary_lower_bound >= best_goal_cost - 1e-9
    )
    return SearchResult(
        path,
        best_goal_cost,
        expanded_nodes,
        limit_hit,
        boundary_lower_bound,
        certified,
        movement_types,
    )


def dijkstra(
    world: AnvilWorld,
    start: Position,
    goals: set[Position],
    bbox: tuple[int, int, int, int, int, int],
    *,
    allow_doors: bool,
    max_drop: int,
    max_nodes: int,
) -> SearchResult:
    return search_static_route(
        world,
        start,
        goals,
        bbox,
        allow_doors=allow_doors,
        max_drop=max_drop,
        max_nodes=max_nodes,
        search_algorithm="dijkstra",
    )


def astar(
    world: AnvilWorld,
    start: Position,
    goals: set[Position],
    bbox: tuple[int, int, int, int, int, int],
    *,
    allow_doors: bool,
    max_drop: int,
    max_nodes: int,
) -> SearchResult:
    return search_static_route(
        world,
        start,
        goals,
        bbox,
        allow_doors=allow_doors,
        max_drop=max_drop,
        max_nodes=max_nodes,
        search_algorithm="astar",
    )



def _goal_cells(
    world: AnvilWorld,
    target: Sequence[float],
    *,
    radius_3d: float,
    radius_y: float,
    allow_doors: bool,
) -> set[Position]:
    tx, ty, tz = (float(value) for value in target)
    return {
        (x, y, z)
        for x in range(math.floor(tx - radius_3d), math.ceil(tx + radius_3d) + 1)
        for y in range(math.floor(ty - radius_y), math.ceil(ty + radius_y) + 1)
        for z in range(math.floor(tz - radius_3d), math.ceil(tz + radius_3d) + 1)
        if math.dist((float(x), float(y), float(z)), (tx, ty, tz)) < radius_3d
        and abs(float(y) - ty) <= radius_y + 1e-9
        and is_standable(world, (x, y, z), allow_doors=allow_doors)
    }


def _segment_reference(
    world: AnvilWorld,
    start: Position,
    target: Sequence[float],
    *,
    radius_3d: float,
    radius_y: float,
    allow_doors: bool,
    max_drop: int,
    max_nodes: int,
    margins: Sequence[int],
) -> dict[str, Any]:
    goals = _goal_cells(
        world,
        target,
        radius_3d=radius_3d,
        radius_y=radius_y,
        allow_doors=allow_doors,
    )
    if not goals:
        return {
            "status": "unreachable",
            "reason": "no_standable_arrival_cell",
            "expanded_nodes": 0,
        }

    expanded_total = 0
    last_result: Optional[SearchResult] = None
    for margin in margins:
        xs = [start[0], *(position[0] for position in goals)]
        ys = [start[1], *(position[1] for position in goals)]
        zs = [start[2], *(position[2] for position in goals)]
        bbox = (
            min(xs) - margin,
            max(xs) + margin,
            min(ys) - 16,
            max(ys) + 24,
            min(zs) - margin,
            max(zs) + margin,
        )
        result = astar(
            world,
            start,
            goals,
            bbox,
            allow_doors=allow_doors,
            max_drop=max_drop,
            max_nodes=max_nodes,
        )
        expanded_total += result.expanded_nodes
        last_result = result
        if result.path is not None and result.certified_within_bbox:
            return {
                "status": "reachable",
                "cost_blocks": result.cost,
                "expanded_nodes": expanded_total,
                "bbox": {
                    "x": [bbox[0], bbox[1]],
                    "y": [bbox[2], bbox[3]],
                    "z": [bbox[4], bbox[5]],
                },
                "path": [list(position) for position in result.path],
            }
        if result.limit_hit:
            break

    reason = "node_limit" if last_result and last_result.limit_hit else "search_bounds_exhausted"
    return {
        "status": "planner_limit",
        "reason": reason,
        "expanded_nodes": expanded_total,
    }


def plan_task_reference(
    world_dir: Path,
    start: Sequence[float],
    endpoints: Sequence[dict[str, Any]],
    *,
    allow_doors: bool = True,
    max_drop: int = 3,
    max_nodes: int = 750_000,
    margins: Sequence[int] = (24, 48, 96, 192, 384),
) -> dict[str, Any]:
    """Plan one route through the supplied ordered endpoint list.

    Endpoints contain the ordered required waypoints followed by the final
    target. The returned path belongs in the ignored local cache. Tracked
    references retain only its digest and aggregate length.
    """

    world = AnvilWorld(Path(world_dir))
    raw_start = tuple(int(round(float(value))) for value in start)
    snapped_start = nearest_standable(
        world,
        raw_start,
        radius=4,
        allow_doors=allow_doors,
    )
    if snapped_start is None:
        return {
            "planner_version": PLANNER_VERSION,
            "status": "unreachable",
            "reason": "no_standable_start_cell",
            "requested_start": list(start),
        }

    complete_path: list[list[int]] = [list(snapped_start)]
    segment_summaries: list[dict[str, Any]] = []
    current = snapped_start
    total_cost = 0.0
    expanded_nodes = 0
    for index, endpoint in enumerate(endpoints):
        segment = _segment_reference(
            world,
            current,
            endpoint["position"],
            radius_3d=float(endpoint["radius_3d"]),
            radius_y=float(endpoint["radius_y"]),
            allow_doors=allow_doors,
            max_drop=max_drop,
            max_nodes=max_nodes,
            margins=margins,
        )
        expanded_nodes += int(segment.get("expanded_nodes", 0))
        segment_summaries.append(
            {key: value for key, value in segment.items() if key != "path"}
        )
        if segment["status"] != "reachable":
            return {
                "planner_version": PLANNER_VERSION,
                "status": segment["status"],
                "reason": segment.get("reason"),
                "requested_start": list(start),
                "snapped_start": list(snapped_start),
                "failed_segment": index,
                "expanded_nodes": expanded_nodes,
                "segments": segment_summaries,
            }
        segment_path = segment["path"]
        complete_path.extend(segment_path[1:])
        current = tuple(segment_path[-1])
        total_cost += float(segment["cost_blocks"])

    encoded = json.dumps(
        complete_path,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return {
        "planner_version": PLANNER_VERSION,
        "status": "reachable",
        "requested_start": list(start),
        "snapped_start": list(snapped_start),
        "goal_cell": list(current),
        "length_blocks": round(total_cost, 6),
        "route_digest": hashlib.sha256(encoded).hexdigest(),
        "expanded_nodes": expanded_nodes,
        "segments": segment_summaries,
        "path": complete_path,
    }
