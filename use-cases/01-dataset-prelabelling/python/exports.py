"""Files that other tools read: COCO JSON, YOLO-seg text files, a review queue (CSV plus crops) and `costs.csv`.

Coordinates. The API gives polygon vertices as pixel *centres* (`0 .. width - 1`) and boxes as pixel *edges* with an
exclusive far side. COCO and YOLO both use the pixel grid, where the left edge of pixel 0 is 0.0, so every polygon
vertex is shifted by +0.5. Boxes need no shift. COCO polygons have no holes, so only outer rings are exported; with
`--masks` the segmentation is a COCO RLE, which keeps holes.
"""

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import coco_rle
from PIL import Image, ImageOps


def grid_ring(ring):
    """A flat ring [x0, y0, x1, y1, ...] from pixel centres to pixel-grid coordinates."""
    return [v + 0.5 for v in ring]


# region coco-export
def coco_annotation(ann_id, image_id, cat_id, det, width, height):
    x1, y1, x2, y2 = det.bbox
    if det.mask:  # --masks: a COCO RLE at the size of the image (the mask grid can be smaller)
        rle = det.mask if det.mask["size"] == [height, width] else coco_rle.resize(det.mask, height, width)
        segmentation, area = {"size": rle["size"], "counts": rle["counts"]}, coco_rle.area(rle)
    else:
        segmentation, area = [grid_ring(r) for r in det.polygon], det.area
    return {
        "id": ann_id,
        "image_id": image_id,
        "category_id": cat_id,
        "bbox": [x1, y1, x2 - x1, y2 - y1],  # [x, y, width, height]
        "area": area,
        "segmentation": segmentation,
        "iscrowd": 0,
        "score": round(det.score, 4),
    }


def write_coco(path, records, classes, band, description):
    """COCO instances file of the detections routed to `band`. Every image is listed, also those without annotations."""
    cats = {name: i + 1 for i, name in enumerate(classes)}
    doc = {
        "info": {"description": description, "version": "1.0", "date_created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
        "licenses": [],
        "images": [],
        "categories": [{"id": i, "name": name} for name, i in cats.items()],
        "annotations": [],
    }
    for n, rec in enumerate(records, 1):
        doc["images"].append({"id": n, "file_name": rec["file"], "width": rec["width"], "height": rec["height"]})
        for det in rec["detections"]:
            if det.route == band and (det.polygon or det.mask):
                doc["annotations"].append(coco_annotation(len(doc["annotations"]) + 1, n, cats[det.cls], det, rec["width"], rec["height"]))
    path.write_text(json.dumps(doc, indent=1) + "\n")


# endregion


# region yolo-export
def write_yolo(root, records, classes):
    """YOLO segmentation labels of the accepted detections: `labels/<image>.txt`, one line per polygon part:
    `class x1 y1 x2 y2 ...` normalised to 0..1, and `data.yaml`. An image without detections gets an empty file."""
    labels = root / "labels"
    labels.mkdir(parents=True, exist_ok=True)
    for rec in records:
        lines = []
        for det in rec["detections"]:
            if det.route != "accept":
                continue
            for ring in det.polygon:
                pts = grid_ring(ring)
                norm = [min(1.0, max(0.0, v / (rec["width"] if i % 2 == 0 else rec["height"]))) for i, v in enumerate(pts)]
                lines.append(f"{classes.index(det.cls)} " + " ".join(f"{v:.6f}" for v in norm))
        (labels / (Path(rec["file"]).stem + ".txt")).write_text("\n".join(lines) + ("\n" if lines else ""))
    names = "\n".join(f"  {i}: {json.dumps(name)}" for i, name in enumerate(classes))
    (root / "data.yaml").write_text(
        f"# Put or link the images in yolo/images; labels/ holds the matching .txt files.\npath: .\ntrain: images\nval: images\nnames:\n{names}\n"
    )


# endregion

QUEUE_FIELDS = ["queue_id", "image_id", "image_file", "class", "reason", "score", "x1", "y1", "x2", "y2", "crop"]


def save_crop(src, box, dest, pad=0.1, longest=384):
    """A JPEG crop of `box` with some context (`pad` of its longer side); no box means the whole image."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if box:
            m = int(pad * max(box[2] - box[0], box[3] - box[1]))
            im = im.crop((max(0, box[0] - m), max(0, box[1] - m), min(im.width, box[2] + m), min(im.height, box[3] + m)))
        im.thumbnail((longest, longest))
        im.save(dest, "JPEG", quality=85)


# region review-queue
def write_review(root, records):
    """`queue.csv` plus one crop per row. Rows: detections between the review and accept thresholds (`low_score`), and
    expected classes with no detection (`empty_but_expected`, the crop is the whole image). Returns the number of rows."""
    (root / "crops").mkdir(parents=True, exist_ok=True)
    rows = []
    for rec in records:
        n = 0
        for det in rec["detections"]:
            if det.route == "review":
                n += 1
                rows.append((rec, f"{rec['id']}-{n:02d}", det.cls, "low_score", round(det.score, 4), det.bbox))
        for cls in rec["missing"]:
            n += 1
            rows.append((rec, f"{rec['id']}-{n:02d}", cls, "empty_but_expected", "", None))
    with (root / "queue.csv").open("w", newline="") as fh:
        out = csv.writer(fh)
        out.writerow(QUEUE_FIELDS)
        for rec, qid, cls, reason, score, box in rows:
            save_crop(rec["path"], box, root / "crops" / f"{qid}.jpg")
            out.writerow([qid, rec["id"], rec["file"], cls, reason, score, *(box or ["", "", "", ""]), f"crops/{qid}.jpg"])
    return len(rows)


# endregion

COST_FIELDS = ["image_id", "status", "list_credits", "charged_credits", "replayed", "cache", "queue_ms", "processing_ms", "instances", "accepted", "review"]


# region costs
def write_costs(path, items):
    """One row per image: what the call cost at list price and what was charged (0 when replayed), timing and counts."""
    with path.open("w", newline="") as fh:
        out = csv.writer(fh)
        out.writerow(COST_FIELDS)
        for it in items:
            calls = it["calls"]
            out_ = it["output"] or {}
            counts = out_.get("counts", {})
            out.writerow(
                [
                    it["id"],
                    it["status"],
                    round(sum(c["list"] for c in calls if not c["replayed"]), 6),
                    round(sum(c["charged"] for c in calls), 6),
                    any(c["replayed"] for c in calls),
                    ",".join(sorted({c["cache"] for c in calls if c["cache"]})),
                    sum(c["queueMs"] for c in calls),
                    sum(c["processingMs"] for c in calls),
                    len(out_.get("instances", [])),
                    counts.get("accept", 0),
                    counts.get("review", 0),
                ]
            )


# endregion
