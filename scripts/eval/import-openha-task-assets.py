#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


FORMAT_VERSION = "mcbots.openha-import.v1"
DEFAULT_INFERRED_Y_FROM_2D_POSITION = 64
OPENHA_OPEN_GUI_ACTIONS_FILENAME = "open_gui_actions.json"
DEFAULT_LOCAL_OPEN_GUI_ACTIONS_ASSET = "eval/openha_assets/open_gui_actions.1.16.json"


SPAWN_SOURCES: Dict[str, str] = {
    "kill_entity": "kill_entity.json",
    "mine_block": "mine_block.json",
    "interact_block": "interact_block.json",
    "craft_item": "craft_item.json",
    "smelt_item": "smelt_item.json",
}


FAMILY_ALIGNMENT_HINTS: Dict[str, Dict[str, Any]] = {
    "kill_entity": {
        "max_steps": 600,
        "callbacks": ["init_inventory", "commands", "mobs"],
        "reward_event": "kill_entity",
        "difficulty_zero_tool_behavior": "exclude-empty-tool",
        "equip_distraction_level_default": "normal",
        "inventory_distraction_level_default": "difficulty",
    },
    "mine_block": {
        "max_steps": 600,
        "callbacks": ["init_inventory", "commands"],
        "reward_event": "mine_block",
        "difficulty_zero_tool_behavior": "exclude-empty-tool",
        "equip_distraction_level_default": "normal",
        "inventory_distraction_level_default": "difficulty",
    },
    "interact_block": {
        "max_steps": 600,
        "callbacks": ["init_inventory", "commands"],
        "reward_event": "custom",
        "equip_distraction_level_default": "difficulty",
        "inventory_distraction_level_default": "difficulty",
        "forbidden_slots_default": [0],
    },
    "craft_item": {
        "max_steps": 600,
        "callbacks": ["init_inventory", "commands"],
        "reward_event": "craft_item",
        "equip_distraction_level_default": "difficulty",
        "inventory_distraction_level_default": "difficulty",
        "forbidden_slots_condition": "if need_crafting_table then [0] else []",
        "init_actions_source": "open_gui_actions(crafting_table|inventory)",
    },
    "smelt_item": {
        "max_steps": 600,
        "callbacks": ["init_inventory", "commands"],
        "reward_event": "craft_item",  # OpenHA smelt tasks still use craft_item reward event.
        "equip_distraction_level_default": "difficulty",
        "inventory_distraction_level_default": "difficulty",
        "forbidden_slots_default": [0],
        "init_actions_source": "open_gui_actions(furnace)",
    },
}


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def parse_position(value: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        return (int(value[0]), int(value[1]), int(value[2]))
    except Exception:
        return None


def parse_position_xz_2d(value: Any) -> Optional[Tuple[int, int]]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        return (int(value[0]), int(value[1]))
    except Exception:
        return None


def normalize_dimension(value: Any) -> str:
    if value is None:
        return "overworld"
    if isinstance(value, int):
        return {0: "overworld", -1: "the_nether", 1: "the_end"}.get(value, str(value))
    if isinstance(value, str):
        s = value.strip().lower()
        aliases = {
            "0": "overworld",
            "-1": "the_nether",
            "1": "the_end",
            "minecraft:overworld": "overworld",
            "overworld": "overworld",
            "nether": "the_nether",
            "minecraft:the_nether": "the_nether",
            "the_nether": "the_nether",
            "end": "the_end",
            "minecraft:the_end": "the_end",
            "the_end": "the_end",
        }
        return aliases.get(s, s or "overworld")
    return "overworld"


def infer_dimension_from_labels(labels: List[str]) -> Optional[str]:
    normalized = {str(x).strip().lower() for x in labels if str(x).strip()}
    if any(x in {"nether", "the_nether", "minecraft:the_nether"} for x in normalized):
        return "the_nether"
    if any(x in {"end", "the_end", "minecraft:the_end"} for x in normalized):
        return "the_end"
    if normalized:
        return "overworld"
    return None


def family_from_spawn_filename(name: str) -> str:
    stem = Path(name).stem
    if stem not in SPAWN_SOURCES:
        raise KeyError(stem)
    return stem


def canonical_task_name(family: str, raw_task_name: str) -> str:
    # OpenHA craft_item has one historical outlier key using spaces.
    if family == "craft_item" and raw_task_name.startswith("craft item "):
        suffix = raw_task_name[len("craft item ") :].strip()
        if suffix:
            return f"craft_item:{suffix.replace(' ', '_')}"
    # OpenHA smelt tasks are stored in smelt_item.json but use craft_item:* keys.
    # Canonicalize to avoid collisions with craft_item family tasks in merged manifests.
    if family == "smelt_item" and raw_task_name.startswith("craft_item:"):
        return "smelt_item:" + raw_task_name.split(":", 1)[1]
    return raw_task_name


def infer_goal_item(family: str, raw_task_name: str, spawn_spec: Dict[str, Any]) -> Optional[str]:
    if "goal" in spawn_spec and isinstance(spawn_spec["goal"], str):
        return spawn_spec["goal"]
    if ":" in raw_task_name:
        suffix = raw_task_name.split(":", 1)[1]
        if family in {"kill_entity", "mine_block", "craft_item", "smelt_item"}:
            return suffix
    return None


def infer_inventory_forbidden_slots_default(family: str, spawn_spec: Dict[str, Any]) -> List[int]:
    if family == "smelt_item":
        return [0]
    if family == "interact_block":
        return [0]
    if family == "craft_item" and bool(spawn_spec.get("need_crafting_table")):
        return [0]
    return []


def exact_instructions(
    instructions_map: Dict[str, Any],
    *,
    raw_task_name: str,
    canonical_name: str,
) -> Tuple[List[str], Optional[str]]:
    candidates = [raw_task_name]
    if canonical_name != raw_task_name:
        candidates.append(canonical_name)
    for key in candidates:
        v = instructions_map.get(key)
        if isinstance(v, list):
            return [str(x) for x in v], key
    return [], None


@dataclass
class ImportStats:
    total_entries: int = 0
    canonical_collisions: int = 0
    raw_duplicate_keys_across_families: int = 0


def normalize_seed_entry(seed_entry: Dict[str, Any]) -> Dict[str, Any]:
    seed_int = parse_int(seed_entry.get("seed"))
    pos = parse_position(seed_entry.get("position"))
    position_inferred_from_2d = False
    if pos is None:
        pos2d = parse_position_xz_2d(seed_entry.get("position"))
        if pos2d is not None:
            pos = (pos2d[0], DEFAULT_INFERRED_Y_FROM_2D_POSITION, pos2d[1])
            position_inferred_from_2d = True
    has_dimension = "dimension" in seed_entry
    return {
        "seed": seed_int,
        "seed_raw": seed_entry.get("seed"),
        "position": list(pos) if pos is not None else None,
        "position_raw": seed_entry.get("position"),
        "position_inferred_from_2d": position_inferred_from_2d,
        "position_inferred_default_y": DEFAULT_INFERRED_Y_FROM_2D_POSITION if position_inferred_from_2d else None,
        "dimension": normalize_dimension(seed_entry.get("dimension")) if has_dimension else None,
        "dimension_explicit": has_dimension,
        "dimension_source": "explicit_seed" if has_dimension else None,
        "raw": seed_entry,
    }


def summarize_dimension_sources(seeds: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for seed in seeds:
        if not isinstance(seed, dict):
            continue
        src = str(seed.get("dimension_source") or "").strip()
        if not src:
            src = "unknown"
        counts[src] = counts.get(src, 0) + 1
    return {k: counts[k] for k in sorted(counts)}


def iter_spawn_entries(
    spawn_root: Path,
    *,
    selected_families: Optional[set[str]],
) -> Iterable[Tuple[str, str, Dict[str, Any]]]:
    for family, filename in SPAWN_SOURCES.items():
        if selected_families and family not in selected_families:
            continue
        p = spawn_root / filename
        raw = load_json(p)
        if not isinstance(raw, dict):
            raise RuntimeError(f"Expected JSON object in {p}")
        for task_name, spawn_spec in raw.items():
            if not isinstance(spawn_spec, dict):
                continue
            yield family, task_name, spawn_spec


def build_record(
    *,
    family: str,
    raw_task_name: str,
    spawn_spec: Dict[str, Any],
    instructions_map: Dict[str, Any],
    spawn_filename: str,
    default_pregen_radius_chunks: int,
) -> Dict[str, Any]:
    canonical_name = canonical_task_name(family, raw_task_name)
    instructions, instruction_key = exact_instructions(
        instructions_map,
        raw_task_name=raw_task_name,
        canonical_name=canonical_name,
    )
    normalized_seeds: List[Dict[str, Any]] = []
    raw_seeds = spawn_spec.get("seeds")
    if isinstance(raw_seeds, list):
        normalized_seeds = [normalize_seed_entry(s) for s in raw_seeds if isinstance(s, dict)]

    positions = [s["position"] for s in normalized_seeds if s.get("position") is not None]
    tools = spawn_spec.get("tool") if isinstance(spawn_spec.get("tool"), list) else []
    labels = spawn_spec.get("label") if isinstance(spawn_spec.get("label"), list) else []
    inferred_dim_from_labels = infer_dimension_from_labels([str(x) for x in labels])
    task_level_dimension = normalize_dimension(spawn_spec.get("dimension")) if "dimension" in spawn_spec else None

    for seed in normalized_seeds:
        if seed.get("dimension") is None:
            if task_level_dimension is not None:
                seed["dimension"] = task_level_dimension
                seed["dimension_source"] = "task_level"
            elif inferred_dim_from_labels is not None:
                seed["dimension"] = inferred_dim_from_labels
                seed["dimension_source"] = "label_inferred"
            else:
                seed["dimension"] = "overworld"
                seed["dimension_source"] = "default_overworld"

    dimensions = sorted({str(s.get("dimension", inferred_dim_from_labels or "overworld")) for s in normalized_seeds})
    if not dimensions:
        dimensions = [task_level_dimension or inferred_dim_from_labels or "overworld"]
    dimension_source_counts = summarize_dimension_sources(normalized_seeds)

    goal_item = infer_goal_item(family, raw_task_name, spawn_spec)

    return {
        "canonical_task_name": canonical_name,
        "openha_task_name": raw_task_name,
        "family": family,
        "source_spawn_file": spawn_filename,
        "goal_item": goal_item,
        "instructions": instructions,
        "instruction_source_key": instruction_key,
        "instruction_count": len(instructions),
        "spawn_spec": spawn_spec,
        "seeds": normalized_seeds,
        "seed_count": len(normalized_seeds),
        "seed_positions": positions,
        "dimensions": dimensions or ["overworld"],
        "dimension_source_counts": dimension_source_counts,
        "labels": [str(x) for x in labels],
        "tools": [str(x) for x in tools],
        "snapshot_hints": {
            "default_dimension": dimensions[0] if len(dimensions) == 1 else (task_level_dimension or inferred_dim_from_labels or "overworld"),
            "suggested_pregen_radius_chunks": int(default_pregen_radius_chunks),
            "seed_positions_count": len(positions),
        },
        "alignment_hints": FAMILY_ALIGNMENT_HINTS.get(family, {}),
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Import OpenHA spawn/instruction assets into a normalized mcbots manifest."
    )
    p.add_argument(
        "--openha-assets-root",
        default="/export/path/to/projects/OpenHA/openagents/assets",
        help="Path to OpenHA openagents/assets directory.",
    )
    p.add_argument(
        "--output-dir",
        default="eval/openha_assets/imported",
        help="Output directory for imported manifests.",
    )
    p.add_argument(
        "--family",
        action="append",
        default=[],
        choices=sorted(SPAWN_SOURCES.keys()),
        help="Limit import to one or more families (repeatable).",
    )
    p.add_argument(
        "--default-pregen-radius-chunks",
        type=int,
        default=3,
        help="Write this suggested pregen radius into snapshot_hints.",
    )
    p.add_argument(
        "--copy-open-gui-actions-to",
        default=DEFAULT_LOCAL_OPEN_GUI_ACTIONS_ASSET,
        help=(
            "Copy OpenHA open_gui_actions.json to this project path (empty to disable). "
            "Used by evaluator init_actions replay alignment."
        ),
    )
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    openha_assets_root = Path(args.openha_assets_root).resolve()
    spawn_root = openha_assets_root / "spawns"
    instructions_path = openha_assets_root / "instructions.json"
    open_gui_actions_path = openha_assets_root / OPENHA_OPEN_GUI_ACTIONS_FILENAME
    output_dir = (project_root / args.output_dir).resolve() if not Path(args.output_dir).is_absolute() else Path(args.output_dir)
    selected_families = set(args.family) if args.family else None

    if not spawn_root.is_dir():
        raise RuntimeError(f"OpenHA spawn root not found: {spawn_root}")
    instructions_raw = load_json(instructions_path)
    if not isinstance(instructions_raw, dict):
        raise RuntimeError(f"Expected JSON object in {instructions_path}")
    instructions_map: Dict[str, Any] = instructions_raw

    stats = ImportStats()
    manifest_tasks: Dict[str, Dict[str, Any]] = {}
    family_configs: Dict[str, Dict[str, Any]] = {family: {} for family in SPAWN_SOURCES}
    raw_key_sources: Dict[str, List[str]] = {}
    canonical_name_collisions: List[Dict[str, Any]] = []

    for family, raw_task_name, spawn_spec in iter_spawn_entries(spawn_root, selected_families=selected_families):
        spawn_filename = SPAWN_SOURCES[family]
        record = build_record(
            family=family,
            raw_task_name=raw_task_name,
            spawn_spec=spawn_spec,
            instructions_map=instructions_map,
            spawn_filename=spawn_filename,
            default_pregen_radius_chunks=max(0, int(args.default_pregen_radius_chunks)),
        )
        canonical_name = record["canonical_task_name"]
        stats.total_entries += 1
        raw_key_sources.setdefault(raw_task_name, []).append(family)

        if canonical_name in manifest_tasks:
            canonical_name_collisions.append(
                {
                    "canonical_task_name": canonical_name,
                    "existing_family": manifest_tasks[canonical_name]["family"],
                    "existing_openha_task_name": manifest_tasks[canonical_name]["openha_task_name"],
                    "incoming_family": family,
                    "incoming_openha_task_name": raw_task_name,
                }
            )
            continue

        manifest_tasks[canonical_name] = record

        family_cfg_entry = dict(spawn_spec)
        if "dimension" not in family_cfg_entry and len(record["dimensions"]) == 1:
            family_cfg_entry["dimension"] = record["dimensions"][0]
        if isinstance(family_cfg_entry.get("seeds"), list):
            patched_seeds = []
            for seed_idx, raw_seed in enumerate(family_cfg_entry["seeds"]):
                if not isinstance(raw_seed, dict):
                    patched_seeds.append(raw_seed)
                    continue
                seed_copy = dict(raw_seed)
                if "dimension" not in seed_copy and seed_idx < len(record["seeds"]):
                    inferred_seed_dim = record["seeds"][seed_idx].get("dimension")
                    if inferred_seed_dim:
                        seed_copy["dimension"] = inferred_seed_dim
                if seed_idx < len(record["seeds"]):
                    inferred_dim_source = record["seeds"][seed_idx].get("dimension_source")
                    if inferred_dim_source:
                        seed_copy["dimension_source"] = inferred_dim_source
                if seed_idx < len(record["seeds"]):
                    inferred_pos = record["seeds"][seed_idx].get("position")
                    inferred_from_2d = bool(record["seeds"][seed_idx].get("position_inferred_from_2d"))
                    if inferred_from_2d and isinstance(inferred_pos, list) and len(inferred_pos) >= 3:
                        seed_copy["position"] = list(inferred_pos)
                patched_seeds.append(seed_copy)
            family_cfg_entry["seeds"] = patched_seeds
        family_cfg_entry["_mcbots_meta"] = {
            "canonical_task_name": canonical_name,
            "openha_task_name": raw_task_name,
            "family": family,
            "source_spawn_file": spawn_filename,
            "instruction_count": record["instruction_count"],
            "instruction_source_key": record["instruction_source_key"],
            "dimensions": record["dimensions"],
            "dimension_source_counts": record.get("dimension_source_counts", {}),
            "suggested_pregen_radius_chunks": record["snapshot_hints"]["suggested_pregen_radius_chunks"],
            "inventory_forbidden_slots_default": infer_inventory_forbidden_slots_default(family, spawn_spec),
            "equip_distraction_level_default": (
                FAMILY_ALIGNMENT_HINTS.get(family, {}).get("equip_distraction_level_default")
            ),
            "inventory_distraction_level_default": (
                FAMILY_ALIGNMENT_HINTS.get(family, {}).get("inventory_distraction_level_default")
            ),
            "has_inferred_2d_seed_position": any(
                bool(s.get("position_inferred_from_2d")) for s in record.get("seeds", [])
            ),
        }
        family_configs[family][canonical_name] = family_cfg_entry

    raw_duplicates = {
        k: sorted(set(v)) for k, v in raw_key_sources.items() if len(set(v)) > 1
    }
    stats.raw_duplicate_keys_across_families = len(raw_duplicates)
    stats.canonical_collisions = len(canonical_name_collisions)

    manifest = {
        "format_version": FORMAT_VERSION,
        "generated_at_utc": utc_ts(),
        "source": {
            "openha_assets_root": str(openha_assets_root),
            "spawn_root": str(spawn_root),
            "instructions_path": str(instructions_path),
            "selected_families": sorted(selected_families) if selected_families else sorted(SPAWN_SOURCES),
        },
        "stats": {
            "total_entries_seen": stats.total_entries,
            "total_tasks_imported": len(manifest_tasks),
            "families_imported": {k: len(v) for k, v in family_configs.items() if v},
            "raw_duplicate_keys_across_families": stats.raw_duplicate_keys_across_families,
            "canonical_collisions": stats.canonical_collisions,
        },
        "raw_duplicate_keys_across_families_detail": raw_duplicates,
        "canonical_name_collisions": canonical_name_collisions,
        "tasks": {k: manifest_tasks[k] for k in sorted(manifest_tasks)},
    }

    instructions_by_task = {
        k: manifest_tasks[k]["instructions"]
        for k in sorted(manifest_tasks)
        if manifest_tasks[k]["instructions"]
    }

    summary = {
        "format_version": FORMAT_VERSION,
        "generated_at_utc": manifest["generated_at_utc"],
        "stats": manifest["stats"],
        "families": manifest["stats"]["families_imported"],
        "example_tasks": sorted(list(manifest_tasks))[:20],
    }

    if args.dry_run:
        copy_dst = (args.copy_open_gui_actions_to or "").strip()
        if copy_dst:
            print(
                json.dumps(
                    {
                        "would_copy_open_gui_actions": str(open_gui_actions_path),
                        "to": str((project_root / copy_dst).resolve() if not Path(copy_dst).is_absolute() else Path(copy_dst)),
                        "exists": open_gui_actions_path.is_file(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    dump_json(output_dir / "openha_task_manifest.json", manifest)
    dump_json(output_dir / "instructions_by_task.json", instructions_by_task)
    dump_json(output_dir / "summary.json", summary)
    for family, cfg in family_configs.items():
        if not cfg:
            continue
        dump_json(output_dir / "task_configs" / f"{family}.json", {k: cfg[k] for k in sorted(cfg)})

    copy_open_gui_actions_to = (args.copy_open_gui_actions_to or "").strip()
    if copy_open_gui_actions_to:
        if not open_gui_actions_path.is_file():
            print(f"[warn] OpenHA GUI actions asset not found, skip copy: {open_gui_actions_path}")
        else:
            dst = (
                (project_root / copy_open_gui_actions_to).resolve()
                if not Path(copy_open_gui_actions_to).is_absolute()
                else Path(copy_open_gui_actions_to)
            )
            dump_json(dst, load_json(open_gui_actions_path))
            print(f"[info] copied OpenHA GUI actions asset to: {dst}")

    print(f"[info] wrote import outputs to: {output_dir}")
    print(f"[info] imported tasks: {len(manifest_tasks)}")
    print(f"[info] families: {manifest['stats']['families_imported']}")
    if raw_duplicates:
        print(f"[info] raw key duplicates across families (handled via canonicalization): {len(raw_duplicates)}")
    if canonical_name_collisions:
        print(f"[warn] canonical collisions skipped: {len(canonical_name_collisions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
