"""Compile the approved RGBA artwork into dependency-free terminal pixels.

Run from the repository root with Python 3.10+:
    python scripts/build_makor_sprite.py

The original PNG is preserved. Only the renderer's palette/grid data is built.
"""

import hashlib
import json
import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src/apsara_cli/shared/assets"
PALETTE = {
    "O": "#181c25", "G": "#fad88f", "H": "#c98e24",
    "B": "#6096fa", "I": "#f5f1e9",
}


def rgba_pixels(data: bytes) -> tuple[int, int, list[bytearray]]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Expected a PNG")
    position, compressed = 8, bytearray()
    width = height = 0
    while position < len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        chunk = data[position + 8:position + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if (depth, color, compression, filtering, interlace) != (8, 6, 0, 0, 0):
                raise ValueError("The approved asset must be a non-interlaced 8-bit RGBA PNG")
        elif kind == b"IDAT":
            compressed.extend(chunk)
        position += length + 12
    raw = zlib.decompress(compressed)
    stride = width * 4
    if len(raw) != height * (stride + 1):
        raise ValueError("Incomplete PNG pixels")
    rows, previous = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        row = bytearray(raw[start + 1:start + stride + 1])
        for x in range(stride):
            a = row[x - 4] if x >= 4 else 0
            b = previous[x]
            c = previous[x - 4] if x >= 4 else 0
            if kind == 4:
                p = a + b - c
                distances = (abs(p - a), abs(p - b), abs(p - c))
                prediction = (a, b, c)[distances.index(min(distances))]
            elif 0 <= kind <= 3:
                prediction = (0, a, b, (a + b) // 2)[kind]
            else:
                raise ValueError("Unknown PNG filter")
            row[x] = (row[x] + prediction) & 255
        rows.append(row)
        previous = row
    return width, height, rows


def main() -> None:
    data = (ASSETS / "makor.png").read_bytes()
    width, height, rows = rgba_pixels(data)
    colors = {key: tuple(bytes.fromhex(value[1:])) for key, value in PALETTE.items()}
    grid_width = 96
    grid_height = round(height * grid_width / width)
    grid = []
    for y in range(grid_height):
        source_y = min(height - 1, int((y + .5) * height / grid_height))
        cells = []
        for x in range(grid_width):
            source_x = min(width - 1, int((x + .5) * width / grid_width))
            r, g, b, alpha = rows[source_y][source_x * 4:source_x * 4 + 4]
            if alpha < 230:  # Ignore the generated export's faint edge halo.
                cells.append(" ")
            else:
                cells.append(min(colors, key=lambda key: sum(
                    (value - target) ** 2 for value, target in zip((r, g, b), colors[key])
                )))
        grid.append("".join(cells))
    payload = {
        "source": "makor.png", "source_sha256": hashlib.sha256(data).hexdigest(),
        "width": grid_width, "height": grid_height, "palette": PALETTE, "pixels": grid,
    }
    destination = ASSETS / "makor.json"
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Built {destination.name}: {grid_width} × {grid_height} terminal pixels")


if __name__ == "__main__":
    main()
