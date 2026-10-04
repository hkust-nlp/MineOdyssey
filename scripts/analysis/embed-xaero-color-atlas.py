#!/usr/bin/env python3
"""Embed exported in-game-color thumbnails into the terrain catalog fragment."""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path


DISPLAY_SLUGS = {
    "Cape Town": "afr-cape-town",
    "Hofburg": "alps-hofburg",
    "Zurich": "alps-zurich",
    "Plaza de Mayo": "arg-plaza-de-mayo",
    "Maribor Old Town": "bal-maribor-old-town",
    "Miljacka Riverside": "bk-miljacka-riverside",
    "Ohrid": "bk-ohrid",
    "Balneario": "br-balneario",
    "Copacabana Waterfront": "br-copacabana-waterfront",
    "FaroLaSerena": "ch-farolaserena",
    "Santa Lucia Hill": "ch-santa-lucia-hill",
    "Innopolis": "cis-innopolis",
    "Sviyazhsk": "cis-sviyazhsk",
    "Entrup": "ger-entrup",
    "Wurzburg": "ger-wurzburg",
    "Shun Lee": "hk-shun-lee",
    "The Chain Bridge": "hun-the-chain-bridge",
    "AlgarveRacetrack": "ib-algarveracetrack",
    "Dublin Departments District": "ire-dublin-departments-district",
    "Ueno Park": "jp-ueno-park",
    "Dubai Downtown": "me-dubai-downtown",
    "Lusail Plaza Towers": "me-lusail-plaza-towers",
    "Denmark Town": "nb-denmark-town",
    "Palmersquare": "nj-palmersquare",
    "911 memorials": "nyc-911-memorials",
    "Mr Beast 1000 harbor city": "nyc-mr-beast-1000-harbor-city",
    "Torrey Mall": "pe-torrey-mall",
    "Memorial Hall Park": "tw-memorial-hall-park",
}


def replace_once(source: str, old: str, new: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"expected one occurrence, found {count}: {old[:80]!r}")
    return source.replace(old, new, 1)


def downsample_terrain(html: str, factor: int) -> str:
    if factor <= 1:
        return html
    match = re.search(
        r'(<script type="application/json" id="mc-terrain-data">)(.*?)(</script>)',
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError("mc-terrain-data not found")
    maps = json.loads(match.group(2))
    for map_data in maps:
        for key in ("lo", "hi"):
            map_data[key] = [row[::factor] for row in map_data[key][::factor]]
        map_data["h"] = len(map_data["lo"])
        map_data["w"] = len(map_data["lo"][0])
        map_data["scale"] *= factor
    compact = json.dumps(maps, separators=(",", ":"))
    return html[: match.start()] + match.group(1) + compact + match.group(3) + html[match.end() :]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--standalone-copy", type=Path)
    parser.add_argument("--terrain-downsample", type=int, default=1)
    parser.add_argument("--omit-atlas", action="store_true")
    args = parser.parse_args()

    encoded = {}
    for display_name, slug in DISPLAY_SLUGS.items():
        payload = base64.b64encode((args.images / f"{slug}.webp").read_bytes()).decode()
        encoded[display_name] = f"data:image/webp;base64,{payload}"

    html = downsample_terrain(args.template.read_text(), args.terrain_downsample)
    html = replace_once(
        html,
        '<h3 id="mc-atlas-title">28 张地图彩色俯视总览</h3>',
        '<h3 id="mc-atlas-title">28 张地图游戏内彩色俯视总览</h3>',
    )
    html = replace_once(
        html,
        '<div class="viz-row text-small text-muted"><span>底色表示表面海拔由低到高；第二色表示同一采样格内存在明显垂直结构。</span></div>',
        '<div class="viz-row text-small text-muted"><span>这里显示地图内的实际方块与材质颜色；上方蓝橙图仍专门用于观察海拔和垂直结构。</span></div>',
    )
    html = replace_once(
        html,
        '  <div class="mc-view-grid">\n    <section>\n      <h3>表面海拔俯视</h3>',
        '  <div class="mc-view-grid">\n'
        '    <section>\n'
        '      <h3>游戏内彩色俯视</h3>\n'
        '      <div class="viz-controls">\n'
        '        <label class="form-label" for="mc-color-zoom">缩放 <span id="mc-color-zoom-value">1.0×</span>\n'
        '          <input class="form-range" id="mc-color-zoom" type="range" min="1" max="8" step="0.25" value="1">\n'
        '        </label>\n'
        '        <button type="button" class="btn btn-ghost" id="mc-color-reset">重置</button>\n'
        '      </div>\n'
        '      <div id="mc-color-viewport">\n'
        '        <img id="mc-color-selected" alt="">\n'
        '      </div>\n'
        '      <div class="text-small text-muted">滚轮或滑块缩放；放大后拖拽查看。一方块对应一个原始地图像素。</div>\n'
        '    </section>\n'
        '    <section>\n'
        '      <h3>表面海拔俯视</h3>',
    )
    color_script = (
        '  <script type="application/json" id="mc-color-data">'
        + json.dumps(encoded, separators=(",", ":"))
        + "</script>\n\n"
    )
    html = replace_once(html, "  <script>\n  (() => {", color_script + "  <script>\n  (() => {")
    html = replace_once(
        html,
        "    const maps = JSON.parse(document.getElementById('mc-terrain-data').textContent);",
        "    const maps = JSON.parse(document.getElementById('mc-terrain-data').textContent);\n"
        "    const colorMaps = JSON.parse(document.getElementById('mc-color-data').textContent);",
    )
    html = replace_once(html, "    const atlasCanvases = [];\n", "")
    html = replace_once(
        html,
        "    const camera = { yaw: -0.78, pitch: 0.72, zoom: 1, dragging: false, x: 0, y: 0 };",
        "    const camera = { yaw: -0.78, pitch: 0.72, zoom: 1, dragging: false, x: 0, y: 0 };\n"
        "    const colorView = { mapName: '', zoom: 1, x: 0, y: 0, dragging: false, pointerX: 0, pointerY: 0 };",
    )
    html = replace_once(
        html,
        "    function css(name) {",
        "    function updateColorView() {\n"
        "      colorZoom.value = String(colorView.zoom);\n"
        "      colorZoomValue.textContent = `${colorView.zoom.toFixed(2)}×`;\n"
        "      colorSelected.style.transform = `translate(calc(-50% + ${colorView.x}px), calc(-50% + ${colorView.y}px)) scale(${colorView.zoom})`;\n"
        "    }\n\n"
        "    function css(name) {",
    )
    html = replace_once(
        html,
        "    const atlasGrid = root.querySelector('#mc-atlas-grid');",
        "    const atlasGrid = root.querySelector('#mc-atlas-grid');\n"
        "    const colorSelected = root.querySelector('#mc-color-selected');\n"
        "    const colorViewport = root.querySelector('#mc-color-viewport');\n"
        "    const colorZoom = root.querySelector('#mc-color-zoom');\n"
        "    const colorZoomValue = root.querySelector('#mc-color-zoom-value');\n"
        "    const colorReset = root.querySelector('#mc-color-reset');",
    )
    html = replace_once(
        html,
        "      zValue.textContent = `${Number(zScale.value).toFixed(1)}×`;",
        "      zValue.textContent = `${Number(zScale.value).toFixed(1)}×`;\n"
        "      if (colorView.mapName !== map.name) {\n"
        "        colorView.mapName = map.name;\n"
        "        colorView.zoom = 1;\n"
        "        colorView.x = 0;\n"
        "        colorView.y = 0;\n"
        "        colorSelected.src = colorMaps[map.name];\n"
        "        colorSelected.alt = `${map.name} 游戏内方块彩色俯视图`;\n"
        "        updateColorView();\n"
        "      }",
    )
    html = replace_once(
        html,
        "\n    function drawAtlas() {\n      atlasCanvases.forEach(({ map, canvas }) => drawTopTo(map, canvas, true));\n    }\n",
        "",
    )
    html = replace_once(
        html,
        "      const canvas = document.createElement('canvas');\n"
        "      canvas.setAttribute('role', 'img');\n"
        "      canvas.setAttribute('aria-label', `${map.name} 表面海拔彩色俯视图`);\n"
        "      const caption = document.createElement('figcaption');\n"
        "      caption.textContent = map.name;\n"
        "      figure.append(canvas, caption);\n"
        "      atlasGrid.appendChild(figure);\n"
        "      atlasCanvases.push({ map, canvas });",
        "      const image = document.createElement('img');\n"
        "      image.src = colorMaps[map.name];\n"
        "      image.alt = `${map.name} 游戏内方块彩色俯视图`;\n"
        "      image.loading = 'eager';\n"
        "      const caption = document.createElement('figcaption');\n"
        "      caption.textContent = map.name;\n"
        "      figure.append(image, caption);\n"
        "      atlasGrid.appendChild(figure);",
    )
    html = html.replace("\n      drawAtlas();", "")
    html = html.replace("\n    drawAtlas();", "")
    html = replace_once(
        html,
        "    const observer = new ResizeObserver(() => {",
        "    colorZoom.addEventListener('input', () => {\n"
        "      colorView.zoom = Number(colorZoom.value);\n"
        "      if (colorView.zoom === 1) { colorView.x = 0; colorView.y = 0; }\n"
        "      updateColorView();\n"
        "    });\n"
        "    colorReset.addEventListener('click', () => {\n"
        "      colorView.zoom = 1; colorView.x = 0; colorView.y = 0; updateColorView();\n"
        "    });\n"
        "    colorViewport.addEventListener('wheel', event => {\n"
        "      event.preventDefault();\n"
        "      colorView.zoom = clamp(colorView.zoom * Math.exp(-event.deltaY * 0.0012), 1, 8);\n"
        "      updateColorView();\n"
        "    }, { passive: false });\n"
        "    colorViewport.addEventListener('pointerdown', event => {\n"
        "      if (colorView.zoom <= 1) return;\n"
        "      colorView.dragging = true; colorView.pointerX = event.clientX; colorView.pointerY = event.clientY;\n"
        "      colorViewport.setPointerCapture(event.pointerId); colorViewport.classList.add('is-dragging');\n"
        "    });\n"
        "    colorViewport.addEventListener('pointermove', event => {\n"
        "      if (!colorView.dragging) return;\n"
        "      colorView.x += event.clientX - colorView.pointerX; colorView.y += event.clientY - colorView.pointerY;\n"
        "      colorView.pointerX = event.clientX; colorView.pointerY = event.clientY; updateColorView();\n"
        "    });\n"
        "    const endColorDrag = event => {\n"
        "      colorView.dragging = false; colorViewport.classList.remove('is-dragging');\n"
        "      if (colorViewport.hasPointerCapture(event.pointerId)) colorViewport.releasePointerCapture(event.pointerId);\n"
        "    };\n"
        "    colorViewport.addEventListener('pointerup', endColorDrag);\n"
        "    colorViewport.addEventListener('pointercancel', endColorDrag);\n"
        "    const observer = new ResizeObserver(() => {",
    )
    html = replace_once(
        html,
        "    #mc-terrain-viewer .mc-atlas-item { min-width: 0; margin: 0; }",
        "    #mc-terrain-viewer .mc-atlas-item { min-width: 0; margin: 0; }\n"
        "    #mc-terrain-viewer .mc-atlas-item img { display: block; width: 100%; height: 150px; object-fit: contain; image-rendering: pixelated; background: color-mix(in srgb, var(--muted) 18%, transparent); }\n"
        "    #mc-terrain-viewer #mc-color-viewport { position: relative; width: 100%; height: 260px; overflow: hidden; cursor: zoom-in; touch-action: none; background: color-mix(in srgb, var(--muted) 18%, transparent); }\n"
        "    #mc-terrain-viewer #mc-color-viewport.is-dragging { cursor: grabbing; }\n"
        "    #mc-terrain-viewer #mc-color-selected { position: absolute; left: 50%; top: 50%; display: block; max-width: 100%; max-height: 100%; width: auto; height: auto; image-rendering: pixelated; transform-origin: center; }",
    )

    if args.omit_atlas:
        html = replace_once(
            html,
            '  <section class="mc-atlas-section" aria-labelledby="mc-atlas-title">\n'
            '    <h3 id="mc-atlas-title">28 张地图游戏内彩色俯视总览</h3>\n'
            '    <div class="mc-atlas-grid" id="mc-atlas-grid"></div>\n'
            '    <div class="viz-row text-small text-muted"><span>这里显示地图内的实际方块与材质颜色；上方蓝橙图仍专门用于观察海拔和垂直结构。</span></div>\n'
            '  </section>\n\n',
            "",
        )
        html = replace_once(html, "    const atlasGrid = root.querySelector('#mc-atlas-grid');\n", "")
        html = replace_once(
            html,
            "\n      const figure = document.createElement('figure');\n"
            "      figure.className = 'mc-atlas-item';\n"
            "      const image = document.createElement('img');\n"
            "      image.src = colorMaps[map.name];\n"
            "      image.alt = `${map.name} 游戏内方块彩色俯视图`;\n"
            "      image.loading = 'eager';\n"
            "      const caption = document.createElement('figcaption');\n"
            "      caption.textContent = map.name;\n"
            "      figure.append(image, caption);\n"
            "      atlasGrid.appendChild(figure);",
            "",
        )

    if len(html.encode()) >= 1_000_000:
        raise ValueError(f"fragment exceeds 1 MB: {len(html.encode()):,} bytes")
    args.output.write_text(html)
    if args.standalone_copy:
        args.standalone_copy.write_text(html)
    print(f"wrote {args.output} ({len(html.encode()):,} bytes)")


if __name__ == "__main__":
    main()
