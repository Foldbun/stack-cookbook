"""Contact sheets for reviewing what was selected: datasets/dist/sheets/<use-case>-{smoke,full}-N.jpg.

  uv run --project datasets python -m datasets.build.sheets [01 02 03 05]

Ground truth is drawn on the thumbnails (boxes for 01, mask outline tint for 02) so mislabelled items show up."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .common import DIST, SMOKE, WORK

UCS = {"01": "01-dataset-prelabelling", "02": "02-click-to-segment", "03": "03-receipts-to-json", "04": "04-documents-to-rag-chunks", "05": "05-image-library-search", "06": "06-product-photo-pipeline"}
COLORS = {"Dog": (255, 80, 80), "Cat": (80, 200, 255), "Bird": (255, 220, 0), "Bicycle": (120, 255, 120)}


def thumb(root: Path, it: dict, truth_item: dict | None, cell: int) -> Image.Image:
    im = Image.open(root / it["image"]).convert("RGB")
    w, h = im.size
    if truth_item and "classes" in truth_item:
        d = ImageDraw.Draw(im)
        for c, v in truth_item["classes"].items():
            for b in v["boxes"]:
                d.rectangle(b["bbox"], outline=COLORS.get(c, (255, 0, 255)), width=max(3, w // 200))
    if truth_item and "mask" in truth_item:
        m = np.array(Image.open(root / truth_item["mask"]).convert("L")) > 0
        a = np.array(im).astype(float)
        a[m] = a[m] * 0.5 + np.array([255, 0, 120]) * 0.5
        im = Image.fromarray(a.astype("uint8"))
    im.thumbnail((cell, cell))
    return im


def sheet(root: Path, out_prefix: Path, max_items: int | None = None, cols: int = 6, cell: int = 240) -> list[Path]:
    man = json.loads((root / "manifest.json").read_text())
    truth = json.loads((root / "truth.json").read_text())["items"] if (root / "truth.json").exists() else {}
    items = man["items"][:max_items] if max_items else man["items"]
    per = cols * 5
    outs = []
    for page in range(0, len(items), per):
        chunk = items[page: page + per]
        rows = (len(chunk) + cols - 1) // cols
        S = Image.new("RGB", (cols * cell, rows * (cell + 16)), (30, 30, 30))
        d = ImageDraw.Draw(S)
        for k, it in enumerate(chunk):
            t = thumb(root, it, truth.get(it["id"]), cell)
            x, y = (k % cols) * cell, (k // cols) * (cell + 16)
            S.paste(t, (x + (cell - t.width) // 2, y))
            d.text((x + 3, y + cell + 2), str(it["id"])[:34], fill=(220, 220, 220))
        p = out_prefix.with_name(f"{out_prefix.name}-{page // per + 1}.jpg")
        p.parent.mkdir(parents=True, exist_ok=True)
        S.save(p, quality=80)
        outs.append(p)
    return outs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("use_cases", nargs="*", choices=list(UCS))
    a = ap.parse_args()
    for k in a.use_cases or UCS:
        uc = UCS[k]
        for kind, root in (("smoke", SMOKE / uc), ("full", WORK / uc)):
            if (root / "manifest.json").exists():
                for p in sheet(root, DIST / "sheets" / f"{uc}-{kind}"):
                    print(p)


if __name__ == "__main__":
    main()
