"""Quality and cost of one run of use case 06: evaluate.py --result <result.json> --truth <truth.json>.

Needs Pillow and numpy (the harness environment has both). Prints one JSON object on the last line.

* IoU: the cut-out alpha (alpha/<id>.png next to result.json, thresholded at 128) against the ground-truth mask; mean and
  10th percentile over the photos that got a cut-out. Open Images masks are polygons, so even a perfect cut-out scores below 1.
* Review rate: the share of cut-outs the four quality rules stopped.
* Confusion matrix of "stopped by the rules" against "IoU below 0.85" (the cut-out is wrong): the rules are useful when
  most wrong cut-outs are stopped (recall) and most stopped ones are wrong (precision).
* By kind of photo: the share of the hard cases by design (cut off by the frame, several products) that the rules stopped,
  and the share of the other photos (plain and thin-structured products) they stopped.
* Cost per 1,000 photos at list price, from the ledger (1 credit per photo, no cache discount in the plan).
No timing metric is reported: network time depends on the client's path and is not published.
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

WRONG_BELOW = 0.85
ALPHA_FOREGROUND = 128
HARD_KINDS = ("truncated", "multi")  # the photos the rules are meant to stop


def iou(pred: np.ndarray, truth: np.ndarray) -> float:
    union = np.logical_or(pred, truth).sum()
    return float(np.logical_and(pred, truth).sum() / union) if union else 1.0


def percentile(values: list[float], p: float) -> float:
    v = sorted(values)
    k = (len(v) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def evaluate(result: dict, truth: dict, base: Path, truth_dir: Path) -> dict:
    ious, rows = {}, []
    for it in result["items"]:
        out = it.get("output")
        if it.get("status") != "ok" or not out:
            continue
        alpha = Image.open(base / "alpha" / f"{it['id']}.png").convert("L")
        t = truth["items"][it["id"]]
        mask = Image.open(truth_dir / t["mask"]).convert("L")
        if mask.size != alpha.size:
            alpha = alpha.resize(mask.size, Image.BILINEAR)
        ious[it["id"]] = iou(np.asarray(alpha) >= ALPHA_FOREGROUND, np.asarray(mask) >= ALPHA_FOREGROUND)
        rows.append((it["id"], not out["quality"]["passed"], ious[it["id"]] < WRONG_BELOW, t["kind"]))
    n = len(rows)
    tp = sum(1 for _, stopped, wrong, _ in rows if stopped and wrong)
    fp = sum(1 for _, stopped, wrong, _ in rows if stopped and not wrong)
    fn = sum(1 for _, stopped, wrong, _ in rows if not stopped and wrong)
    tn = n - tp - fp - fn
    by_kind = {}
    for _, stopped, wrong, kind in rows:
        k = by_kind.setdefault(kind, {"photos": 0, "stopped": 0, "wrong": 0})
        k["photos"] += 1
        k["stopped"] += stopped
        k["wrong"] += wrong
    hard = [k for kind, k in by_kind.items() if kind in HARD_KINDS]
    rest = [k for kind, k in by_kind.items() if kind not in HARD_KINDS]
    n_hard, n_rest = sum(k["photos"] for k in hard), sum(k["photos"] for k in rest)
    values = list(ious.values())
    ledger = result["ledger"]
    return {
        "photos": n,
        "iou_mean": round(sum(values) / n, 4) if n else None,
        "iou_p10": round(percentile(values, 0.10), 4) if n else None,
        "review_rate": round((tp + fp) / n, 4) if n else None,
        "wrong_cutouts": tp + fn,
        "confusion": {"stopped_wrong": tp, "stopped_fine": fp, "passed_wrong": fn, "passed_fine": tn},
        "rules_precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "rules_recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "by_kind": by_kind,
        "hard_cases_stopped_rate": round(sum(k["stopped"] for k in hard) / n_hard, 4) if n_hard else None,
        "other_photos_stopped_rate": round(sum(k["stopped"] for k in rest) / n_rest, 4) if n_rest else None,
        "cost_per_1000_images_list": round(ledger["listTotal"] / n * 1000, 3) if n else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--result", required=True)
    ap.add_argument("--truth", required=True)
    a = ap.parse_args()
    result_path, truth_path = Path(a.result), Path(a.truth)
    print(json.dumps(evaluate(json.loads(result_path.read_text()), json.loads(truth_path.read_text()), result_path.parent, truth_path.parent)))


if __name__ == "__main__":
    main()
