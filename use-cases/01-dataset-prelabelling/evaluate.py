"""Quality, review load and cost of one run of use case 01.

    python evaluate.py --result out/result.json --truth truth.json [--timing]

Prints one JSON object of flat metrics on its last line (standard library only). Language independent: it reads only
`result.json` and the ground truth. Throughput depends on the client's network path, so `images_per_minute` is printed only
with --timing and never published.

Boxes are scored at IoU 0.5 the Open Images way: a class is counted only on images where it is verified present
(`verified: 1`: its boxes are the targets) or verified absent (`verified: 0`: every detection is a false positive).
On images where the class is not verified, detections are neither right nor wrong and are left out. Detections are
matched to ground-truth boxes greedily, highest score first, each box once; a detection that matches nothing is a false
positive, a box that nothing matches is a miss.

  precision@0.5.<Class>, recall@0.5.<Class>      only the accepted detections (what ships without review)
  *_with_review@0.5.<Class>                      accepted and review detections together (what a reviewer could keep)
  review_share                                   rows of the review queue / (detections + empty-but-expected rows)
  review_true_positive_share                     review detections that match a box / review detections on verified pairs
"""

import argparse
import json


def iou(a, b):
    iw, ih = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def match(preds, boxes, threshold=0.5):
    """Greedy matching, highest score first. Returns a list of booleans (true positive?) aligned with `preds`."""
    taken, flags = set(), []
    order = sorted(range(len(preds)), key=lambda i: -preds[i]["score"])
    result = {}
    for i in order:
        best, best_iou = None, threshold
        for j, box in enumerate(boxes):
            v = iou(preds[i]["bbox"], box)
            if j not in taken and v >= best_iou:
                best, best_iou = j, v
        if best is not None:
            taken.add(best)
        result[i] = best is not None
    flags = [result[i] for i in range(len(preds))]
    return flags


def ratio(num, den):
    return round(num / den, 4) if den else None


def macro(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


ENCODE_FEE = {"sam3": 0.5, "sam-lite": 0.2}  # credits per image, from the public price list


def evaluate(result, truth, timing=False):
    classes = sorted({c for it in truth["items"].values() for c in it["classes"]})
    # per class: [accepted TP, accepted FP, GT boxes, all TP, all FP]
    tally = {c: [0, 0, 0, 0, 0] for c in classes}
    review_eval = review_tp = queue_rows = detections = images_with_queue = ok = 0
    for it in result["items"]:
        if it["status"] != "ok":
            continue
        ok += 1
        out, tr = it["output"], truth["items"].get(it["id"])
        detections += len(out["instances"])
        rows = out["counts"]["review"] + len(out["missing"])
        queue_rows += rows
        images_with_queue += rows > 0
        if tr is None:
            continue
        for cls, info in tr["classes"].items():
            if info["verified"] not in (0, 1):
                continue
            boxes = [b["bbox"] for b in info["boxes"]]
            preds = [p for p in out["instances"] if p["class"] == cls]
            accepted = [p for p in preds if p["route"] == "accept"]
            acc_flags, all_flags = match(accepted, boxes), match(preds, boxes)
            t = tally[cls]
            t[0] += sum(acc_flags)
            t[1] += len(acc_flags) - sum(acc_flags)
            t[2] += len(boxes)
            t[3] += sum(all_flags)
            t[4] += len(all_flags) - sum(all_flags)
            for p, hit in zip(preds, all_flags, strict=True):
                if p["route"] == "review":
                    review_eval += 1
                    review_tp += hit
    metrics = {}
    for cls, (tp, fp, gt, tp_all, fp_all) in tally.items():
        metrics[f"precision@0.5.{cls}"] = ratio(tp, tp + fp)
        metrics[f"recall@0.5.{cls}"] = ratio(tp, gt)
        metrics[f"precision_with_review@0.5.{cls}"] = ratio(tp_all, tp_all + fp_all)
        metrics[f"recall_with_review@0.5.{cls}"] = ratio(tp_all, gt)
    for name in ("precision", "recall", "precision_with_review", "recall_with_review"):
        metrics[f"{name}@0.5.macro"] = macro([metrics[f"{name}@0.5.{c}"] for c in classes])
    metrics["review_share"] = ratio(queue_rows, detections + sum(len(i["output"]["missing"]) for i in result["items"] if i["status"] == "ok"))
    metrics["review_images_share"] = ratio(images_with_queue, ok)
    metrics["review_true_positive_share"] = ratio(review_tp, review_eval)
    metrics["review_detections_evaluated"] = review_eval
    metrics["images"] = len(result["items"])
    metrics["images_ok"] = ok
    if timing:
        minutes = result["elapsedMs"] / 60000
        metrics["images_per_minute"] = round(ok / minutes, 1) if minutes else None
    # The planning price counts the image encoding fee on every call. A call that hit the encoding cache was priced
    # without it (the fee is waived then), and a cache hit is not promised, so the fee is added back here.
    calls = [c for item in result["items"] for c in item.get("calls", [])]
    planning = sum(c["list"] + (ENCODE_FEE.get(c["model"], 0) if c.get("cache") == "hit" else 0) for c in calls)
    n = len(result["items"])
    metrics["cost_per_1000_images"] = round(planning / n * 1000, 2) if n else None
    metrics["cost_per_1000_images_this_run"] = round(sum(c["list"] for c in calls) / n * 1000, 2) if n else None
    metrics["encoding_cache_hit_share"] = round(sum(c.get("cache") == "hit" for c in calls) / len(calls), 4) if calls else None
    return metrics


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--timing", action="store_true", help="also print images_per_minute (depends on the network path)")
    args = ap.parse_args()
    with open(args.result, encoding="utf-8") as f:
        result = json.load(f)
    with open(args.truth, encoding="utf-8") as f:
        truth = json.load(f)
    print(json.dumps(evaluate(result, truth, args.timing)))


if __name__ == "__main__":
    main()
