"""Use case 01 against the fake API: exports checked against their formats, resume, expired items, a cut stream.

The implementation runs as a subprocess in its own environment (as the harness runs it), so its module names
(`main`, `batches`, ...) never clash with those of other use cases.
"""

import csv
import json
import os
import subprocess
from pathlib import Path

import coco_rle
import pytest
import yaml
from conftest import ROOT, fake_log, scenario

from harness.fakeapi import FakeServer

UC = ROOT / "use-cases" / "01-dataset-prelabelling"
# Unit tests run against hand-made responses that cover all three routing bands; the use case itself ships recorded ones.
SYNTHETIC = Path(__file__).parent / "data" / "prelabelling"
MANIFEST = ROOT / "datasets" / "smoke" / "01-dataset-prelabelling" / "manifest.json"


@pytest.fixture
def api():
    with FakeServer(fixtures=[str(SYNTHETIC)], validate=True) as srv:
        yield srv
        assert fake_log(srv)["violations"] == []


def run(srv, out, *args, check=True):
    env = dict(os.environ, FOLDBUN_API_BASE=srv.url, FOLDBUN_API_KEY="fbs_fake", FOLDBUN_MAX_CREDITS="10")
    cmd = [
        "uv",
        "run",
        "--frozen",
        "--project",
        str(UC / "python"),
        "python",
        "main.py",
        "--manifest",
        str(MANIFEST),
        "--out",
        str(out),
        "--retry-wait",
        "0.1",
        *args,
    ]
    p = subprocess.run(cmd, cwd=UC / "python", env=env, capture_output=True, text=True)
    if check:
        assert p.returncode == 0, p.stderr
    return p, json.loads((out / "result.json").read_text()) if (out / "result.json").exists() else None


def test_exports_follow_their_formats(api, tmp_path):
    _, result = run(api, tmp_path)
    assert [i["status"] for i in result["items"]] == ["ok"] * 4
    coco = json.loads((tmp_path / "coco.json").read_text())
    images = {im["id"]: im for im in coco["images"]}
    cats = {c["id"]: c["name"] for c in coco["categories"]}
    assert len(images) == 4 and cats == {1: "Dog", 2: "Cat", 3: "Bird", 4: "Boat"}
    ids = [a["id"] for a in coco["annotations"]]
    assert ids == sorted(set(ids)) and len(ids) == 4  # four labels are at or above 0.6 in the fixtures
    for a in coco["annotations"]:
        im = images[a["image_id"]]
        x, y, w, h = a["bbox"]
        assert a["category_id"] in cats and a["iscrowd"] == 0 and a["area"] > 0
        assert 0 <= x and 0 <= y and x + w <= im["width"] and y + h <= im["height"]
        for ring in a["segmentation"]:
            xs, ys = ring[0::2], ring[1::2]
            assert len(ring) >= 6 and len(ring) % 2 == 0
            assert all(v % 1 == 0.5 for v in ring)  # pixel centres shifted onto the pixel grid
            assert x <= min(xs) and max(xs) <= x + w and y <= min(ys) and max(ys) <= y + h
    review = json.loads((tmp_path / "coco-review.json").read_text())
    assert len(review["annotations"]) == 6 and len(review["images"]) == 4
    # YOLO-seg: class x1 y1 ... normalised, even coordinate count of at least 6, data.yaml names match
    names = yaml.safe_load((tmp_path / "yolo" / "data.yaml").read_text())["names"]
    assert names == {0: "Dog", 1: "Cat", 2: "Bird", 3: "Boat"}
    lines = [ln.split() for f in (tmp_path / "yolo" / "labels").glob("*.txt") for ln in f.read_text().splitlines()]
    assert len(lines) == 4 and len(list((tmp_path / "yolo" / "labels").glob("*.txt"))) == 4
    for cls, *coords in lines:
        assert int(cls) in names and len(coords) >= 6 and len(coords) % 2 == 0 and all(0 <= float(v) <= 1 for v in coords)
    rows = list(csv.DictReader((tmp_path / "review" / "queue.csv").open()))
    assert len(rows) == 6 and {r["reason"] for r in rows} == {"low_score"}
    assert all((tmp_path / "review" / r["crop"]).stat().st_size > 0 for r in rows)
    costs = list(csv.DictReader((tmp_path / "costs.csv").open()))
    assert [float(r["list_credits"]) for r in costs] == [0.7] * 4 and result["ledger"] == {"calls": 4, "listTotal": 2.8, "chargedTotal": 2.8}


def test_masks_export_rle(api, tmp_path):
    run(api, tmp_path, "--masks")
    coco = json.loads((tmp_path / "coco.json").read_text())
    images = {im["id"]: im for im in coco["images"]}
    for a in coco["annotations"]:
        seg, im = a["segmentation"], images[a["image_id"]]
        assert seg["size"] == [im["height"], im["width"]] and coco_rle.area(seg) == a["area"]
        x1, y1, x2, y2 = coco_rle.bbox(seg)
        assert a["bbox"] == [x1, y1, x2 - x1, y2 - y1]


def test_resume_does_not_call_the_api_again(api, tmp_path):
    run(api, tmp_path)
    before = len([e for e in fake_log(api)["log"] if e["path"] == "/v1/batch/segment"])
    _, second = run(api, tmp_path, "--resume")
    after = len([e for e in fake_log(api)["log"] if e["path"] == "/v1/batch/segment"])
    assert before == 1 and after == 1
    assert all(c["replayed"] for i in second["items"] for c in i["calls"])
    assert second["ledger"] == {"calls": 4, "listTotal": 0, "chargedTotal": 0}


def test_expired_items_are_sent_again_and_errors_are_kept(api, tmp_path):
    scenario(api, "batch-partial")  # item 1 fails for good, items 3 and later expire
    _, result = run(api, tmp_path)
    status = {i["id"]: i["status"] for i in result["items"]}
    assert list(status.values()) == ["ok", "error", "ok", "ok"]
    failed = result["items"][1]
    assert failed["error"]["code"] == "bad_model_output" and failed["output"] is None
    batches = [e for e in fake_log(api)["log"] if e["path"] == "/v1/batch/segment"]
    assert len(batches) == 2 and batches[0]["idempotencyKey"] != batches[1]["idempotencyKey"]


def test_cut_stream_is_repeated_under_the_same_key(api, tmp_path):
    scenario(api, "batch-dropped")
    _, result = run(api, tmp_path)
    assert [i["status"] for i in result["items"]] == ["ok"] * 4
    batches = [e for e in fake_log(api)["log"] if e["path"] == "/v1/batch/segment"]
    assert len(batches) == 2 and batches[0]["idempotencyKey"] == batches[1]["idempotencyKey"]
    assert result["ledger"]["chargedTotal"] == 2.8  # the two items finished before the cut are replayed, not charged again
    assert sum(c["replayed"] for i in result["items"] for c in i["calls"]) == 2


def test_exemplar_and_boxes_modes(api, tmp_path):
    ids = [i["id"] for i in json.loads(MANIFEST.read_text())["items"]]
    ex = tmp_path / "ex.json"
    ex.write_text(json.dumps({"class": "Dog", "items": {ids[0]: {"positive": [[443, 591, 603, 984]], "negative": [[484, 32, 556, 216]]}}}))
    _, result = run(api, tmp_path / "ex", "--exemplar", str(ex))
    assert [i["status"] for i in result["items"]] == ["ok", "error", "error", "error"]  # images without an exemplar are not sent
    assert result["items"][1]["error"]["code"] == "no_prompts"
    assert result["items"][0]["calls"][0]["list"] == 0.55  # an encoding and one prompt, however many boxes
    boxes = tmp_path / "boxes.csv"
    boxes.write_text(f"image_id,x1,y1,x2,y2,label\n{ids[0]},443,591,603,984,Dog\n{ids[0]},484,32,556,216,Dog\n{ids[1]},407,131,744,465,Cat\n")
    _, result = run(api, tmp_path / "boxes", "--boxes", str(boxes), "--model", "sam-lite")
    assert [i["status"] for i in result["items"]] == ["ok", "ok", "error", "error"]
    assert result["items"][0]["calls"][0]["model"] == "sam-lite" and len(result["items"][0]["output"]["instances"]) == 2
    assert Path(tmp_path / "boxes" / "coco-review.json").exists()
