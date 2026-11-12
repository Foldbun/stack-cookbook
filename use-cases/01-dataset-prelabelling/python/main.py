"""Pre-label an image dataset from text prompts with batch segmentation, route by score, export COCO and YOLO-seg.

python main.py --manifest manifest.json --out out [--concurrency N] [--limit N] [--resume]
python main.py ... --exemplar boxes.json            find objects that are hard to name from one example box
python main.py ... --boxes detections.csv --model sam-lite    turn detector boxes into masks
"""

import argparse
import csv
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import batches
import exports
import routing
import stack_client as sc

EXIT_FAILED, EXIT_NO_KEY, EXIT_TIER, EXIT_BUDGET = 1, 2, 3, 4


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--concurrency", type=int, default=2, help="batches in flight at once; accounts may have 1 to 4 open batches (default 2)")
    ap.add_argument("--limit", type=int, help="only the first N manifest items")
    ap.add_argument("--resume", action="store_true", help="skip items finished in an earlier run of the same out directory")
    ap.add_argument("--batch-size", type=int, default=100, help="images per batch request (at most 1000, default 100)")
    ap.add_argument("--accept", type=float, default=0.6, help="score from which a label is exported without review (default 0.6)")
    ap.add_argument("--review", type=float, default=0.3, help="lowest score kept; between this and --accept goes to the review queue (default 0.3)")
    ap.add_argument("--rounds", type=int, default=3, help="how often expired or failed items are sent again (default 3)")
    ap.add_argument("--retry-wait", type=float, default=5.0, help="seconds to wait before sending expired or failed items again (default 5)")
    ap.add_argument("--masks", action="store_true", help="also request pixel masks and export COCO RLE segmentations (keeps holes)")
    ap.add_argument("--exemplar", type=Path, help='JSON {"class": "Bolt", "items": {"<id>": {"positive": [[x1,y1,x2,y2]], "negative": []}}}')
    ap.add_argument("--boxes", type=Path, help="CSV with image_id,x1,y1,x2,y2[,label]: segment these boxes instead of searching by text")
    ap.add_argument("--model", choices=["sam3", "sam-lite"], help="sam3 (default) or sam-lite (boxes only; the default with --boxes)")
    cfg = ap.parse_args(argv)
    cfg.mode = "exemplar" if cfg.exemplar else "boxes" if cfg.boxes else "text"
    cfg.model = cfg.model or ("sam-lite" if cfg.mode == "boxes" else "sam3")
    if cfg.exemplar and cfg.boxes:
        ap.error("--exemplar and --boxes exclude each other")
    if cfg.model == "sam-lite" and cfg.mode != "boxes":
        ap.error("sam-lite takes boxes and points only: use it with --boxes")
    if cfg.mode == "boxes" and cfg.model != "sam-lite":
        ap.error("--boxes needs --model sam-lite")
    if not 0 <= cfg.review <= cfg.accept <= 1 or not 1 <= cfg.batch_size <= 1000 or cfg.concurrency < 1:
        ap.error("need 0 <= --review <= --accept <= 1, 1 <= --batch-size <= 1000 and --concurrency >= 1")
    return cfg


def class_names(manifest, items):
    """Class names in a stable order: the manifest's `classes`, then any prompt an item uses that is not listed."""
    by_prompt = {c["prompt"]: c["name"] for c in manifest.get("classes", [])}
    names = [c["name"] for c in manifest.get("classes", [])]
    for it in items:
        for p in it.get("prompts", []):
            if p not in by_prompt:
                by_prompt[p] = p
                names.append(p)
    return by_prompt, names


# region batch-request
def build_prompts(unit, cfg, by_prompt, extra):
    """The API prompts of one image, and the class each prompt stands for."""
    if cfg.mode == "exemplar":
        ex = extra["items"].get(unit.id)
        if not ex or not ex.get("positive"):
            return [], []
        boxes = [{"box": b, "positive": True} for b in ex["positive"]] + [{"box": b, "positive": False} for b in ex.get("negative", [])]
        return boxes, [extra["class"]] * len(boxes)
    if cfg.mode == "boxes":
        rows = extra.get(unit.id, [])
        return [{"box": r["box"]} for r in rows], [r["label"] for r in rows]
    texts = unit.item.get("prompts") or list(by_prompt)
    return [{"text": t} for t in texts], [by_prompt.get(t, t) for t in texts]


def build_body(unit, cfg):
    """One batch item: the body of POST /v1/segment without `model`. The image is an uploaded handle."""
    defaults = {"geometry": ["polygon", "mask"] if cfg.masks else ["polygon"], "polygon": {"tolerancePx": 1.0, "holes": False}}
    options = {"defaults": defaults}
    if cfg.mode != "boxes":  # filters and overlap removal belong to search prompts; box prompts are one object each
        defaults["filter"] = {"minScore": cfg.review}  # take everything from the review threshold up: low scores are routed, not lost
        options["nms"] = {"iou": 0.7, "metric": "mask", "scope": "group"}  # one object found twice under the same class
    if cfg.masks:
        options["maskSize"] = 4096  # the grid is never larger than the image, so masks come at image size
    return {"image": {"id": unit.handle}, "prompts": unit.prompts, "options": options}


# endregion


def read_extra(cfg):
    if cfg.mode == "exemplar":
        return json.loads(cfg.exemplar.read_text())
    if cfg.mode == "boxes":
        rows = {}
        with cfg.boxes.open(newline="") as fh:
            for r in csv.DictReader(fh):
                box = [int(float(r[k])) for k in ("x1", "y1", "x2", "y2")]
                rows.setdefault(r["image_id"], []).append({"box": box, "label": r.get("label") or ""})
        return rows
    return None


def item_output(unit, found, missing):
    """The `output` of result.json: what routing decided, without geometry (that goes to the export files)."""
    counts = {"accept": sum(d.route == "accept" for d in found), "review": sum(d.route == "review" for d in found)}
    instances = [{"class": d.cls, "score": round(d.score, 4), "bbox": d.bbox, "area": d.area, "route": d.route} for d in found]
    return {"image": {"width": unit.width, "height": unit.height, "sha256": unit.sha256}, "instances": instances, "counts": counts, "missing": missing}


def run(cfg):
    started, t0 = datetime.now(UTC), time.monotonic()
    manifest = json.loads(cfg.manifest.read_text())
    items = manifest["items"][: cfg.limit]
    base = cfg.manifest.parent
    by_prompt, names = class_names(manifest, items)
    extra = read_extra(cfg)
    if cfg.mode == "boxes":  # a box without a label belongs to the first class
        extra = {k: [dict(r, label=r["label"] or names[0]) for r in v] for k, v in extra.items()}
    # region client
    client = sc.StackClient(max_concurrency=cfg.concurrency)  # FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
    client.models()  # free: checks the key
    # endregion
    units = batches.scan(items, base)
    batches.upload_all(client, units, cfg.concurrency)
    results, runnable = {}, []
    for u in units:
        u.prompts, u.labels = build_prompts(u, cfg, by_prompt, extra)
        if not u.prompts:
            results[u.id] = {
                "status": "error",
                "error": batches.local_error("no_prompts", "no prompts for this image in this mode"),
                "calls": [],
                "result": None,
            }
        elif len(u.prompts) > batches.MAX_PROMPTS:
            results[u.id] = {
                "status": "error",
                "error": batches.local_error("too_many_prompts", f"{len(u.prompts)} prompts; the API takes at most 50"),
                "calls": [],
                "result": None,
            }
        else:
            u.body = build_body(u, cfg)
            u.digest = batches.request_digest(u)
            runnable.append(u)
    results.update(batches.run_all(client, runnable, cfg, cfg.out / "raw"))

    entries, records = [], []
    for u in units:
        o = results[u.id]
        entry = {"id": u.id, "status": o["status"] if o["status"] in ("ok", "expired") else "error", "calls": o["calls"], "output": None}
        if o["status"] == "ok":
            found, missing = routing.detections(u, o["result"], cfg)
            entry["output"] = item_output(u, found, missing)
            records.append({"id": u.id, "file": u.item["image"], "path": u.path, "width": u.width, "height": u.height, "detections": found, "missing": missing})
        else:
            entry["error"] = o["error"] or batches.local_error("no_result", "no result")
        entries.append(entry)

    # region export
    out = cfg.out
    out.mkdir(parents=True, exist_ok=True)
    note = f"Pre-labels from text prompts, accepted at score >= {cfg.accept}"
    exports.write_coco(out / "coco.json", records, names, "accept", note)
    exports.write_coco(out / "coco-review.json", records, names, "review", f"Detections for review, score {cfg.review} to {cfg.accept}")
    exports.write_yolo(out / "yolo", records, names)
    queued = exports.write_review(out / "review", records)
    exports.write_costs(out / "costs.csv", entries)
    # endregion

    rows = [c for e in entries for c in e["calls"]]
    fresh = [c for c in rows if not c["replayed"]]
    ledger = {"calls": len(rows), "listTotal": round(sum(c["list"] for c in fresh), 6), "chargedTotal": round(sum(c["charged"] for c in fresh), 6)}
    elapsed = time.monotonic() - t0
    result = {
        "usecase": manifest["usecase"],
        "language": "python",
        "client": f"stack_client {sc.VERSION}",
        "startedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsedMs": int(elapsed * 1000),
        "items": entries,
        "ledger": ledger,
    }
    (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    ok = sum(e["status"] == "ok" for e in entries)
    accepted = sum(r["counts"]["accept"] for r in (e["output"] for e in entries if e["output"]))
    print(f"{ok} of {len(entries)} images done in {elapsed:.1f} s ({ok / max(elapsed, 1e-9) * 60:.0f} images/min)")
    print(f"{accepted} labels accepted, {queued} rows in the review queue, list price {ledger['listTotal']:g} credits, charged {ledger['chargedTotal']:g}")
    return 0


def main(argv=None):
    cfg = parse_args(argv)
    try:
        return run(cfg)
    except sc.MissingApiKey as err:
        print(err.message, file=sys.stderr)
        return EXIT_NO_KEY
    except sc.StackError as err:
        print(f"{err.status} {err.code}: {err.message}" + (f" (request {err.request_id})" if err.request_id else ""), file=sys.stderr)
        return {"model_locked": EXIT_TIER, "budget_exceeded": EXIT_BUDGET}.get(err.code, EXIT_FAILED)


if __name__ == "__main__":
    sys.exit(main())
