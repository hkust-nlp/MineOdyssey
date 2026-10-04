#!/usr/bin/env python3
"""Export a catalog navigation task, optionally staging verified private assets."""
import argparse
import gzip
import json
import re
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from eval.navigation import schema
from eval.navigation.schema import load_map
from eval.navigation.snapshots import _source_config_digest, sha256_file, verify_snapshot
from eval.harbor_agents.instructions import check_instruction, render_instruction, task_definition


def archive_tree(destination, files):
    # The gzip header also needs normalization: tar entry mtimes alone do not
    # prevent identical exports from getting different hashes/build-cache keys.
    with destination.open("wb") as output, gzip.GzipFile(
            filename="", mode="wb", fileobj=output, mtime=0) as compressed, \
            tarfile.open(fileobj=compressed, mode="w") as archive:
        for path, name in files:
            if path.is_symlink():
                raise ValueError(f"Symlinks cannot be exported: {name}")
            if not path.is_file():
                continue
            info = archive.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with path.open("rb") as source:
                archive.addfile(info, source)


def export_agent_source(destination):
    """Package the unchanged Agent without maps, evaluator source or answers."""
    files = [(path, str(path.relative_to(ROOT))) for path in sorted((ROOT / "agent").rglob("*"))
             if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    for name in ("eval/harbor_agents/__init__.py", "eval/harbor_agents/container.py",
                 "scripts/analysis/finalize_agent_messages.py", "uv.lock"):
        files.append((ROOT / name, name))
    archive_tree(destination, sorted(files, key=lambda item: item[1]))


def configure_task(output, task_id, map_payload, task):
    if task_id == "innopolis-006":
        check_instruction(output)
        return
    map_id = map_payload["map_id"]
    spec = json.dumps({"task_id": task_id, "map_id": map_id}, indent=2) + "\n"
    for relative in ("environment/world/task-spec.json", "tests/task-spec.json"):
        (output / relative).write_text(spec)
    (output / "instruction.md").write_text(render_instruction(task["prompt"]))
    config = output / "task.toml"
    text = config.read_text().replace('anonymous/innopolis-006', 'anonymous/' + task_id)
    text = text.replace('source_task_id = "innopolis-006"', f'source_task_id = "{task_id}"')
    text = text.replace('Minecraft navigation through four destinations in Innopolis',
                        'Minecraft navigation task ' + task_id)
    config.write_text(text)
    dockerfile = output / "environment/world/Dockerfile"
    asset = map_payload['source']['asset_name']
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", asset):
        raise ValueError("Map archive name is not a safe Docker build argument")
    text = dockerfile.read_text().replace('ARG NAVIGATION_MAP_ID=innopolis',
                                        'ARG NAVIGATION_MAP_ID=' + map_id)
    text = text.replace('ARG NAVIGATION_MAP_ASSET=navigation-1.21.11-innopolis.zip',
                        'ARG NAVIGATION_MAP_ASSET=' + asset)
    dockerfile.write_text(text)
    if task_id != "innopolis-006":
        # Shared-template evidence is not a completed model trial for this task.
        (output / "validation.json").write_text(json.dumps({
            "schema_version": 1, "task_id": task_id, "map_id": map_id,
            "status": "exported_not_live_validated", "shared_template": "innopolis-006",
            "shared_template_validation_sha256": sha256_file(ROOT / "eval/harbor/innopolis-006/validation.json"),
            "map_fingerprint": map_payload["world"]["expected_prepared_fingerprint"],
        }, indent=2) + "\n")
        (output / "README.md").write_text(
            f"# {task_id} — Harbor navigation task\n\n"
            f"Generated from the original catalog for `{map_id}`. Task text and evaluator settings\n"
            "are preserved; navigation guidance comes from the shared formal navigation prompt.\n\n"
            "Use `eval.harbor_agents.original:OriginalNavigationAgent` from the full anonymous\n"
            "source (or extracted source.tar.gz); the original Agent runs inside main.\n"
            "Launch with scripts/eval/run-harbor-navigation.py; it selects the navigation verifier.\n"
            "Direct Harbor commands need --verifier eval.harbor_agents.verifier:NavigationVerifier\n"
            "to report unscored infrastructure outcomes with their original terminal reason.\n"
            "See docs/harbor-pilot.md in the full source for setup and isolation details.\n"
            "Map/runtime archives are private inputs. Generic agents use the CLI in instruction.md.\n"
            "World and the separate verifier each have a baked task-spec.json; another task's\n"
            "completion cannot receive a reward. validation.json does not claim a model result.\n")
    check_instruction(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--task", default="innopolis-006", help="Task ID from the original catalog")
    parser.add_argument("--map-archive", type=Path)
    parser.add_argument("--snapshot", type=Path, help="Existing selected-map snapshot cache; reverified before export")
    parser.add_argument("--runtime", type=Path, help="Optional prepared navigation-linux-cpu/1.21.11 directory")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new directory")
    if args.map_archive and args.snapshot:
        parser.error("Choose --map-archive or --snapshot")
    map_id, selected_task = task_definition(args.task)
    map_payload = load_map(map_id)
    task = ROOT / "eval/harbor/innopolis-006"
    check_instruction(task)
    shutil.copytree(task, args.output, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.tar.gz", "*.zip"))
    configure_task(args.output, args.task, map_payload, selected_task)
    export_agent_source(args.output / "environment/agent-source.tar.gz")
    world = args.output / "environment/world"
    assets = world / "assets"
    files = []
    for relative in ("agent", "eval/navigation", "eval/harbor_agents", "scripts", "config", "configs", "containers"):
        for path in (ROOT / relative).rglob("*"):
            if "__pycache__" in path.parts or path.suffix == ".pyc" or path.name == "api_models.json":
                continue
            files.append((path, str(path.relative_to(ROOT))))
    for relative in ("pyproject.toml", "uv.lock", "README.md"):
        files.append((ROOT / relative, relative))
    archive_tree(world / "source.tar.gz", sorted(files, key=lambda row: row[1]))
    if args.map_archive:
        if sha256_file(args.map_archive) != map_payload["source"]["archive_sha256"]:
            raise ValueError("Map archive does not match the selected map's pinned SHA-256")
        shutil.copyfile(args.map_archive, assets / map_payload["source"]["asset_name"])
    if args.snapshot:
        # Rebind only the redacted repository metadata; world contents and original
        # archive identity must still exactly match the published task manifest.
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "1.21.11" / map_id
            shutil.copytree(args.snapshot, cache)
            receipt_path = cache / "source-receipt.json"
            receipt = json.loads(receipt_path.read_text())
            expected = map_payload["source"]
            if receipt["source_archive"] != {"name": expected["asset_name"],
                    "bytes": expected["archive_bytes"], "sha256": expected["archive_sha256"],
                    "world_root": expected["world_root"]}:
                raise ValueError("Snapshot archive identity does not match")
            receipt["map_source_config_digest"] = _source_config_digest(map_payload)
            receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
            previous = schema.SNAPSHOT_CACHE_ROOT
            try:
                schema.SNAPSHOT_CACHE_ROOT = Path(temporary)
                verified = verify_snapshot(map_payload)
            finally:
                schema.SNAPSHOT_CACHE_ROOT = previous
            archive_tree(assets / "snapshot.tar.gz", [
                (p, "eval/snapshots/_cache/navigation/" + str(p.relative_to(temporary)))
                for p in sorted(Path(temporary).rglob("*"))])
            print("Verified map fingerprint:", verified["fingerprint"]["value"])
    if args.runtime:
        if not (args.runtime / "runtime-receipt.json").is_file():
            raise ValueError("Runtime receipt missing")
        archive_tree(assets / "runtime.tar.gz", [
            (p, "eval/templates/_local/navigation-linux-cpu/1.21.11/" + str(p.relative_to(args.runtime)))
            for p in sorted(args.runtime.rglob("*")) if "__pycache__" not in p.parts])
    print(args.output)
    if not args.map_archive and not args.snapshot:
        print("Source task exported. Supply the pinned map archive in environment/world/assets before building.")


if __name__ == "__main__":
    main()
