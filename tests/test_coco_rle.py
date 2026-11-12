"""The COCO RLE codec against vectors made with pycocotools (not a dependency: the vectors are recorded in tests/data)."""

import json
import random
from pathlib import Path

import coco_rle as rle
import pytest

VECTORS = json.loads((Path(__file__).parent / "data" / "coco_rle_vectors.json").read_text())
WITH_ROWS = [k for k, v in VECTORS.items() if "rows" in v]


def rows_of(vec):
    return [bytearray(int(c) for c in line) for line in vec["rows"]]


@pytest.mark.parametrize("name", WITH_ROWS)
def test_encode_matches_pycocotools(name):
    vec = VECTORS[name]
    assert rle.encode(rows_of(vec)) == {"size": vec["size"], "counts": vec["counts"]}


@pytest.mark.parametrize("name", WITH_ROWS)
def test_decode_matches_pycocotools(name):
    vec = VECTORS[name]
    assert rle.decode({"size": vec["size"], "counts": vec["counts"]}) == rows_of(vec)


@pytest.mark.parametrize("name", list(VECTORS))
def test_area_and_bbox(name):
    vec = VECTORS[name]
    assert rle.area(vec) == vec["area"]
    assert rle.bbox(vec) == vec["bbox"]


def test_large_mask_with_a_hole_round_trips():
    vec = VECTORS["big_with_hole"]
    rows = [bytearray(300) for _ in range(200)]
    for y in range(10, 190):
        rows[y][5:290] = b"\x01" * 285
    for y in range(50, 100):
        rows[y][60:200] = bytes(140)
    assert rle.decode(vec) == rows
    assert rle.encode(rows)["counts"] == vec["counts"]


def test_runs_with_negative_deltas_round_trip():
    for runs in ([0, 5, 3, 1, 9, 2, 100, 4000], [7, 70000, 1, 1, 1, 1, 1, 90000, 3], [0]):
        assert rle.counts_to_runs(rle.runs_to_counts(runs)) == runs


def test_random_masks_round_trip_and_bbox():
    rng = random.Random(1)
    for _ in range(25):
        h, w = rng.randint(1, 30), rng.randint(1, 30)
        density = rng.choice((0.05, 0.5, 0.95))
        rows = [bytearray(1 if rng.random() < density else 0 for _ in range(w)) for _ in range(h)]
        enc = rle.encode(rows)
        assert rle.decode(enc) == rows
        assert rle.area(enc) == sum(map(sum, rows))
        ys = [y for y in range(h) if any(rows[y])]
        xs = [x for x in range(w) if any(r[x] for r in rows)]
        assert rle.bbox(enc) == ([min(xs), min(ys), max(xs) + 1, max(ys) + 1] if ys else None)


def test_resize_scales_a_block():
    small = rle.encode([bytearray([0, 1]), bytearray([0, 1])])  # right half of a 2x2 grid
    big = rle.decode(rle.resize(small, 4, 4))
    assert [list(r) for r in big] == [[0, 0, 1, 1]] * 4


def test_decode_rejects_counts_that_do_not_fit():
    with pytest.raises(ValueError):
        rle.decode({"size": [2, 2], "counts": VECTORS["empty"]["counts"]})
