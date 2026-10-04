from __future__ import annotations

import functools
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


COMMON_HOSTILE_ENTITY_TYPES = [
    "minecraft:zombie",
    "minecraft:skeleton",
    "minecraft:creeper",
    "minecraft:spider",
    "minecraft:cave_spider",
    "minecraft:enderman",
    "minecraft:witch",
    "minecraft:slime",
    "minecraft:phantom",
    "minecraft:husk",
    "minecraft:stray",
    "minecraft:drowned",
    "minecraft:pillager",
    "minecraft:vindicator",
    "minecraft:evoker",
    "minecraft:ravager",
    "minecraft:vex",
    "minecraft:endermite",
    "minecraft:silverfish",
    "minecraft:bogged",
]


@dataclass(frozen=True)
class EvalFamilySpec:
    family_key: str
    parser_description: str
    default_task_config: str
    default_task_name: str
    score_objective: str
    score_criterion: str
    success_reason: str
    default_tool_fallback: str
    summon_entity: Optional[str] = None
    summon_x_range: Optional[Tuple[float, float]] = None
    summon_z_range: Optional[Tuple[float, float]] = None

    @property
    def uses_summon_target(self) -> bool:
        return bool(self.summon_entity and self.summon_x_range and self.summon_z_range)


KILL_ENTITY_SHEEP_SPEC = EvalFamilySpec(
    family_key="kill_entity",
    parser_description="Run minimal kill sheep evaluation on mcbots.",
    default_task_config="eval/openha_assets/kill_entity_min.json",
    default_task_name="kill_entity:sheep",
    score_objective="oha_kill_sheep",
    score_criterion="minecraft.killed:minecraft.sheep",
    success_reason="kill_detected",
    default_tool_fallback="diamond_sword",
    summon_entity="minecraft:sheep",
    summon_x_range=(-1.0, 1.0),
    summon_z_range=(2.0, 7.0),
)


KILL_ENTITY_SPEC = EvalFamilySpec(
    family_key="kill_entity",
    parser_description="Run OpenHA-style kill_entity evaluation on mcbots.",
    default_task_config="eval/openha_assets/kill_entity_min.json",
    default_task_name="kill_entity:sheep",
    score_objective="oha_kill_entity",
    score_criterion="minecraft.killed:minecraft.sheep",
    success_reason="kill_detected",
    default_tool_fallback="diamond_sword",
    summon_entity="minecraft:sheep",
    summon_x_range=(-1.0, 1.0),
    summon_z_range=(2.0, 7.0),
)


MINE_BLOCK_DIRT_SPEC = EvalFamilySpec(
    family_key="mine_block",
    parser_description="Run minimal mine block evaluation on mcbots.",
    default_task_config="eval/openha_assets/mine_block_min.json",
    default_task_name="mine_block:dirt",
    score_objective="oha_mine_dirt",
    score_criterion="minecraft.mined:minecraft.dirt",
    success_reason="mine_detected",
    default_tool_fallback="air",
)


MINE_BLOCK_SPEC = EvalFamilySpec(
    family_key="mine_block",
    parser_description="Run OpenHA-style mine_block evaluation on mcbots.",
    default_task_config="eval/openha_assets/mine_block_min.json",
    default_task_name="mine_block:dirt",
    score_objective="oha_mine_block",
    score_criterion="minecraft.mined:minecraft.dirt",
    success_reason="mine_detected",
    default_tool_fallback="air",
)


INTERACT_BLOCK_SPEC = EvalFamilySpec(
    family_key="interact_block",
    parser_description="Run minimal interact block evaluation on mcbots.",
    default_task_config="eval/openha_assets/interact_block_min.json",
    default_task_name="custom:interact_with_anvil",
    score_objective="oha_interact",
    score_criterion="minecraft.custom:minecraft.interact_with_anvil",
    success_reason="interact_detected",
    default_tool_fallback="air",
)


CRAFT_ITEM_SPEC = EvalFamilySpec(
    family_key="craft_item",
    parser_description="Run minimal craft_item evaluation on mcbots.",
    default_task_config="eval/openha_assets/craft_item_min.json",
    default_task_name="craft_item:stick",
    score_objective="oha_craft_item",
    score_criterion="minecraft.crafted:minecraft.stick",
    success_reason="craft_detected",
    default_tool_fallback="air",
)


SMELT_ITEM_SPEC = EvalFamilySpec(
    family_key="smelt_item",
    parser_description="Run minimal smelt_item evaluation on mcbots.",
    default_task_config="eval/openha_assets/smelt_item_min.json",
    default_task_name="smelt_item:baked_potato",
    score_objective="oha_smelt_item",
    score_criterion="minecraft.crafted:minecraft.baked_potato",
    success_reason="smelt_detected",
    default_tool_fallback="air",
)

EQUIP_SLOT_ITEM_REPLACE_TARGET = {
    "head": "armor.head",
    "chest": "armor.chest",
    "legs": "armor.legs",
    "feet": "armor.feet",
    "offhand": "weapon.offhand",
}

# Minimal initial pool to support the pumpkin-helmet alignment experiments.
# We can later replace this with imported OpenHA equipment tables/probabilities.
DEFAULT_RANDOM_EQUIP_DISTRACTION_POOL = {
    "head": ["carved_pumpkin"],
}
OPENHA_EQUIPMENT_ASSET_PATH = Path(__file__).resolve().parent / "openha_assets" / "mc_equipments.1.16.json"
OPENHA_MC_CONSTANTS_ASSET_PATH = Path(__file__).resolve().parent / "openha_assets" / "mc_constants.1.16.json"
OPENHA_OPEN_GUI_ACTIONS_ASSET_PATH = (
    Path(__file__).resolve().parent / "openha_assets" / "open_gui_actions.1.16.json"
)
OPENHA_RANDOM_EQUIP_SLOT_ORDER = ["offhand", "head", "chest", "legs", "feet"]
OPENHA_MIN_SLOT_IDX = 0
OPENHA_MAX_INVENTORY_SLOT_IDX = 35
OPENHA_INVENTORY_DISTRACTION_LEVEL = {
    "zero": list([0]),
    "one": list([1]),
    "easy": list(range(3, 7)),
    "middle": list(range(7, 16)),
    "hard": list(range(16, 36)),
    "normal": list(range(0, 36)),
}
OPENHA_EQUIP_DISTRACTION_LEVEL = {
    "zero": [0],
    "one": [1],
    "easy": [0, 1, 2],
    "middle": [1, 2, 3],
    "hard": [2, 3, 4, 5],
    "normal": [0, 1, 2, 3, 4, 5],
}


@functools.lru_cache(maxsize=1)
def _load_openha_open_gui_actions() -> Dict[str, Any]:
    if not OPENHA_OPEN_GUI_ACTIONS_ASSET_PATH.exists():
        return {}
    try:
        data = json.loads(OPENHA_OPEN_GUI_ACTIONS_ASSET_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _seed_position_lookup_key(seed: Any, spawn_pos: Any) -> Optional[str]:
    try:
        seed_i = int(seed)
    except Exception:
        return None
    if not isinstance(spawn_pos, (list, tuple)) or len(spawn_pos) < 3:
        return None
    try:
        x = int(spawn_pos[0])
        y = int(spawn_pos[1])
        z = int(spawn_pos[2])
    except Exception:
        return None
    return f"{seed_i}_{x}_{y}_{z}"


def _normalize_openha_action_seq(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for step in value:
        if isinstance(step, dict):
            out.append(step)
    return out


def resolve_openha_init_actions_for_task(
    *,
    spec: EvalFamilySpec,
    task_cfg: Dict[str, Any],
    seed: Optional[int],
    spawn_pos: Optional[List[int]],
) -> Tuple[List[Dict[str, Any]], str]:
    family = spec.family_key
    if family in {"kill_entity", "interact_block"}:
        return [], "openha.init_actions.none"
    if family == "mine_block":
        # OpenHA uses two no-op steps after reset as a brief settle period.
        return [{}, {}], "openha.init_actions.mine_block.noop_x2"

    gui_actions = _load_openha_open_gui_actions()
    if not gui_actions:
        return [], "openha.init_actions.asset_missing"

    if family == "craft_item":
        if bool(task_cfg.get("need_crafting_table")):
            key = _seed_position_lookup_key(seed, spawn_pos)
            if not key:
                return [], "openha.init_actions.crafting_table.missing_seed_key"
            seq = _normalize_openha_action_seq(gui_actions.get("crafting_table", {}).get(key))
            if not seq:
                return [], f"openha.init_actions.crafting_table.missing:{key}"
            return seq, f"openha.init_actions.crafting_table:{key}"
        seq = _normalize_openha_action_seq(gui_actions.get("inventory", {}).get("init"))
        if not seq:
            return [], "openha.init_actions.inventory.missing"
        return seq, "openha.init_actions.inventory:init"

    if family == "smelt_item":
        if not bool(task_cfg.get("need_furnace")):
            return [], "openha.init_actions.furnace.skip_need_furnace_false"
        key = _seed_position_lookup_key(seed, spawn_pos)
        if not key:
            return [], "openha.init_actions.furnace.missing_seed_key"
        seq = _normalize_openha_action_seq(gui_actions.get("furnace", {}).get(key))
        if not seq:
            return [], f"openha.init_actions.furnace.missing:{key}"
        return seq, f"openha.init_actions.furnace:{key}"

    return [], f"openha.init_actions.unsupported_family:{family}"


def pick_init_tool(task_cfg: Dict, *, fallback_tool: str) -> str:
    tools = task_cfg.get("tool", [])
    valid_tools = [tool for tool in tools if isinstance(tool, str) and tool and tool != "air"]
    if valid_tools:
        return random.choice(valid_tools)
    return fallback_tool


def sample_family_spawn_offset(spec: EvalFamilySpec) -> Optional[Tuple[float, float]]:
    if not spec.uses_summon_target:
        return None
    assert spec.summon_x_range is not None
    assert spec.summon_z_range is not None
    return (
        random.uniform(spec.summon_x_range[0], spec.summon_x_range[1]),
        random.uniform(spec.summon_z_range[0], spec.summon_z_range[1]),
    )


def load_task_commands(task_cfg: Dict) -> List[str]:
    raw = task_cfg.get("commands")
    if raw is None:
        raw = task_cfg.get("command", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    commands: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        cmd = item.strip()
        if not cmd:
            continue
        if cmd.startswith("/"):
            cmd = cmd[1:].strip()
        if cmd:
            commands.append(cmd)
    return commands


def detect_task_command_overrides(task_commands: List[str]) -> Dict[str, bool]:
    override = {"weather": False, "time": False, "mob_spawning": False}
    for cmd in task_commands:
        normalized = cmd.strip().lower()
        if re.match(r"^weather\s+\S+", normalized):
            override["weather"] = True
        if re.match(r"^(time\s+set|set\s+time)\s+\S+", normalized):
            override["time"] = True
        if re.match(r"^gamerule\s+domobspawning\s+(true|false)\b", normalized):
            override["mob_spawning"] = True
    return override


def resolve_distraction_level_for_task(
    *,
    task_cfg: Dict,
    requested_level: str,
    kind: str,
) -> str:
    level_maps = {
        "equip": OPENHA_EQUIP_DISTRACTION_LEVEL,
        "inventory": OPENHA_INVENTORY_DISTRACTION_LEVEL,
    }
    default_keys = {
        "equip": "equip_distraction_level_default",
        "inventory": "inventory_distraction_level_default",
    }
    if kind not in level_maps:
        raise RuntimeError(f"Unsupported distraction level kind: {kind!r}")
    valid_levels = level_maps[kind]

    def _normalize_level(raw: Any) -> str:
        if raw is None:
            return ""
        return str(raw).strip().lower()

    def _pick_supported(raw: Any) -> Optional[str]:
        s = _normalize_level(raw)
        if s in valid_levels:
            return s
        return None

    level = _normalize_level(requested_level) or "normal"
    if level != "difficulty":
        if level in valid_levels:
            return level
        raise RuntimeError(
            f"Unsupported {kind} distraction level: {requested_level!r}. "
            f"Supported: {sorted(list(valid_levels.keys()) + ['difficulty'])}"
        )

    task_difficulty = _pick_supported(task_cfg.get("difficulty"))
    if task_difficulty:
        return task_difficulty

    meta = task_cfg.get("_mcbots_meta")
    if isinstance(meta, dict):
        hinted = _normalize_level(meta.get(default_keys[kind]))
        if hinted == "difficulty":
            if task_difficulty:
                return task_difficulty
        elif hinted in valid_levels:
            return hinted

    return "normal"


def resolve_score_criterion_for_task(spec: EvalFamilySpec, task_name: str, task_cfg: Dict) -> str:
    suffix = task_name.split(":", 1)[1] if ":" in task_name else task_name
    if spec.family_key == "interact_block":
        # OpenHA interact tasks are named like custom:interact_with_anvil.
        return f"minecraft.custom:minecraft.{suffix}"
    if spec.family_key == "kill_entity":
        return f"minecraft.killed:minecraft.{suffix}"
    if spec.family_key == "mine_block":
        return f"minecraft.mined:minecraft.{suffix}"
    if spec.family_key in {"craft_item", "smelt_item"}:
        crafted_item = _resolve_goal_item_suffix(task_name=task_name, task_cfg=task_cfg)
        return f"minecraft.crafted:minecraft.{crafted_item}"
    return spec.score_criterion


def resolve_summon_entity_for_task(spec: EvalFamilySpec, task_name: str, task_cfg: Dict) -> Optional[str]:
    if spec.family_key != "kill_entity":
        return spec.summon_entity
    suffix = task_name.split(":", 1)[1] if ":" in task_name else task_name
    suffix = suffix.strip()
    if not suffix:
        return spec.summon_entity
    if ":" in suffix:
        return suffix
    return f"minecraft:{suffix}"


def build_family_extra_init_commands(
    *,
    spec: EvalFamilySpec,
    task_cfg: Dict,
    player_name: str,
    inventory_distraction_mode: str = "off",
    inventory_distraction_level: str = "normal",
) -> List[str]:
    if spec.family_key == "interact_block":
        commands = _build_interact_block_init_commands(task_cfg=task_cfg, player_name=player_name)
        commands.extend(
            _build_init_inventory_commands(
                spec=spec,
                player_name=player_name,
                task_cfg=task_cfg,
                inventory_distraction_mode=inventory_distraction_mode,
                inventory_distraction_level=inventory_distraction_level,
            )
        )
        return commands
    if spec.family_key in {"craft_item", "smelt_item"}:
        commands: List[str] = []
        commands.extend(_build_craft_smelt_setup_commands(spec=spec, player_name=player_name, task_cfg=task_cfg))
        commands.extend(
            _build_init_inventory_commands(
                spec=spec,
                player_name=player_name,
                task_cfg=task_cfg,
                inventory_distraction_mode=inventory_distraction_mode,
                inventory_distraction_level=inventory_distraction_level,
            )
        )
        return commands
    return []


def _build_interact_block_init_commands(*, task_cfg: Dict, player_name: str) -> List[str]:
    blocks = task_cfg.get("block")
    if not isinstance(blocks, list):
        return []

    commands: List[str] = []
    for item in blocks:
        if not isinstance(item, dict) or len(item) != 1:
            continue
        block_name, rel_pos = next(iter(item.items()))
        if not isinstance(block_name, str):
            continue
        if not isinstance(rel_pos, (list, tuple)) or len(rel_pos) < 3:
            continue
        try:
            dx = int(rel_pos[0])
            dy = int(rel_pos[1])
            dz = int(rel_pos[2])
        except Exception:
            continue
        bn = block_name.strip()
        if not bn:
            continue
        if ":" not in bn:
            bn = f"minecraft:{bn}"
        commands.append(
            f"execute as {player_name} at {player_name} run setblock ^{dx} ^{dy} ^{dz} {bn}"
        )
    return commands


def _resolve_goal_item_suffix(*, task_name: str, task_cfg: Dict) -> str:
    goal_raw = task_cfg.get("goal")
    if isinstance(goal_raw, str) and goal_raw.strip():
        goal = goal_raw.strip()
        if ":" in goal:
            ns, item = goal.split(":", 1)
            if ns == "minecraft" and item:
                return item
        return goal.replace(" ", "_")

    if ":" in task_name:
        return task_name.split(":", 1)[1].strip().replace(" ", "_")
    return task_name.strip().replace(" ", "_")


def _parse_min_quantity(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(1, int(value))
    if isinstance(value, float):
        return max(1, int(value))
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.lower() == "random":
        return 1
    if s.isdigit():
        return max(1, int(s))
    lower = 1
    upper = 64
    matched = False
    for part in s.split(","):
        p = part.strip()
        if not p:
            continue
        m = re.fullmatch(r"(<=|>=|<|>|==)\s*(\d+)", p)
        if not m:
            continue
        matched = True
        op, raw_num = m.groups()
        n = int(raw_num)
        if op == "==":
            lower = max(lower, n)
            upper = min(upper, n)
        elif op == ">=":
            lower = max(lower, n)
        elif op == ">":
            lower = max(lower, n + 1)
        elif op == "<=":
            upper = min(upper, n)
        elif op == "<":
            upper = min(upper, n - 1)
    if not matched:
        return None
    if lower > upper:
        return 1
    return max(1, lower)


def _inventory_slot_to_item_replace_target(slot: int) -> str:
    if not (0 <= slot <= 40):
        raise RuntimeError(f"slot out of supported range: {slot}")
    if slot == 0:
        return "weapon.mainhand"
    if slot == 40:
        return "weapon.offhand"
    if slot == 39:
        return "armor.head"
    if slot == 38:
        return "armor.chest"
    if slot == 37:
        return "armor.legs"
    if slot == 36:
        return "armor.feet"
    if 1 <= slot <= 8:
        return f"hotbar.{slot}"
    return f"inventory.{slot - 9}"


def _parse_slot_index(raw_slot: Any) -> Optional[int]:
    if isinstance(raw_slot, bool):
        return None
    if isinstance(raw_slot, int):
        return raw_slot
    if isinstance(raw_slot, str) and raw_slot.strip().isdigit():
        return int(raw_slot.strip())
    return None


def _craft_smelt_forbidden_inventory_slots(*, spec: EvalFamilySpec, task_cfg: Dict) -> set[int]:
    meta = task_cfg.get("_mcbots_meta")
    if isinstance(meta, dict):
        raw_slots = meta.get("inventory_forbidden_slots_default")
        if isinstance(raw_slots, list):
            parsed: set[int] = set()
            for raw in raw_slots:
                slot = _parse_slot_index(raw)
                if slot is not None and 0 <= slot <= 40:
                    parsed.add(slot)
            if parsed:
                return parsed
    if spec.family_key == "smelt_item":
        return {0}
    if spec.family_key == "craft_item" and bool(task_cfg.get("need_crafting_table")):
        return {0}
    if spec.family_key == "interact_block":
        return {0}
    return set()


@functools.lru_cache(maxsize=1)
def _load_openha_inventory_item_pool() -> List[str]:
    if not OPENHA_MC_CONSTANTS_ASSET_PATH.exists():
        return ["minecraft:stick", "minecraft:oak_planks", "minecraft:cobblestone"]
    try:
        data = json.loads(OPENHA_MC_CONSTANTS_ASSET_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ["minecraft:stick", "minecraft:oak_planks", "minecraft:cobblestone"]
    raw_items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        return ["minecraft:stick", "minecraft:oak_planks", "minecraft:cobblestone"]
    out: List[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        raw_type = item.get("type")
        if not isinstance(raw_type, str):
            continue
        item_id = _normalize_item_id(raw_type)
        if not item_id or item_id == "minecraft:air" or item_id in seen:
            continue
        seen.add(item_id)
        out.append(item_id)
    return out or ["minecraft:stick", "minecraft:oak_planks", "minecraft:cobblestone"]


@functools.lru_cache(maxsize=1)
def _load_openha_inventory_item_stack_sizes() -> Dict[str, int]:
    if not OPENHA_MC_CONSTANTS_ASSET_PATH.exists():
        return {}
    try:
        data = json.loads(OPENHA_MC_CONSTANTS_ASSET_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw_items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        return {}
    out: Dict[str, int] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        raw_type = item.get("type")
        if not isinstance(raw_type, str):
            continue
        item_id = _normalize_item_id(raw_type)
        if not item_id or item_id == "minecraft:air":
            continue
        stack_size = item.get("stackSize", 64)
        try:
            stack_size_i = int(stack_size)
        except Exception:
            stack_size_i = 64
        out[item_id] = max(1, min(64, stack_size_i))
    return out


def _sample_openha_like_random_quantity_for_item(item_id: str, *, one_p: float = 0.7) -> int:
    stack_sizes = _load_openha_inventory_item_stack_sizes()
    max_stack = max(1, int(stack_sizes.get(item_id, 64)))
    if max_stack <= 1:
        return 1
    if random.random() < one_p:
        return 1
    return random.randint(1, max_stack)


def _sample_inventory_distraction_entries(
    *,
    available_random_slots: List[int],
    mode: str,
    level: str,
) -> List[Tuple[int, str, int]]:
    if mode == "off":
        return []
    if mode != "random":
        raise RuntimeError(f"Unsupported inventory distraction mode: {mode}")
    level_key = (level or "normal").strip().lower()
    sample_space = OPENHA_INVENTORY_DISTRACTION_LEVEL.get(level_key)
    if not sample_space:
        raise RuntimeError(
            f"Unsupported inventory distraction level: {level!r}. "
            f"Supported: {sorted(OPENHA_INVENTORY_DISTRACTION_LEVEL)}"
        )
    if not available_random_slots:
        return []
    sample_num = min(random.choice(sample_space), len(available_random_slots))
    if sample_num <= 0:
        return []
    pool = _load_openha_inventory_item_pool()
    remaining_slots = list(available_random_slots)
    out: List[Tuple[int, str, int]] = []
    for _ in range(sample_num):
        slot = int(random.choice(remaining_slots))
        remaining_slots.remove(slot)
        item_id = random.choice(pool)
        qty = _sample_openha_like_random_quantity_for_item(item_id)
        out.append((slot, item_id, qty))
    return out


def _build_init_inventory_commands(
    *,
    spec: EvalFamilySpec,
    player_name: str,
    task_cfg: Dict,
    inventory_distraction_mode: str = "off",
    inventory_distraction_level: str = "normal",
) -> List[str]:
    raw_items = task_cfg.get("init_inventory")
    if not isinstance(raw_items, list):
        raw_items = []
    fixed_entries: List[Tuple[int, str, int]] = []
    random_entries: List[Tuple[str, int]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        raw_type = item.get("type")
        if not isinstance(raw_type, str):
            continue
        item_id = _normalize_item_id(raw_type)
        if not item_id:
            continue
        qty = _parse_min_quantity(item.get("quantity"))
        if qty is None:
            qty = 1
        slot = item.get("slot")
        if isinstance(slot, str) and slot.strip().lower() == "random":
            random_entries.append((item_id, qty))
            continue
        parsed_slot = _parse_slot_index(slot)
        if parsed_slot is None or parsed_slot < 0:
            random_entries.append((item_id, qty))
            continue
        fixed_entries.append((parsed_slot, item_id, qty))

    forbidden_slots = _craft_smelt_forbidden_inventory_slots(spec=spec, task_cfg=task_cfg)
    used_slots = {slot for slot, _item_id, _qty in fixed_entries}
    available_random_slots = [
        slot
        for slot in range(OPENHA_MIN_SLOT_IDX, OPENHA_MAX_INVENTORY_SLOT_IDX + 1)
        if slot not in forbidden_slots and slot not in used_slots
    ]

    commands: List[str] = []
    for slot, item_id, qty in fixed_entries:
        target = _inventory_slot_to_item_replace_target(slot)
        commands.append(f"item replace entity {player_name} {target} with {item_id} {qty}")
    for item_id, qty in random_entries:
        if not available_random_slots:
            commands.append(f"give {player_name} {item_id} {qty}")
            continue
        slot = int(random.choice(available_random_slots))
        available_random_slots.remove(slot)
        target = _inventory_slot_to_item_replace_target(slot)
        commands.append(f"item replace entity {player_name} {target} with {item_id} {qty}")

    # Optional OpenHA-style inventory distraction filler items.
    for slot, item_id, qty in _sample_inventory_distraction_entries(
        available_random_slots=available_random_slots,
        mode=inventory_distraction_mode,
        level=inventory_distraction_level,
    ):
        target = _inventory_slot_to_item_replace_target(slot)
        commands.append(f"item replace entity {player_name} {target} with {item_id} {qty}")
    return commands


def _build_craft_smelt_setup_commands(*, spec: EvalFamilySpec, player_name: str, task_cfg: Dict) -> List[str]:
    commands: List[str] = []
    if spec.family_key == "craft_item" and bool(task_cfg.get("need_crafting_table")):
        commands.append(
            f"execute as {player_name} at {player_name} run setblock ^0 ^0 ^5 minecraft:crafting_table"
        )
    if spec.family_key == "smelt_item" and bool(task_cfg.get("need_furnace")):
        commands.append(
            f"execute as {player_name} at {player_name} run setblock ^0 ^0 ^5 minecraft:furnace"
        )
    return commands


def build_pre_player_init_commands(
    *,
    init_weather: str,
    init_mob_spawning: str,
    init_time: str,
    init_clear_existing_hostiles: str,
    task_overrides: Dict[str, bool],
) -> List[str]:
    commands: List[str] = []
    if init_mob_spawning in {"true", "false"} and not task_overrides["mob_spawning"]:
        commands.append(f"gamerule doMobSpawning {init_mob_spawning}")
    if init_weather != "keep" and not task_overrides["weather"]:
        commands.append(f"weather {init_weather}")
    if init_time.strip() and not task_overrides["time"]:
        commands.append(f"time set {init_time.strip()}")
    if init_clear_existing_hostiles == "true":
        for entity_type in COMMON_HOSTILE_ENTITY_TYPES:
            commands.append(f"kill @e[type={entity_type}]")
    return commands


def _normalize_item_id(raw_item: str) -> Optional[str]:
    item = raw_item.strip()
    if not item:
        return None
    if item in {"air", "minecraft:air"}:
        return None
    if ":" not in item:
        item = f"minecraft:{item}"
    return item


def _parse_fixed_equip_json(raw_json: str) -> Dict[str, str]:
    text = (raw_json or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Invalid equip distraction JSON: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError("Equip distraction JSON must be an object, e.g. {\"head\":\"carved_pumpkin\"}")

    normalized: Dict[str, str] = {}
    for raw_slot, raw_item in data.items():
        slot = str(raw_slot).strip().lower()
        if slot not in EQUIP_SLOT_ITEM_REPLACE_TARGET:
            raise RuntimeError(
                f"Unsupported equip slot in fixed JSON: {raw_slot!r}. "
                f"Supported: {sorted(EQUIP_SLOT_ITEM_REPLACE_TARGET)}"
            )
        if not isinstance(raw_item, str):
            raise RuntimeError(f"Equip item for slot {raw_slot!r} must be string")
        item_id = _normalize_item_id(raw_item)
        if item_id is None:
            continue
        normalized[slot] = item_id
    return normalized


def _parse_random_head_candidates(raw_csv: str) -> List[str]:
    raw = (raw_csv or "").strip()
    if not raw:
        return []
    candidates = [p.strip() for p in raw.split(",")]
    out: List[str] = []
    seen: set[str] = set()
    for item in candidates:
        item_id = _normalize_item_id(item)
        if item_id is None or item_id in seen:
            continue
        seen.add(item_id)
        out.append(item_id)
    return out


@functools.lru_cache(maxsize=1)
def _load_openha_equipment_pool() -> Dict[str, List[str]]:
    if not OPENHA_EQUIPMENT_ASSET_PATH.exists():
        return {k: list(v) for k, v in DEFAULT_RANDOM_EQUIP_DISTRACTION_POOL.items()}
    try:
        data = json.loads(OPENHA_EQUIPMENT_ASSET_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {k: list(v) for k, v in DEFAULT_RANDOM_EQUIP_DISTRACTION_POOL.items()}
    if not isinstance(data, dict):
        return {k: list(v) for k, v in DEFAULT_RANDOM_EQUIP_DISTRACTION_POOL.items()}

    normalized: Dict[str, List[str]] = {}
    for slot in ["head", "chest", "legs", "feet", "offhand"]:
        raw_list = data.get(slot)
        if not isinstance(raw_list, list):
            continue
        slot_items: List[str] = []
        seen: set[str] = set()
        for raw_item in raw_list:
            if not isinstance(raw_item, str):
                continue
            item_id = _normalize_item_id(raw_item)
            if item_id is None or item_id in seen:
                continue
            seen.add(item_id)
            slot_items.append(item_id)
        if slot_items:
            normalized[slot] = slot_items

    # Keep head fallback even if asset exists but malformed.
    if "head" not in normalized:
        normalized["head"] = list(DEFAULT_RANDOM_EQUIP_DISTRACTION_POOL["head"])
    return normalized


def _sample_openha_like_random_equip_map(
    *,
    random_head_candidates_csv: str,
    level: str,
) -> Dict[str, str]:
    pool = _load_openha_equipment_pool()

    # Allow caller to override head candidates while still using OpenHA defaults for other slots.
    head_override = _parse_random_head_candidates(random_head_candidates_csv)
    if head_override:
        pool = dict(pool)
        pool["head"] = head_override

    available_slots = [slot for slot in OPENHA_RANDOM_EQUIP_SLOT_ORDER if pool.get(slot)]
    if not available_slots:
        return {}

    level_key = (level or "normal").strip().lower()
    sample_space = OPENHA_EQUIP_DISTRACTION_LEVEL.get(level_key)
    if not sample_space:
        raise RuntimeError(
            f"Unsupported equip distraction level: {level!r}. "
            f"Supported: {sorted(OPENHA_EQUIP_DISTRACTION_LEVEL)}"
        )
    sample_num = min(random.choice(sample_space), len(available_slots))
    if sample_num <= 0:
        return {}

    remaining_slots = list(available_slots)
    equip_map: Dict[str, str] = {}
    for _ in range(sample_num):
        slot = random.choice(remaining_slots)
        remaining_slots.remove(slot)
        candidates = pool.get(slot, [])
        if not candidates:
            continue
        equip_map[slot] = random.choice(candidates)
    return equip_map


def build_equip_distraction_commands(
    *,
    player_name: str,
    mode: str,
    random_level: str,
    fixed_json: str,
    random_head_candidates_csv: str,
) -> List[str]:
    if mode == "off":
        return []

    equip_map: Dict[str, str] = {}
    if mode == "fixed":
        equip_map = _parse_fixed_equip_json(fixed_json)
    elif mode == "random":
        equip_map = _sample_openha_like_random_equip_map(
            random_head_candidates_csv=random_head_candidates_csv,
            level=random_level,
        )
    else:
        raise RuntimeError(f"Unsupported equip distraction mode: {mode}")

    commands: List[str] = []
    for slot in ["head", "chest", "legs", "feet", "offhand"]:
        item_id = equip_map.get(slot)
        if not item_id:
            continue
        target_slot = EQUIP_SLOT_ITEM_REPLACE_TARGET[slot]
        commands.append(f"item replace entity {player_name} {target_slot} with {item_id} 1")
    return commands


def build_player_init_commands(
    *,
    spec: EvalFamilySpec,
    player_name: str,
    spawn_pos: List[int],
    init_tool: str,
    summon_offset: Optional[Tuple[float, float]] = None,
    summon_entity_override: Optional[str] = None,
    equip_distraction_mode: str = "off",
    equip_distraction_level: str = "normal",
    equip_distraction_fixed_json: str = "",
    equip_distraction_random_head_candidates: str = "",
    inventory_distraction_mode: str = "off",
    inventory_distraction_level: str = "normal",
) -> List[str]:
    _ = (inventory_distraction_mode, inventory_distraction_level)
    x, y, z = spawn_pos
    commands: List[str] = [
        "gamerule sendCommandFeedback false",
        f"clear {player_name}",
        f"effect clear {player_name}",
        f"gamemode survival {player_name}",
        f"tp {player_name} {x} {y} {z}",
    ]
    if init_tool and init_tool != "air":
        commands.append(f"give {player_name} minecraft:{init_tool} 1")
    commands.extend(
        build_equip_distraction_commands(
            player_name=player_name,
            mode=equip_distraction_mode,
            random_level=equip_distraction_level,
            fixed_json=equip_distraction_fixed_json,
            random_head_candidates_csv=equip_distraction_random_head_candidates,
        )
    )

    if spec.uses_summon_target:
        if summon_offset is None:
            summon_offset = sample_family_spawn_offset(spec)
        if summon_offset is None:
            raise RuntimeError("summon_offset missing for summon-target family")
        summon_entity = summon_entity_override or spec.summon_entity
        if not summon_entity:
            raise RuntimeError("summon_entity missing for summon-target family")
        dx, dz = summon_offset
        commands.append(
            f"execute as {player_name} at {player_name} run summon {summon_entity} "
            f"~{dx:.3f} ~ ~{dz:.3f} {{Age:0}}"
        )
    return commands
