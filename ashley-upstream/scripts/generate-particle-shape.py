from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "src" / "renderer" / "particle-shape.ts",
    )
    parser.add_argument("--count", type=int, default=6000)
    args = parser.parse_args()

    image = Image.open(args.source).convert("RGB")
    image = ImageOps.fit(image, (128, 128), method=Image.Resampling.LANCZOS)
    gray = ImageOps.grayscale(image)
    edge = gray.filter(ImageFilter.FIND_EDGES)
    gray_data = np.asarray(gray, dtype=np.float32)
    edge_data = np.asarray(edge, dtype=np.float32)
    color_data = np.asarray(image, dtype=np.uint8)
    brightness = np.clip(gray_data / 255.0, 0.0, 1.0)
    weights = brightness**1.15
    weights += np.clip(edge_data / 255.0, 0.0, 1.0) * 0.85

    yy, xx = np.mgrid[0:128, 0:128]
    nx = (xx - 63.5) / 63.5
    ny = (yy - 63.5) / 63.5
    radius = np.sqrt(nx * nx + ny * ny)
    weights *= np.clip(1.05 - radius, 0.0, 1.0)
    weights *= (brightness > 0.08).astype(np.float32)
    weights = np.maximum(weights, 0.0)
    flat_weights = weights.reshape(-1).astype(np.float64)
    flat_weights /= max(float(flat_weights.sum()), 1e-9)

    rng = random.Random(20260912)
    cell_size = 3
    selected: dict[tuple[int, int], tuple[float, int, int]] = {}
    for index, score in enumerate(flat_weights):
        if score <= 0:
            continue
        row, column = divmod(index, 128)
        key = (row // cell_size, column // cell_size)
        current = selected.get(key)
        if current is None or score > current[0]:
            selected[key] = (float(score), row, column)
    candidates = sorted(
        selected.values(),
        key=lambda item: item[0],
        reverse=True,
    )
    if args.count > 0:
        candidates = candidates[: args.count]

    points: list[tuple[float, float, float, int, str, int]] = []

    for _score, row, column in candidates:
        jitter_x = (rng.random() - 0.5) * 0.11
        jitter_y = (rng.random() - 0.5) * 0.11
        x = nx[row, column] + jitter_x
        y = -ny[row, column] + jitter_y
        radial = max(0.0, 1.0 - x * x - y * y)
        z = math.sqrt(radial) * (0.38 if (column + row) % 2 else -0.38)
        intensity = int(np.clip(gray_data[row, column], 8, 255))
        r, g, b = (int(value) for value in color_data[row, column])
        size = 2.2 + intensity / 70
        points.append(
            (
                round(x * 176, 2),
                round(y * 176, 2),
                round(z * 152, 2),
                round(size, 2),
                f"rgb({r},{g},{b})",
                intensity,
            )
        )

    lines = [
        "// Generated from the reference image by scripts/generate-particle-shape.py.",
        "export const referenceParticleShape = [",
    ]
    for x, y, z, size, color, intensity in points:
        lines.append(f"  [{x}, {y}, {z}, {size}, '{color}', {intensity}],")
    lines.extend(["] as const;", ""])
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(f"particles={len(points)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
