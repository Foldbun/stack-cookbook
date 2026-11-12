"""Open Images V7 (validation split) metadata: pinned download, person-free eligibility filter, class tables."""
from __future__ import annotations

import collections
import csv
import pickle
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import oi_exclusions
from .common import CACHE, fetch

GCS = "https://storage.googleapis.com/openimages"
META_URLS = {
    "classes": f"{GCS}/v7/oidv7-class-descriptions.csv",
    "boxable": f"{GCS}/v7/oidv7-class-descriptions-boxable.csv",
    "bbox": f"{GCS}/v5/validation-annotations-bbox.csv",
    "human": f"{GCS}/v7/oidv7-val-annotations-human-imagelabels.csv",
    "machine": f"{GCS}/v7/oidv7-val-annotations-machine-imagelabels.csv",
    "images": f"{GCS}/2018_04/validation/validation-images-with-rotation.csv",
    "segmentation": f"{GCS}/v5/validation-annotations-object-segmentation.csv",
}
MASK_ZIP_URL = GCS + "/v5/validation-masks/validation-masks-{c}.zip"
IMAGE_URL = "https://open-images-dataset.s3.amazonaws.com/validation/{id}.jpg"
OI_DIR = CACHE / "openimages"
LICENCE = "CC BY 2.0"
LICENCE_URL = "https://creativecommons.org/licenses/by/2.0/"
csv.field_size_limit(10**8)


def meta_path(key: str) -> Path:
    return OI_DIR / "meta" / META_URLS[key].rsplit("/", 1)[1]


def download_meta(keys=None) -> None:
    for k in keys or META_URLS:
        fetch(META_URLS[k], meta_path(k))


@dataclass
class Pool:
    cls: dict                 # mid -> display name (all 20k classes)
    name2mid: dict
    boxable: dict             # mid -> name
    imgs: dict                # eligible ImageID -> metadata row
    boxes: dict               # ImageID -> [bbox rows] (non-excluded classes only)
    human: dict               # ImageID -> {mid: 0/1} (human verified image-level labels, non-excluded)
    funnel: dict = field(default_factory=dict)

    def mid(self, name: str) -> str:
        return self.name2mid[name]


def _rows(key):
    with open(meta_path(key), newline="") as f:
        yield from csv.DictReader(f)


def build_pool() -> Pool:
    """Eligible = validation image, CC BY 2.0 with an author, rotation 0, and none of: an excluded-class box
    (includes Person, Human face, ..., Vehicle registration plate, Flag, Car ...), a human-verified positive
    image-level label of an excluded class, a machine label of an excluded class with confidence >= 0.2,
    or a depiction box (drawings, toys, posters)."""
    cache = CACHE / "oi_pool.pkl"
    if cache.exists():
        return pickle.load(open(cache, "rb"))
    download_meta()
    cls, boxable = {}, {}
    for key, d in (("classes", cls), ("boxable", boxable)):
        with open(meta_path(key), newline="") as f:
            for r in csv.reader(f):
                if r and r[0].startswith("/"):
                    d[r[0]] = r[1]
    name2mid = {v: k for k, v in cls.items()}
    excl = {name2mid[n] for n in oi_exclusions.EXPLICIT if n in name2mid}
    for n in oi_exclusions.EXPLICIT:
        if n not in name2mid:
            print("warning: exclusion name not in class table:", n, file=sys.stderr)
    excl |= {m for m, n in cls.items() if oi_exclusions.KEYWORDS.search(n)}

    fun = collections.OrderedDict()
    imgs = {}
    for r in _rows("images"):
        imgs[r["ImageID"]] = r
    fun["all_validation"] = len(imgs)
    ok = {k: r for k, r in imgs.items() if r["License"].rstrip("/").endswith("licenses/by/2.0")}
    fun["licence_cc_by_2.0"] = len(ok)
    ok = {k: r for k, r in ok.items() if r["Author"].strip()}
    fun["author_present"] = len(ok)
    ok = {k: r for k, r in ok.items() if r["Rotation"].strip() in ("", "0", "0.0")}
    fun["rotation_0"] = len(ok)

    bad = collections.defaultdict(set)
    boxes = collections.defaultdict(list)
    for r in _rows("bbox"):
        i = r["ImageID"]
        if i not in ok:
            continue
        if r["LabelName"] in excl:
            bad[i].add("bbox")
        elif r["IsDepiction"] == "1":
            bad[i].add("depiction")
        else:
            boxes[i].append(r)
    fun["after_bbox_exclusion"] = len(set(ok) - set(bad))
    human = collections.defaultdict(dict)
    for r in _rows("human"):
        i = r["ImageID"]
        if i not in ok:
            continue
        if r["Confidence"] == "1" and r["LabelName"] in excl:
            bad[i].add("label")
        if r["LabelName"] not in excl:
            human[i][r["LabelName"]] = int(float(r["Confidence"]))
    fun["after_human_label_exclusion"] = len(set(ok) - set(bad))
    for r in _rows("machine"):
        i = r["ImageID"]
        if i in ok and r["LabelName"] in excl and float(r["Confidence"]) >= 0.2:
            bad[i].add("machine")
    keep = {i: ok[i] for i in ok if i not in bad}
    fun["after_machine_label_exclusion"] = len(keep)
    pool = Pool(cls, name2mid, boxable, keep, {i: boxes[i] for i in keep}, {i: human[i] for i in keep}, dict(fun))
    pool.excluded_mids = excl
    pickle.dump(pool, open(cache, "wb"))
    return pool


def attribution_row(img: dict, file: str, modified: str, notes: str = "") -> dict:
    return {"file": file, "id": img["ImageID"], "title": img["Title"], "author": img["Author"],
            "author_url": img["AuthorProfileURL"], "source_page_url": img["OriginalLandingURL"],
            "licence": LICENCE, "licence_url": LICENCE_URL, "original_url": IMAGE_URL.format(id=img["ImageID"]),
            "modified": modified, "notes": notes}
