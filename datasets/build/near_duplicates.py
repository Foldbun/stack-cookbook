"""Near-duplicate variant generator for the image-library search use case (05).

Each variant is derived from one source image by one or two of these transforms, with parameters drawn from a
seeded generator, and the exact parameters are returned so they can be recorded in truth.json:

  resize     downscale to 35-60 % of the side lengths
  crop       keep 80-92 % of each side at a random offset
  jpeg       re-encode at JPEG quality 25-50
  color      brightness / contrast / saturation within +-10 %, small per-channel gain
  flip       horizontal mirror
  watermark  semi-transparent text overlay near a corner

Usage as a script: python -m datasets.build.near_duplicates --help
"""
from __future__ import annotations

import argparse
import io
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

RECIPES = [
    ["resize"], ["crop"], ["jpeg"], ["color"], ["flip"], ["watermark"],
    ["resize", "jpeg"], ["crop", "color"], ["flip", "jpeg"], ["crop", "resize"],
]


def apply(im: Image.Image, op: str, rng: random.Random) -> tuple[Image.Image, dict]:
    w, h = im.size
    if op == "resize":
        s = round(rng.uniform(0.35, 0.60), 3)
        return im.resize((max(8, round(w * s)), max(8, round(h * s))), Image.LANCZOS), {"op": op, "scale": s}
    if op == "crop":
        kw, kh = round(rng.uniform(0.80, 0.92), 3), round(rng.uniform(0.80, 0.92), 3)
        cw, ch = round(w * kw), round(h * kh)
        x, y = rng.randint(0, w - cw), rng.randint(0, h - ch)
        return im.crop((x, y, x + cw, y + ch)), {"op": op, "box": [x, y, x + cw, y + ch], "keep": [kw, kh]}
    if op == "color":
        b, c, s = (round(rng.uniform(0.90, 1.10), 3) for _ in range(3))
        gains = [round(rng.uniform(0.96, 1.04), 3) for _ in range(3)]
        out = ImageEnhance.Color(ImageEnhance.Contrast(ImageEnhance.Brightness(im).enhance(b)).enhance(c)).enhance(s)
        bands = [band.point(lambda v, g=g: min(255, round(v * g))) for band, g in zip(out.split(), gains)]
        return Image.merge("RGB", bands), {"op": op, "brightness": b, "contrast": c, "saturation": s, "channel_gain": gains}
    if op == "flip":
        return im.transpose(Image.FLIP_LEFT_RIGHT), {"op": op, "axis": "horizontal"}
    if op == "watermark":
        size = max(14, round(min(w, h) * 0.08))
        font = ImageFont.load_default(size=size)
        base = im.convert("RGBA")
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        text = "SAMPLE"
        tw = d.textlength(text, font=font)
        corner = rng.choice(["bottom-right", "bottom-left", "top-right"])
        x = w - tw - size if corner.endswith("right") else size
        y = h - size * 2 if corner.startswith("bottom") else size
        d.text((x, y), text, font=font, fill=(255, 255, 255, 150))
        return Image.alpha_composite(base, layer).convert("RGB"), {"op": op, "text": text, "corner": corner, "opacity": 0.6}
    if op == "jpeg":
        return im, {"op": op}          # the quality is applied when saving, see make_variant
    raise ValueError(op)


def make_variant(src: Image.Image, recipe: list[str], rng: random.Random) -> tuple[bytes, list[dict]]:
    """Return (JPEG bytes, transforms). The JPEG quality is part of the record."""
    im, params = src.convert("RGB"), []
    quality = 88
    for op in recipe:
        im, p = apply(im, op, rng)
        if op == "jpeg":
            quality = rng.randint(25, 50)
            p["quality"] = quality
        params.append(p)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    params.append({"op": "save", "format": "jpeg", "quality": quality, "size": list(im.size)})
    return buf.getvalue(), params


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate near-duplicate variants of images (resize, crop, JPEG, "
                                 "colour shift, flip, watermark) and print the transform record as JSON.")
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()
    import json
    rng = random.Random(a.seed)
    a.out.mkdir(parents=True, exist_ok=True)
    rec = []
    for k, p in enumerate(a.images):
        data, tf = make_variant(Image.open(p), RECIPES[k % len(RECIPES)], rng)
        (a.out / f"{p.stem}.dup.jpg").write_bytes(data)
        rec.append({"source": p.name, "variant": f"{p.stem}.dup.jpg", "transforms": tf})
    print(json.dumps(rec, indent=1))


if __name__ == "__main__":
    main()
