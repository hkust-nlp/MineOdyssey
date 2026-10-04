#!/usr/bin/env python3
"""Extract downsampled terrain and skyline views from candidate map ZIPs."""

from __future__ import annotations

import argparse
import gzip
import json
import lzma
import math
import re
import struct
import io
import zipfile
import zlib
from pathlib import Path
from typing import BinaryIO

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[2]
DOWNLOADS = REPO_ROOT / "downloads"
BOUNDS_ROOT = REPO_ROOT / "eval/results/navigation/candidate-built-area-analysis-v1"
DEFAULT_OUTPUT = REPO_ROOT / "eval/results/navigation/candidate-terrain-analysis-v1"
REGION_RE = re.compile(r"r\.(-?\d+)\.(-?\d+)\.mca$")
MISSING = -32768
MIN_Y = -64
WORLD_HEIGHT = 512


class NbtCursor:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def read(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise ValueError("truncated NBT")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def u8(self) -> int:
        return self.read(1)[0]

    def i32(self) -> int:
        return struct.unpack(">i", self.read(4))[0]

    def string(self) -> str:
        length = struct.unpack(">H", self.read(2))[0]
        return self.read(length).decode("utf-8", errors="replace")

    def skip(self, tag: int) -> None:
        fixed = {1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}
        if tag in fixed:
            self.read(fixed[tag])
        elif tag == 7:
            self.read(self.i32())
        elif tag == 8:
            self.string()
        elif tag == 9:
            subtype = self.u8()
            length = self.i32()
            for _ in range(length):
                self.skip(subtype)
        elif tag == 10:
            while True:
                subtype = self.u8()
                if subtype == 0:
                    break
                self.string()
                self.skip(subtype)
        elif tag == 11:
            self.read(self.i32() * 4)
        elif tag == 12:
            self.read(self.i32() * 8)
        else:
            raise ValueError(f"unsupported NBT tag {tag}")

    def long_array(self) -> list[int]:
        length = self.i32()
        return list(struct.unpack(f">{length}q", self.read(length * 8)))


def read_chunk_heightmap(raw: bytes) -> tuple[int, int, list[int]]:
    cursor = NbtCursor(raw)
    if cursor.u8() != 10:
        raise ValueError("NBT root is not a compound")
    cursor.string()
    chunk_x: int | None = None
    chunk_z: int | None = None
    heights: list[int] | None = None
    while True:
        tag = cursor.u8()
        if tag == 0:
            break
        name = cursor.string()
        if name == "xPos" and tag == 3:
            chunk_x = cursor.i32()
        elif name == "zPos" and tag == 3:
            chunk_z = cursor.i32()
        elif name == "Heightmaps" and tag == 10:
            found: dict[str, list[int]] = {}
            while True:
                subtype = cursor.u8()
                if subtype == 0:
                    break
                field = cursor.string()
                if subtype == 12:
                    found[field] = cursor.long_array()
                else:
                    cursor.skip(subtype)
            heights = (
                found.get("WORLD_SURFACE")
                or found.get("MOTION_BLOCKING_NO_LEAVES")
                or found.get("MOTION_BLOCKING")
            )
        else:
            cursor.skip(tag)
        if chunk_x is not None and chunk_z is not None and heights is not None:
            return chunk_x, chunk_z, heights
    raise ValueError("chunk lacks coordinates or a surface heightmap")


def decompress_chunk(region: bytes, sector_offset: int) -> bytes:
    position = sector_offset * 4096
    length = struct.unpack_from(">I", region, position)[0]
    compression = region[position + 4]
    if compression & 0x80:
        raise ValueError("external chunks are unsupported")
    payload = region[position + 5 : position + 4 + length]
    functions = {
        1: gzip.decompress,
        2: zlib.decompress,
        3: lambda value: value,
        4: lzma.decompress,
    }
    return functions[compression](payload)


def unpack_heightmap(values: list[int], preferred_bits: int) -> np.ndarray:
    # The downloaded worlds contain a mix of chunks serialized against the
    # legacy 384-block and replacement 512-block dimension heights. Mojang's
    # padded storage therefore uses either 37 longs (9 bits) or 43 longs
    # (10 bits). Infer the layout per chunk instead of trusting level.dat.
    candidates = [
        bits
        for bits in range(1, 33)
        if math.ceil(256 / (64 // bits)) == len(values)
    ]
    if not candidates:
        raise ValueError(f"unsupported heightmap storage length: {len(values)}")
    bits = preferred_bits if preferred_bits in candidates else min(candidates)
    per_long = 64 // bits
    mask = (1 << bits) - 1
    output = np.full(256, MISSING, dtype=np.int16)
    for index in range(256):
        word_index = index // per_long
        if word_index >= len(values):
            break
        shift = (index % per_long) * bits
        unsigned = int(values[word_index]) & ((1 << 64) - 1)
        output[index] = ((unsigned >> shift) & mask) + MIN_Y
    return output.reshape(16, 16)


def archive_world_root(handle: zipfile.ZipFile) -> str:
    levels = [
        name
        for name in handle.namelist()
        if name.endswith("level.dat") and "__MACOSX" not in name
    ]
    if not levels:
        raise ValueError("archive has no level.dat")
    selected = min(levels, key=lambda name: (name.count("/"), len(name)))
    return selected[: -len("level.dat")]


def archive_dimension_height(handle: zipfile.ZipFile, world_root: str) -> int:
    heights: list[int] = []
    prefix = world_root + "datapacks/"
    for name in handle.namelist():
        if not name.startswith(prefix):
            continue
        if name.endswith("data/minecraft/dimension_type/overworld.json"):
            try:
                payload = json.loads(handle.read(name).decode("utf-8"))
                heights.append(int(payload["height"]))
            except (KeyError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
                pass
        elif name.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(handle.read(name))) as nested:
                    for nested_name in nested.namelist():
                        if nested_name.endswith("data/minecraft/dimension_type/overworld.json"):
                            payload = json.loads(nested.read(nested_name).decode("utf-8"))
                            heights.append(int(payload["height"]))
            except (KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
                pass
    return max(heights, default=384)


def bounds_payload(archive: Path) -> dict[str, object]:
    normalized = archive.stem.replace(".", "-")
    path = BOUNDS_ROOT / f"{normalized}.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing built-area analysis for {archive.name}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def extract_surface(archive: Path, bounds: dict[str, int]) -> tuple[np.ndarray, dict[str, int]]:
    min_x, max_x = bounds["min_x"], bounds["max_x"]
    min_z, max_z = bounds["min_z"], bounds["max_z"]
    width, depth = max_x - min_x + 1, max_z - min_z + 1
    surface = np.full((depth, width), MISSING, dtype=np.int16)
    region_min_x, region_max_x = min_x // 512, max_x // 512
    region_min_z, region_max_z = min_z // 512, max_z // 512
    chunks_seen = 0
    chunks_failed = 0
    with zipfile.ZipFile(archive) as handle:
        root = archive_world_root(handle)
        dimension_height = archive_dimension_height(handle, root)
        preferred_bits = (dimension_height - 1).bit_length()
        names = set(handle.namelist())
        for region_z in range(region_min_z, region_max_z + 1):
            for region_x in range(region_min_x, region_max_x + 1):
                member = f"{root}region/r.{region_x}.{region_z}.mca"
                if member not in names:
                    continue
                region = handle.read(member)
                for local_z in range(32):
                    chunk_z = region_z * 32 + local_z
                    block_z = chunk_z * 16
                    if block_z > max_z or block_z + 15 < min_z:
                        continue
                    for local_x in range(32):
                        chunk_x = region_x * 32 + local_x
                        block_x = chunk_x * 16
                        if block_x > max_x or block_x + 15 < min_x:
                            continue
                        table_index = local_x + local_z * 32
                        sector = int.from_bytes(region[table_index * 4 : table_index * 4 + 3], "big")
                        if not sector:
                            continue
                        try:
                            decoded_x, decoded_z, packed = read_chunk_heightmap(
                                decompress_chunk(region, sector)
                            )
                            heights = unpack_heightmap(packed, preferred_bits)
                        except Exception:
                            chunks_failed += 1
                            continue
                        chunks_seen += 1
                        origin_x, origin_z = decoded_x * 16, decoded_z * 16
                        source_x0, source_x1 = max(min_x, origin_x), min(max_x + 1, origin_x + 16)
                        source_z0, source_z1 = max(min_z, origin_z), min(max_z + 1, origin_z + 16)
                        if source_x0 >= source_x1 or source_z0 >= source_z1:
                            continue
                        surface[
                            source_z0 - min_z : source_z1 - min_z,
                            source_x0 - min_x : source_x1 - min_x,
                        ] = heights[
                            source_z0 - origin_z : source_z1 - origin_z,
                            source_x0 - origin_x : source_x1 - origin_x,
                        ]
    return surface, {
        "chunks_seen": chunks_seen,
        "chunks_failed": chunks_failed,
        "source_dimension_height": dimension_height,
        "preferred_heightmap_bits": preferred_bits,
    }


def downsample(surface: np.ndarray, max_dimension: int) -> tuple[np.ndarray, np.ndarray, int]:
    depth, width = surface.shape
    scale = max(1, math.ceil(max(depth, width) / max_dimension))
    output_depth = math.ceil(depth / scale)
    output_width = math.ceil(width / scale)
    padded = np.full((output_depth * scale, output_width * scale), MISSING, dtype=np.int16)
    padded[:depth, :width] = surface
    blocks = padded.reshape(output_depth, scale, output_width, scale).transpose(0, 2, 1, 3)
    valid = blocks != MISSING
    low_input = np.where(valid, blocks, np.iinfo(np.int16).max)
    high_input = np.where(valid, blocks, np.iinfo(np.int16).min)
    low = low_input.min(axis=(2, 3)).astype(np.int16)
    high = high_input.max(axis=(2, 3)).astype(np.int16)
    low[~valid.any(axis=(2, 3))] = MISSING
    high[~valid.any(axis=(2, 3))] = MISSING
    return low, high, scale


def rows(array: np.ndarray) -> list[list[int | None]]:
    return [
        [None if int(value) == MISSING else int(value) for value in row]
        for row in array
    ]


def analyze(archive: Path, output: Path, max_dimension: int) -> Path:
    source = bounds_payload(archive)
    bounds = dict(source["target_cluster_bounds"])
    surface, diagnostics = extract_surface(archive, bounds)
    low, high, scale = downsample(surface, max_dimension)
    valid = surface[surface != MISSING]
    if not valid.size:
        raise RuntimeError(f"no surface data in target bounds for {archive.name}")
    payload = {
        "schema_version": 1,
        "asset": archive.name,
        "bounds": bounds,
        "source_method": "minecraft-world-surface-heightmap",
        "height_semantics": "first non-air block Y plus one; player-feet-compatible surface height",
        "min_y": int(valid.min()),
        "max_y": int(valid.max()),
        "p05_y": round(float(np.percentile(valid, 5)), 2),
        "p50_y": round(float(np.percentile(valid, 50)), 2),
        "p95_y": round(float(np.percentile(valid, 95)), 2),
        "downsample_scale_blocks": scale,
        "grid_width": int(high.shape[1]),
        "grid_depth": int(high.shape[0]),
        "low_surface_y": rows(low),
        "high_surface_y": rows(high),
        **diagnostics,
    }
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{archive.stem.replace('.', '-')}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", action="append", default=[], help="archive stem or filename; repeatable")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-dimension", type=int, default=128)
    args = parser.parse_args()
    archives = sorted(DOWNLOADS.glob("*.zip"))
    if args.map:
        requested = {
            value[:-4] if value.lower().endswith(".zip") else value
            for value in args.map
        }
        archives = [archive for archive in archives if archive.stem in requested]
        missing = requested - {archive.stem for archive in archives}
        if missing:
            parser.error(f"unknown maps: {', '.join(sorted(missing))}")
    if not archives:
        parser.error("no map archives selected")
    for index, archive in enumerate(archives, 1):
        target = analyze(archive, args.output, args.max_dimension)
        payload = json.loads(target.read_text(encoding="utf-8"))
        print(
            f"{index:02d}/{len(archives):02d} {archive.stem}: "
            f"Y {payload['min_y']}..{payload['max_y']}, "
            f"grid {payload['grid_width']}x{payload['grid_depth']}, "
            f"chunks {payload['chunks_seen']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
