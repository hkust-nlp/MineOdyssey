import gzip
import hashlib
import json
import stat
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import eval.navigation.snapshots as snapshots
from eval.navigation.snapshots import (
    SnapshotError,
    _expected_prepared_world_rows,
    fingerprint_upgraded_world,
    read_level_version,
    validate_archive,
    world_fingerprint,
)


def _nbt_name(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def _minimal_level_dat(*, data_version: int = 4671, name: str = "1.21.11") -> bytes:
    payload = bytearray()
    payload.extend(b"\x0a\x00\x00")  # unnamed root compound
    payload.extend(b"\x0a" + _nbt_name("Data"))
    payload.extend(b"\x03" + _nbt_name("DataVersion") + struct.pack(">i", data_version))
    payload.extend(b"\x0a" + _nbt_name("Version"))
    payload.extend(b"\x08" + _nbt_name("Name") + _nbt_name(name))
    payload.extend(b"\x03" + _nbt_name("Id") + struct.pack(">i", data_version))
    payload.extend(b"\x00")  # Version
    payload.extend(b"\x00")  # Data
    payload.extend(b"\x00")  # root
    return gzip.compress(bytes(payload), mtime=0)


def _minimal_level_dat_reordered() -> bytes:
    payload = bytearray(b"\x0a\x00\x00\x0a" + _nbt_name("Data"))
    payload.extend(b"\x0a" + _nbt_name("Version"))
    payload.extend(b"\x03" + _nbt_name("Id") + struct.pack(">i", 4671))
    payload.extend(b"\x08" + _nbt_name("Name") + _nbt_name("1.21.11"))
    payload.extend(b"\x00")
    payload.extend(b"\x03" + _nbt_name("DataVersion") + struct.pack(">i", 4671))
    payload.extend(b"\x00\x00")
    return gzip.compress(bytes(payload), mtime=0)


class NavigationSnapshotTest(unittest.TestCase):
    def test_modified_utf8_nbt_strings(self):
        for raw, expected in [(b'a\xc0\x80b', 'a\0b'),
                              (bytes.fromhex('eda0beedbcb4'), '\U0001fb34'),
                              ('中文'.encode(), '中文')]:
            reader = snapshots.NbtReader(struct.pack('>H', len(raw)) + raw)
            self.assertEqual(reader.string(), expected)
        for raw in [b'\xff', b'\xc0\x81', b'\xed\xa0']:
            with self.assertRaises(UnicodeDecodeError):
                snapshots.NbtReader(struct.pack('>H', len(raw)) + raw).string()

    def _validate(self, archive: Path):
        raw = archive.read_bytes()
        return validate_archive(
            archive,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            expected_bytes=len(raw),
            world_root="World",
        )

    def test_level_dat_version_readback(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "level.dat"
            path.write_bytes(_minimal_level_dat())
            self.assertEqual(
                read_level_version(path),
                {
                    "data_version": 4671,
                    "version_name": "1.21.11",
                    "version_id": 4671,
                },
            )

    def test_zip_crc_and_world_root_are_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "world.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("World/level.dat", _minimal_level_dat())
                handle.writestr("World/region/r.0.0.mca", b"region")
            selected = self._validate(archive)
            self.assertEqual(
                {row.filename for row in selected},
                {"World/level.dat", "World/region/r.0.0.mca"},
            )

    def test_zip_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("World/level.dat", _minimal_level_dat())
                handle.writestr("World/../escape", b"bad")
            with self.assertRaisesRegex(SnapshotError, "unsafe ZIP"):
                self._validate(archive)

    def test_zip_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "bad.zip"
            link = zipfile.ZipInfo("World/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("World/level.dat", _minimal_level_dat())
                handle.writestr(link, "target")
            with self.assertRaisesRegex(SnapshotError, "symlink"):
                self._validate(archive)

    def test_duplicate_zip_world_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "duplicate.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("World/level.dat", _minimal_level_dat())
                handle.writestr("World/region/r.0.0.mca", b"first")
                with self.assertWarns(UserWarning):
                    handle.writestr("World/region/r.0.0.mca", b"second")
            with self.assertRaisesRegex(SnapshotError, "duplicate world paths"):
                self._validate(archive)

    def test_world_fingerprint_binds_paths_and_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            world = Path(temporary)
            (world / "level.dat").write_bytes(_minimal_level_dat())
            (world / "region").mkdir()
            region = world / "region" / "r.0.0.mca"
            region.write_bytes(b"one")
            first = world_fingerprint(world)
            region.write_bytes(b"two")
            second = world_fingerprint(world)
            self.assertEqual(first["file_count"], 2)
            self.assertNotEqual(first["value"], second["value"])

    def test_release_snapshot_import_and_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            downloads = root / "downloads"
            cache = root / "cache" / "release-map"
            manifest_path = (
                repo
                / "eval/navigation/releases/navigation-maps-1.21.11-v1.json"
            )
            manifest_path.parent.mkdir(parents=True)
            downloads.mkdir()

            source_world = root / "source-world"
            (source_world / "region").mkdir(parents=True)
            (source_world / "level.dat").write_bytes(_minimal_level_dat())
            (source_world / "region" / "r.0.0.mca").write_bytes(b"region")
            fingerprint = snapshots.world_fingerprint(source_world)
            archive = downloads / "navigation-1.21.11-release-map.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("release-map/level.dat", _minimal_level_dat())
                handle.writestr("release-map/region/r.0.0.mca", b"region")
            archive_bytes = archive.read_bytes()
            manifest = {
                "schema_version": 1,
                "artifact_kind": "navigation-map-release-manifest",
                "repository": "anonymous/source",
                "release_tag": "navigation-maps-1.21.11-v1",
                "release_url": "https://example.invalid/release",
                "minecraft_version": "1.21.11",
                "data_version": 4671,
                "asset_count": 1,
                "assets": [
                    {
                        "map_id": "release-map",
                        "asset_name": archive.name,
                        "asset_bytes": len(archive_bytes),
                        "asset_sha256": hashlib.sha256(archive_bytes).hexdigest(),
                        "world_root": "release-map",
                        "minecraft_version": "1.21.11",
                        "data_version": 4671,
                        "world_fingerprint": fingerprint,
                    }
                ],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            map_payload = {
                "map_id": "release-map",
                "world": {
                    "minecraft_version": "1.21.11",
                    "version_name": "1.21.11",
                    "data_version": 4671,
                    "expected_source_fingerprint": "a" * 64,
                    "expected_prepared_fingerprint": "b" * 64,
                    "preparation": {
                        "id": "identity-minecraft-1.21.11-v1",
                        "reason": "test fixture",
                    },
                },
            }

            with (
                patch.object(snapshots, "REPO_ROOT", repo),
                patch.object(snapshots, "snapshot_cache_path", return_value=cache),
            ):
                imported = snapshots.import_release_snapshot(
                    map_payload,
                    manifest_path=manifest_path,
                    downloads_dir=downloads,
                )
                self.assertEqual(imported["fingerprint"], fingerprint)
                self.assertIsNone(imported["source"]["world_dir"])
                self.assertEqual(
                    imported["receipt"]["artifact_kind"],
                    "navigation-release-snapshot-receipt",
                )
                verified = snapshots.verify_snapshot(map_payload)
                self.assertEqual(verified["world_dir"], imported["world_dir"])

                region = Path(verified["world_dir"]) / "region" / "r.0.0.mca"
                region.chmod(0o644)
                region.write_bytes(b"changed")
                with self.assertRaisesRegex(SnapshotError, "content drift"):
                    snapshots.verify_snapshot(map_payload)

    def test_upgraded_fingerprint_ignores_compound_serialization_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "level.dat").write_bytes(_minimal_level_dat())
            (second / "level.dat").write_bytes(_minimal_level_dat_reordered())
            (first / "region.mca").write_bytes(b"same")
            (second / "region.mca").write_bytes(b"same")
            self.assertNotEqual(world_fingerprint(first), world_fingerprint(second))
            self.assertEqual(
                fingerprint_upgraded_world(first)["value"],
                fingerprint_upgraded_world(second)["value"],
            )

    def test_prepared_rows_replace_the_complete_locked_datapack_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replacement = root / "compatibility" / "heights"
            replacement.mkdir(parents=True)
            new_pack = b'{"pack":{"min_format":[94,1],"max_format":[94,1]}}'
            new_dimension = b'{"min_y":-64,"height":512,"logical_height":512}'
            (replacement / "pack.mcmeta").write_bytes(new_pack)
            dimension_path = (
                replacement
                / "data"
                / "minecraft"
                / "dimension_type"
                / "overworld.json"
            )
            dimension_path.parent.mkdir(parents=True)
            dimension_path.write_bytes(new_dimension)

            old_files = {
                "data/minecraft/dimension/overworld.json": b"old-dimension",
                "data/minecraft/dimension_type/overworld.json": b"old-type",
                "pack.mcmeta": b"old-pack",
            }

            def row(path: str, content: bytes) -> dict[str, object]:
                return {
                    "path": path,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }

            source_rows = [
                row("level.dat", b"level"),
                *[
                    row(f"datapacks/heights/{path}", content)
                    for path, content in old_files.items()
                ],
                row("region/r.0.0.mca", b"region"),
            ]
            source_rows.sort(key=lambda value: str(value["path"]))
            map_payload = {
                "map_id": "test-map",
                "world": {
                    "preparation": {
                        "source_datapack": {
                            "relative_path": "datapacks/heights",
                            "files": {
                                path: hashlib.sha256(content).hexdigest()
                                for path, content in old_files.items()
                            },
                        },
                        "replacement_datapack": {
                            "source_path": "compatibility/heights",
                            "install_path": "datapacks/heights",
                        },
                    }
                },
            }

            with patch.object(snapshots, "REPO_ROOT", root):
                prepared = _expected_prepared_world_rows(
                    map_payload,
                    source_rows,
                )

            paths = {str(value["path"]) for value in prepared}
            self.assertIn("level.dat", paths)
            self.assertIn("region/r.0.0.mca", paths)
            self.assertIn("datapacks/heights/pack.mcmeta", paths)
            self.assertIn(
                "datapacks/heights/data/minecraft/dimension_type/overworld.json",
                paths,
            )
            self.assertNotIn(
                "datapacks/heights/data/minecraft/dimension/overworld.json",
                paths,
            )
            self.assertEqual(len(prepared), len(source_rows) - 1)

            corrupt_rows = [dict(value) for value in source_rows]
            corrupt_rows[1]["sha256"] = "0" * 64
            with patch.object(snapshots, "REPO_ROOT", root):
                with self.assertRaisesRegex(
                    SnapshotError,
                    "differs from its lock",
                ):
                    _expected_prepared_world_rows(map_payload, corrupt_rows)


if __name__ == "__main__":
    unittest.main()
