import json

import embedder
import numpy as np
import search
import stack_client as sc
from conftest import api_calls
from index import LibraryIndex, scan_folder

from harness.fakeapi import tiny_png

PUBLIC, FUNDED = "fbs_fake_public", "fbs_fake"


def build(server, folder, index_dir, key=PUBLIC, **kw):
    client = sc.StackClient(key, server.url, throttle=False)
    index = LibraryIndex.load(index_dir)
    index.set_items(scan_folder(folder, index.items))
    index.commit()
    report = embedder.index_library(client, index, folder, log=lambda *_: None, **kw)
    return client, index, report


def test_index_then_reindex_sends_nothing(server, make_library, tmp_path):
    folder = make_library(12)
    _, index, report = build(server, folder, tmp_path / "idx")
    assert report.embedded == 24 and not report.stopped and not report.failed
    assert index.dimensions("dinov3") == 16  # read from the response, never assumed
    before = len(api_calls(server))
    _, index, report = build(server, folder, tmp_path / "idx")
    assert report.embedded == 0 and len(api_calls(server)) == before


def test_only_new_and_changed_files_are_embedded(server, make_library, tmp_path):
    folder = make_library(10)
    build(server, folder, tmp_path / "idx")
    make_library(2, start=10)  # two new files
    changed = folder / "img003.png"
    changed.write_bytes(tiny_png(99, 8))  # same path, new content
    (folder / "img000.png").rename(folder / "renamed.png")  # same content, new path
    (folder / "img001.png").unlink()  # gone
    _, index, report = build(server, folder, tmp_path / "idx")
    assert report.embedded == 6  # 3 new contents (2 new files, 1 changed) x 2 models; the rename costs nothing
    assert len(index.items) == 11 and len(index.models["dinov3"]["sha256s"]) == 11  # the removed file's row is gone
    again = LibraryIndex.load(tmp_path / "idx")
    assert [it["id"] for it in again.items] == [it["id"] for it in index.items]
    files = sorted(p.name for p in (tmp_path / "idx").iterdir())
    assert len([f for f in files if f.startswith("vectors-dinov3")]) == 1 and "index.json.tmp" not in files


def test_public_allowance_stops_cleanly_and_resumes(server, make_library, tmp_path):
    folder = make_library(70)  # 140 vectors needed, 100 calls included per UTC day
    _, index, report = build(server, folder, tmp_path / "idx", batch_size=10)
    assert report.stopped and report.stopped["code"] == "daily_limit_reached"
    assert report.embedded == 100 and not report.failed
    saved = LibraryIndex.load(tmp_path / "idx")  # everything that finished was committed
    assert sum(len(m["sha256s"]) for m in saved.models.values()) == 100
    assert len(saved.models["dinov3"]["sha256s"]) == len(saved.models["siglip2"]["sha256s"]) == 50  # chunks alternate the models
    # "Next day": a fresh server has a fresh allowance. Only the 40 missing vectors are embedded.
    with type(server)(validate=True) as tomorrow:
        _, index, report = build(tomorrow, folder, tmp_path / "idx", batch_size=10)
        assert report.embedded == 40 and not report.stopped
        assert len(api_calls(tomorrow)) == 4  # the 20 images left, in chunks of 10, with 2 models
    assert all(len(m["sha256s"]) == 70 for m in index.models.values())


def test_free_only_stops_a_funded_account_at_the_allowance(server, make_library, tmp_path):
    folder = make_library(8)
    _, _, report = build(server, folder, tmp_path / "idx", key=FUNDED, free_only=True, keep_free=0)
    assert not report.stopped  # 16 calls are far inside the 1,000 included ones
    _, _, report = build(server, folder, tmp_path / "idx2", key=PUBLIC, keep_free=90)  # leave 90 calls for queries
    assert report.stopped and report.embedded == 10


def test_partial_batch_is_retried(server, make_library, tmp_path):
    folder = make_library(6)
    server.fake.reset({"rules": [{"path": "/v1/batch/*", "times": 1, "itemErrors": {"1": "bad_model_output"}, "expireFrom": 3}]})
    _, index, report = build(server, folder, tmp_path / "idx", batch_size=6)
    # item 1 failed for good, items 3 to 5 expired (not billed) and went into the next attempt
    assert len(report.failed) == 1 and next(iter(report.failed.values()))["code"] == "bad_model_output"
    assert len(index.models["dinov3"]["sha256s"]) == 5


def test_stream_that_drops_is_resumed_without_charging_twice(server, make_library, tmp_path):
    folder = make_library(6)
    server.fake.reset({"rules": [{"path": "/v1/batch/*", "times": 1, "disconnectAfterItems": 2}]})
    client, index, report = build(server, folder, tmp_path / "idx", batch_size=6)
    assert not report.failed and len(index.models["dinov3"]["sha256s"]) == 6
    assert client.summary()["listTotal"] == 0.6  # 12 images at 0.05, each billed once


def test_top_k_and_duplicate_groups():
    rng = np.random.default_rng(1)
    base = rng.normal(size=(6, 32)).astype(np.float32)
    base[4] = base[1] + 0.01 * rng.normal(size=32)  # row 4 is a near-copy of row 1
    base[5] = base[4] + 0.01 * rng.normal(size=32)  # and row 5 of row 4: a chain
    m = base / np.linalg.norm(base, axis=1, keepdims=True)
    assert [r for r, _ in search.top_k(m, m[1], 2, exclude=1)] == [4, 5]
    assert search.top_k(m, m[1], 99, exclude=1).__len__() == 5
    assert search.duplicate_groups(m, 0.95) == [[1, 4, 5]]
    assert search.duplicate_groups(m, 0.9999999) == []


def test_index_file_is_row_major_float32(server, make_library, tmp_path):
    folder = make_library(3)
    _, index, _ = build(server, folder, tmp_path / "idx")
    meta = json.loads((tmp_path / "idx" / "index.json").read_text())
    info = meta["models"]["siglip2"]
    raw = np.fromfile(tmp_path / "idx" / info["file"], dtype="<f4")
    assert raw.size == 3 * info["dimensions"] and np.allclose(np.linalg.norm(raw.reshape(3, -1), axis=1), 1, atol=1e-5)
