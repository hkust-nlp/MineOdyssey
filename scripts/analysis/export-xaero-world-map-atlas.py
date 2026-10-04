#!/usr/bin/env python3
"""Export stitched in-game-color maps from Xaero World Map level-1 caches."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import lzma
import math
import re
import struct
import zipfile
import zlib
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[2]


REGION_SIZE = 512
TEXTURE_SIZE = 64
COLOR_BYTES = TEXTURE_SIZE * TEXTURE_SIZE * 4
HEIGHT_BYTES = 1024 * 8
SLUG_OVERRIDES = {
    "CH-FaroLaSerena.zip": "ch-farolaserena",
    "IB-AlgarveRacetrack.zip": "ib-algarveracetrack",
    "NJ-Palmersquare.zip": "nj-palmersquare",
}
COLOR_WORDS = {
    "black": (29, 29, 33), "gray": (75, 79, 82), "light_gray": (142, 142, 134),
    "white": (223, 223, 216), "red": (151, 52, 49), "orange": (216, 125, 51),
    "yellow": (229, 193, 49), "lime": (128, 199, 31), "green": (84, 109, 27),
    "cyan": (21, 137, 145), "light_blue": (58, 175, 217), "blue": (53, 57, 157),
    "purple": (126, 61, 181), "magenta": (178, 76, 216), "pink": (216, 129, 152),
    "brown": (114, 71, 40),
}
AIR_BLOCKS = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air", "minecraft:light"}


class CacheReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise ValueError(f"Xaero cache truncated at {self.offset} (need {size} bytes)")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self.take(4))[0]


class NbtCursor:
    def __init__(self, data: bytes) -> None:
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

    def long_array(self) -> list[int]:
        length = self.i32()
        return list(struct.unpack(f">{length}q", self.read(length * 8)))

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
            for _ in range(self.i32()):
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


def read_xaero_cache(path: Path) -> dict[tuple[int, int], Image.Image]:
    with zipfile.ZipFile(path) as archive:
        data = archive.read("cache.xaero")
    reader = CacheReader(data)
    full_version = reader.i32()
    major = (full_version >> 16) & 0xFFFF
    minor = full_version & 0xFFFF
    if major != 2 or minor < 19:
        raise ValueError(f"unsupported Xaero cache version {major}.{minor} in {path}")

    reader.take(20)  # map-region cache/reload/highlight/cave metadata
    while True:
        texture_coords = reader.u8()
        if texture_coords == 255:
            break
        reader.take(4)  # buffered texture version

    biome_palette_size = reader.i32()
    for _ in range(biome_palette_size):
        element_type = reader.u8()
        if element_type != 255:
            reader.take(reader.u16())  # Java DataInput UTF payload; value is not needed

    textures: dict[tuple[int, int], Image.Image] = {}
    while True:
        texture_coords = reader.u8()
        if texture_coords == 255:
            break
        texture_x = texture_coords >> 4
        texture_z = texture_coords & 15
        rgba = reader.take(COLOR_BYTES)
        # Xaero serializes its native-endian packed integer buffer. On this
        # little-endian runtime the bytes are light, red, green, blue. The first
        # channel is lighting data used by Xaero's shader, not display alpha;
        # only an all-zero pixel means that the cache has no map data.
        decoded = bytearray(COLOR_BYTES)
        for index in range(0, COLOR_BYTES, 4):
            light, red, green, blue = rgba[index : index + 4]
            decoded[index : index + 4] = bytes(
                (red, green, blue, 255 if light or red or green or blue else 0)
            )
        textures[(texture_x, texture_z)] = Image.frombytes(
            "RGBA", (TEXTURE_SIZE, TEXTURE_SIZE), bytes(decoded)
        )
        reader.take(1)  # bufferHasLight
        reader.take(HEIGHT_BYTES)
        reader.take(HEIGHT_BYTES)

        palette_size = reader.i32()
        for _ in range(palette_size):
            palette_value = reader.i32()
            if palette_value != -1:
                reader.take(2)
        if palette_size > 0 and reader.u8() == 1:
            reader.take(HEIGHT_BYTES)

    return textures


def cache_slug(asset: str) -> str:
    if asset in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[asset]
    stem = asset.removesuffix(".zip").lower()
    return re.sub(r"[^a-z0-9]+", "-", stem).strip("-")


def load_bounds(analysis_dir: Path) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for path in analysis_dir.glob("*.json"):
        payload = json.loads(path.read_text())
        result[payload["asset"]] = payload["target_cluster_bounds"]
    return result


def block_color(name: str) -> tuple[int, int, int, int]:
    short = name.removeprefix("minecraft:")
    if name in AIR_BLOCKS:
        return (0, 0, 0, 0)
    if "water" in short or short in {"ice", "frosted_ice", "blue_ice", "packed_ice"}:
        return (55, 105, 175, 220)
    if "lava" in short or "magma" in short:
        return (232, 97, 24, 255)
    for word in sorted(COLOR_WORDS, key=len, reverse=True):
        if short.startswith(word + "_"):
            return (*COLOR_WORDS[word], 255)
    if any(word in short for word in ("leaves", "grass", "moss", "vine", "azalea")):
        return (74, 123, 45, 255)
    if any(word in short for word in ("sand", "sandstone", "end_stone")):
        return (210, 194, 141, 255)
    if any(word in short for word in ("dirt", "mud", "podzol", "rooted")):
        return (120, 87, 58, 255)
    if any(word in short for word in ("log", "wood", "planks", "chest", "barrel")):
        return (139, 105, 63, 255)
    if any(word in short for word in ("brick", "terracotta", "granite")):
        return (153, 91, 68, 255)
    if any(word in short for word in ("copper", "prismarine")):
        return (70, 143, 125, 255)
    if any(word in short for word in ("quartz", "calcite", "snow", "bone")):
        return (224, 221, 207, 255)
    if any(word in short for word in ("deepslate", "blackstone", "basalt", "coal")):
        return (56, 58, 61, 255)
    if any(word in short for word in ("stone", "cobble", "andesite", "tuff", "gravel")):
        return (119, 120, 115, 255)
    if "glass" in short:
        return (177, 202, 205, 210)
    return (151, 146, 132, 255)


def unpack_palette_indices(palette_size: int, packed: list[int]) -> list[int]:
    if palette_size <= 1 or not packed:
        return [0] * 4096
    bits = max(4, math.ceil(math.log2(palette_size)))
    per_long = 64 // bits
    mask = (1 << bits) - 1
    values: list[int] = []
    for signed in packed:
        value = int(signed) & ((1 << 64) - 1)
        values.extend((value >> (index * bits)) & mask for index in range(per_long))
    return values[:4096]


def decompress_chunk(region: bytes, sector_offset: int) -> bytes:
    position = sector_offset * 4096
    length = struct.unpack_from(">I", region, position)[0]
    compression = region[position + 4]
    if compression & 0x80:
        raise ValueError("external chunk payload is not supported")
    payload = region[position + 5 : position + 4 + length]
    return {1: gzip.decompress, 2: zlib.decompress, 3: lambda value: value, 4: lzma.decompress}[compression](payload)


def read_palette(cursor: NbtCursor) -> list[str]:
    subtype = cursor.u8()
    length = cursor.i32()
    if subtype != 10:
        raise ValueError("block-state palette is not a compound list")
    names: list[str] = []
    for _ in range(length):
        block_name = "minecraft:air"
        while True:
            tag = cursor.u8()
            if tag == 0:
                break
            name = cursor.string()
            if name == "Name" and tag == 8:
                block_name = cursor.string()
            else:
                cursor.skip(tag)
        names.append(block_name)
    return names


def read_block_states(cursor: NbtCursor) -> tuple[list[str], list[int]]:
    palette: list[str] = []
    packed: list[int] = []
    while True:
        tag = cursor.u8()
        if tag == 0:
            return palette, packed
        name = cursor.string()
        if name == "palette" and tag == 9:
            palette = read_palette(cursor)
        elif name == "data" and tag == 12:
            packed = cursor.long_array()
        else:
            cursor.skip(tag)


def read_sections(cursor: NbtCursor) -> dict[int, tuple[list[str], list[int]]]:
    subtype = cursor.u8()
    length = cursor.i32()
    if subtype != 10:
        raise ValueError("sections is not a compound list")
    sections: dict[int, tuple[list[str], list[int]]] = {}
    for _ in range(length):
        section_y: int | None = None
        block_states: tuple[list[str], list[int]] | None = None
        while True:
            tag = cursor.u8()
            if tag == 0:
                break
            name = cursor.string()
            if name == "Y" and tag == 1:
                value = cursor.u8()
                section_y = value - 256 if value >= 128 else value
            elif name == "block_states" and tag == 10:
                block_states = read_block_states(cursor)
            else:
                cursor.skip(tag)
        if section_y is not None and block_states is not None:
            sections[section_y] = block_states
    return sections


def unpack_surface_heights(values: list[int]) -> list[int]:
    candidates = [bits for bits in range(1, 33) if math.ceil(256 / (64 // bits)) == len(values)]
    if not candidates:
        raise ValueError(f"unsupported heightmap storage length: {len(values)}")
    bits = 10 if 10 in candidates else min(candidates)
    per_long = 64 // bits
    mask = (1 << bits) - 1
    result = []
    for index in range(256):
        word = int(values[index // per_long]) & ((1 << 64) - 1)
        result.append(((word >> ((index % per_long) * bits)) & mask) - 64 - 1)
    return result


def palette_index_at(palette_size: int, packed: list[int], block_index: int) -> int:
    if palette_size <= 1 or not packed:
        return 0
    bits = max(4, math.ceil(math.log2(palette_size)))
    per_long = 64 // bits
    word_index = block_index // per_long
    if word_index >= len(packed):
        return 0
    word = int(packed[word_index]) & ((1 << 64) - 1)
    return (word >> ((block_index % per_long) * bits)) & ((1 << bits) - 1)


def read_chunk_surface(raw: bytes) -> tuple[int, int, list[tuple[int, str]]]:
    cursor = NbtCursor(raw)
    if cursor.u8() != 10:
        raise ValueError("NBT root is not a compound")
    cursor.string()
    chunk_x: int | None = None
    chunk_z: int | None = None
    heights_raw: list[int] | None = None
    sections: dict[int, tuple[list[str], list[int]]] = {}
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
            heights_raw = (
                found.get("WORLD_SURFACE")
                or found.get("MOTION_BLOCKING_NO_LEAVES")
                or found.get("MOTION_BLOCKING")
            )
        elif name == "sections" and tag == 9:
            sections = read_sections(cursor)
        else:
            cursor.skip(tag)
    if chunk_x is None or chunk_z is None or heights_raw is None:
        raise ValueError("chunk lacks coordinates or surface heightmap")
    surfaces: list[tuple[int, str]] = []
    for index, height in enumerate(unpack_surface_heights(heights_raw)):
        section = sections.get(height // 16)
        if section is None:
            surfaces.append((height, "minecraft:air"))
            continue
        palette, packed = section
        local_x, local_z, local_y = index & 15, index >> 4, height & 15
        block_index = local_y * 256 + local_z * 16 + local_x
        palette_index = palette_index_at(len(palette), packed, block_index)
        name = palette[palette_index] if palette_index < len(palette) else "minecraft:air"
        surfaces.append((height, name))
    return chunk_x, chunk_z, surfaces


def render_world_surface(asset: str, bounds: dict[str, int]) -> Image.Image:
    world_zip = REPO / "downloads" / asset
    min_x, max_x = bounds["min_x"], bounds["max_x"]
    min_z, max_z = bounds["min_z"], bounds["max_z"]
    width, depth = max_x - min_x + 1, max_z - min_z + 1
    image = Image.new("RGBA", (width, depth), (0, 0, 0, 0))
    pixels = image.load()
    with zipfile.ZipFile(world_zip) as archive:
        region_entries: dict[tuple[int, int], str] = {}
        for entry in archive.namelist():
            match = re.search(r"/region/r\.(-?\d+)\.(-?\d+)\.mca$", entry)
            if match:
                region_entries[(int(match.group(1)), int(match.group(2)))] = entry
        for region_x in range(min_x // 512, max_x // 512 + 1):
            for region_z in range(min_z // 512, max_z // 512 + 1):
                entry = region_entries.get((region_x, region_z))
                if not entry:
                    continue
                region = archive.read(entry)
                for local_chunk_z in range(32):
                    for local_chunk_x in range(32):
                        chunk_x = region_x * 32 + local_chunk_x
                        chunk_z = region_z * 32 + local_chunk_z
                        if chunk_x * 16 > max_x or chunk_x * 16 + 15 < min_x:
                            continue
                        if chunk_z * 16 > max_z or chunk_z * 16 + 15 < min_z:
                            continue
                        location = struct.unpack_from(">I", region, 4 * (local_chunk_x + local_chunk_z * 32))[0]
                        sector_offset = location >> 8
                        if not sector_offset:
                            continue
                        try:
                            _, _, surfaces = read_chunk_surface(
                                decompress_chunk(region, sector_offset)
                            )
                        except ValueError:
                            continue
                        for index, (height, name) in enumerate(surfaces):
                            local_x, local_z = index & 15, index >> 4
                            world_x, world_z = chunk_x * 16 + local_x, chunk_z * 16 + local_z
                            if min_x <= world_x <= max_x and min_z <= world_z <= max_z:
                                color = block_color(name)
                                shade = max(0.72, min(1.12, 0.9 + (height - 64) * 0.002))
                                pixels[world_x - min_x, world_z - min_z] = tuple(
                                    min(255, round(channel * shade)) for channel in color[:3]
                                ) + (color[3],)
    return image


def export_map(
    asset: str,
    bounds: dict[str, int],
    xaero_root: Path,
    output_dir: Path,
    thumbnail_size: int,
    thumbnail_quality: int,
) -> dict[str, object]:
    slug = cache_slug(asset)
    map_root = xaero_root / f"Multiplayer_{slug}.localhost" / "null" / "mw$default"
    min_x, max_x = bounds["min_x"], bounds["max_x"]
    min_z, max_z = bounds["min_z"], bounds["max_z"]
    width, depth = max_x - min_x + 1, max_z - min_z + 1
    cache_dir = map_root / "cache_1"
    # Xaero's cache is sparse when the review client has not displayed every
    # chunk. Start from the world's real top blocks, then overlay Xaero's exact
    # rendered pixels wherever they exist so undiscovered cache gaps stay filled.
    stitched = render_world_surface(asset, bounds)
    caches_used = 0

    candidates: dict[tuple[int, int], Path] = {}
    for path in sorted(cache_dir.glob("*.xwmc*")) if cache_dir.is_dir() else []:
        match = re.fullmatch(r"(-?\d+)_(-?\d+)\.xwmc(?:\.outdated)?", path.name)
        if not match:
            continue
        coords = (int(match.group(1)), int(match.group(2)))
        if coords not in candidates or path.suffix == ".xwmc":
            candidates[coords] = path

    for (region_x, region_z), cache_path in candidates.items():
        origin_x, origin_z = region_x * REGION_SIZE, region_z * REGION_SIZE
        if origin_x > max_x or origin_x + REGION_SIZE - 1 < min_x:
            continue
        if origin_z > max_z or origin_z + REGION_SIZE - 1 < min_z:
            continue
        try:
            textures = read_xaero_cache(cache_path)
        except Exception as error:
            raise ValueError(f"failed to parse {cache_path}: {error}") from error
        for (texture_x, texture_z), texture in textures.items():
            world_x = origin_x + texture_x * TEXTURE_SIZE
            world_z = origin_z + texture_z * TEXTURE_SIZE
            stitched.alpha_composite(texture, (world_x - min_x, world_z - min_z))
        caches_used += 1

    output_dir.mkdir(parents=True, exist_ok=True)
    full_path = output_dir / f"{slug}.png"
    stitched.save(full_path, compress_level=3)

    content_bounds = stitched.getbbox()
    thumb = stitched.crop(content_bounds) if content_bounds else stitched.copy()
    thumb.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
    thumbnail_path = output_dir / f"{slug}.webp"
    thumb.save(
        thumbnail_path,
        "WEBP",
        quality=thumbnail_quality,
        method=4,
        lossless=False,
    )
    return {
        "asset": asset,
        "slug": slug,
        "width": width,
        "depth": depth,
        "caches_used": caches_used,
        "png": str(full_path),
        "thumbnail": str(thumbnail_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--analysis-dir",
        type=Path,
        default=Path("eval/results/navigation/candidate-built-area-analysis-v1"),
    )
    parser.add_argument(
        "--xaero-root",
        type=Path,
        default=Path("eval/templates/_local/navigation-review-client/xaero/world-map"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("eval/results/navigation/candidate-xaero-color-atlas-v1"),
    )
    parser.add_argument("--thumbnail-size", type=int, default=360)
    parser.add_argument("--thumbnail-quality", type=int, default=70)
    parser.add_argument("--asset", action="append", help="export only this ZIP asset name")
    args = parser.parse_args()

    bounds_by_asset = load_bounds(args.analysis_dir)
    if args.asset:
        requested = set(args.asset)
        bounds_by_asset = {
            asset: bounds for asset, bounds in bounds_by_asset.items() if asset in requested
        }
        missing = requested.difference(bounds_by_asset)
        if missing:
            raise ValueError(f"unknown assets: {', '.join(sorted(missing))}")
    results = [
        export_map(
            asset,
            bounds,
            args.xaero_root,
            args.output_dir,
            args.thumbnail_size,
            args.thumbnail_quality,
        )
        for asset, bounds in sorted(bounds_by_asset.items())
    ]
    manifest = {"schema_version": 1, "maps": results}
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"exported {len(results)} Xaero color maps to {args.output_dir}")


if __name__ == "__main__":
    main()
