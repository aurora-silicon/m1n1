#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate built-in Aurora logo assets from the supplied master mark."""

from pathlib import Path
import argparse
import math

from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()

    source = Image.open(args.source).convert("RGBA")
    mask = source.getchannel("A").point(lambda alpha: 255 if alpha > 8 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        raise ValueError("source logo has no visible pixels")
    side = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    side += 2 * math.ceil(side * 0.08)
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    bounds = (round(cx - side / 2), round(cy - side / 2),
              round(cx + side / 2), round(cy + side / 2))
    crop = source.crop(bounds)
    black = Image.new("RGBA", crop.size, (0, 0, 0, 255))
    black.alpha_composite(crop)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for size in (128, 256):
        scaled = black.convert("RGB").resize((size, size), Image.Resampling.BOX)
        scaled.save(args.output_dir / f"aurora_{size}.png")
        rgba = scaled.convert("RGBA")
        (args.output_dir / f"aurora_{size}.bin").write_bytes(rgba.tobytes())


if __name__ == "__main__":
    main()
