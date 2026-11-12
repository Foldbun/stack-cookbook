"""Writes fixtures/segment-sam3.json: SYNTHETIC `/v1/segment` answers of `sam3` for the four smoke images.

Nothing here comes from the model. Each instance is an ellipse inscribed in a box taken from (or placed near) the
ground truth, with a hand-picked score so that the three routing bands, a false positive that is accepted and a miss
inside the review queue all occur. The maintainers replace the file with recorded answers
(`python -m harness refresh --uc 01 --record-fixtures`); until then the offline numbers only prove that the plumbing
works, not how good the model is.

    python tests/data/prelabelling/synthesize.py     (from the repository root; needs nothing but the standard library)
"""

import hashlib
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "clients" / "python"))
import coco_rle  # noqa: E402

SMOKE = ROOT / "datasets" / "smoke" / "01-dataset-prelabelling"
PROMPTS = ["dog", "cat", "bird", "boat"]
# image id -> [(prompt index, box [x1, y1, x2, y2] in image pixels, score)]
INSTANCES = {
    "746b596e2148b044": [(0, [446, 594, 601, 981], 0.94), (0, [487, 35, 554, 213], 0.52), (1, [452, 600, 598, 700], 0.33)],
    "7803588ebab09921": [(1, [410, 136, 740, 462], 0.91), (2, [600, 140, 730, 250], 0.62), (0, [420, 150, 700, 450], 0.38)],
    "16774edaeacc5aed": [(2, [190, 150, 880, 684], 0.57), (0, [200, 300, 500, 600], 0.36)],
    "5da2941bab9c2f4a": [(3, [199, 140, 543, 616], 0.88), (3, [320, 480, 530, 618], 0.45)],
}
PROCESSING_MS = {"746b596e2148b044": 612, "7803588ebab09921": 574, "16774edaeacc5aed": 655, "5da2941bab9c2f4a": 598}


def ellipse(width, height, box):
    """Rows (0/1) of the ellipse inscribed in `box`, and its polygon at pixel centres."""
    x1, y1, x2, y2 = box
    cx, cy, a, b = (x1 + x2 - 1) / 2, (y1 + y2 - 1) / 2, (x2 - x1) / 2, (y2 - y1) / 2
    rows = [bytearray(width) for _ in range(height)]
    for y in range(y1, y2):
        t = 1 - ((y - cy) / b) ** 2
        if t > 0:
            half = a * math.sqrt(t)
            lo, hi = max(x1, math.ceil(cx - half)), min(x2 - 1, math.floor(cx + half))
            if hi >= lo:
                rows[y][lo : hi + 1] = b"\x01" * (hi - lo + 1)
    ring = []
    for k in range(24):  # clockwise on screen: the angle grows with y pointing down
        ang = 2 * math.pi * k / 24
        ring += [min(x2 - 1, max(x1, round(cx + (a - 0.5) * math.cos(ang)))), min(y2 - 1, max(y1, round(cy + (b - 0.5) * math.sin(ang))))]
    return rows, ring


def instance(width, height, prompt, box, score):
    rows, ring = ellipse(width, height, box)
    rle = coco_rle.encode(rows)
    return {
        "prompt": prompt,
        "label": PROMPTS[prompt],
        "score": score,
        "bbox": coco_rle.bbox(rle),
        "area": coco_rle.area(rle),
        "polygon": [{"outer": ring}],
        "mask": {"encoding": "coco", "size": rle["size"], "counts": rle["counts"]},
    }


def main():
    truth = json.loads((SMOKE / "truth.json").read_text())["items"]
    entries = []
    for iid, specs in INSTANCES.items():
        data = (SMOKE / "images" / f"{iid}.jpg").read_bytes()
        w, h = truth[iid]["width"], truth[iid]["height"]
        insts = sorted((instance(w, h, p, box, s) for p, box, s in specs), key=lambda i: -i["score"])
        drop = [{"prompt": p, "lowScore": 0, "tooSmall": 0, "tooSmallArea": 0, "overlap": 0, "limit": 0, "budget": 0} for p in range(4)]
        entries.append(
            {
                "endpoint": "/v1/segment",
                "model": "sam3",
                "image": hashlib.sha256(data).hexdigest(),
                "synthetic": "made by tests/data/prelabelling/synthesize.py, not by the model",
                "timing": {"queueMs": 9, "processingMs": PROCESSING_MS[iid]},
                "response": {"instances": insts, "dropStats": drop},
            }
        )
    (HERE / "segment-sam3.json").write_text(json.dumps(entries, indent=1) + "\n")


if __name__ == "__main__":
    main()
