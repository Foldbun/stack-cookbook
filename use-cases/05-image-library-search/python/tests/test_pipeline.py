import json

import embedder
import numpy as np
import pytest
import search
import stack_client as sc
from conftest import api_calls
from index import LibraryIndex, scan_folder

from harness.fakeapi import tiny_png

KEY, PROJECT = "fbs_fake", "fbp_testProject0001"


def build(server, folder, index_dir, key=KEY, **kw):
    """Index `folder` with a key (batches, handles), or without one (key=None: the free tier, single calls with base64)."""
    client = sc.StackClient(key or "", server.url, project=PROJECT)
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


def test_free_mode_embeds_with_single_inline_calls(server, make_library, tmp_path):
    folder = make_library(6)
    client, index, report = build(server, folder, tmp_path / "idx", key=None)
    assert client.free and report.embedded == 12 and not report.stopped and not report.failed
    assert len(api_calls(server, "/v1/embeddings")) == 12  # one call per image and model
    assert not api_calls(server, "/v1/batch") and not api_calls(server, "/v1/images")  # no batch, no upload, no handle
    assert all(len(rows) == 2 and all(r["charged"] == 0 and r["list"] > 0 for r in rows) for rows in report.rows.values())
    assert client.summary()["chargedTotal"] == 0 and client.summary()["listTotal"] > 0
    assert "free tier, not charged" in embedder.cost_line(client)
    before = len(api_calls(server, "/v1/embeddings"))
    _, _, report = build(server, folder, tmp_path / "idx", key=None)  # incremental: nothing is missing
    assert report.embedded == 0 and len(api_calls(server, "/v1/embeddings")) == before
    _, keyed, _ = build(server, folder, tmp_path / "idx2")  # the keyed vectors are the same numbers
    assert np.allclose(index.matrix("siglip2")[1], keyed.matrix("siglip2")[1])


def test_free_mode_needs_a_key_for_a_bigger_library(server, make_library, tmp_path):
    folder = make_library(embedder.FREE_MAX_IMAGES + 1)
    with pytest.raises(sc.MissingApiKey) as exc:
        build(server, folder, tmp_path / "idx", key=None)
    assert str(embedder.FREE_MAX_IMAGES + 1) in exc.value.message and not api_calls(server, "/v1/embeddings")
    _, index, report = build(server, folder, tmp_path / "idx")  # with a key the library has no size limit
    assert report.embedded == 2 * len(index.items) and not report.failed


def test_free_mode_stops_at_the_daily_limit_and_resumes(server, make_library, tmp_path):
    folder = make_library(10)
    server.fake.reset({"free": {"enforce": True, "ipShare": 1, "classes": {"light": {"rpm": 6000, "burst": 100, "daily": 7}}}})
    _, index, report = build(server, folder, tmp_path / "idx", key=None, batch_size=4)
    assert report.stopped and report.stopped["code"] == "daily_limit_reached" and not report.failed
    saved = LibraryIndex.load(tmp_path / "idx")  # what finished was committed
    assert report.embedded == sum(len(m["sha256s"]) for m in saved.models.values()) == 7
    server.fake.reset({})  # "next day": the daily count starts again
    _, index, report = build(server, folder, tmp_path / "idx", key=None, batch_size=4)
    assert report.embedded == 13 and not report.stopped
    assert all(len(m["sha256s"]) == 10 for m in index.models.values())


def test_credit_cap_stops_a_keyed_run_and_resumes(server, make_library, tmp_path):
    folder = make_library(8)
    client = sc.StackClient(KEY, server.url, max_credits=0.5)  # 16 images at 0.05 would cost 0.8
    index = LibraryIndex.load(tmp_path / "idx")
    index.set_items(scan_folder(folder, index.items))
    index.commit()
    report = embedder.index_library(client, index, folder, batch_size=4, log=lambda *_: None)
    assert report.stopped and report.stopped["code"] == "budget_exceeded"
    kept = sum(len(m["sha256s"]) for m in LibraryIndex.load(tmp_path / "idx").models.values())
    assert 0 < kept < 16 and report.embedded == kept
    _, index, report = build(server, folder, tmp_path / "idx")  # the same command without the cap embeds only what is missing
    assert report.embedded == 16 - kept and not report.stopped


def test_partial_batch_is_retried(server, make_library, tmp_path):
    folder = make_library(6)
    server.fake.reset({"rules": [{"path": "/v1/batch/*", "times": 1, "itemErrors": {"1": "bad_model_output"}, "expireFrom": 3}]})
    _, index, report = build(server, folder, tmp_path / "idx", batch_size=6)
    # item 1 failed for good, items 3 to 5 expired (not billed) and went into the next attempt
    assert len(report.failed) == 1 and next(iter(report.failed.values()))["code"] == "bad_model_output"
    assert len(index.models["dinov3"]["sha256s"]) == 5


def test_stream_that_drops_is_resumed_without_charging_twice(server, make_library, tmp_path, monkeypatch):
    monkeypatch.setattr(embedder, "_pause", lambda _s: None)
    folder = make_library(6)
    server.fake.reset({"rules": [{"path": "/v1/batch/*", "times": 1, "disconnectAfterItems": 2}]})
    client, index, report = build(server, folder, tmp_path / "idx", batch_size=6)
    assert not report.failed and len(index.models["dinov3"]["sha256s"]) == 6
    assert client.summary()["listTotal"] == 0.6  # 12 images at 0.05, each billed once


def batch_keys(server):
    return [e["idempotencyKey"] for e in api_calls(server, "/v1/batch/embeddings")]


def test_item_charged_but_event_lost_is_recovered_under_the_same_key(server, make_library, tmp_path, monkeypatch):
    monkeypatch.setattr(embedder, "_pause", lambda _s: None)
    folder = make_library(6)
    # the server runs and charges item 2, then the connection drops before its event is written
    server.fake.reset({"rules": [{"path": "/v1/batch/*", "times": 1, "disconnectBeforeItem": 2}]})
    funded = server.fake.balance["funded"]
    client, index, report = build(server, folder, tmp_path / "idx", batch_size=6, models=("dinov3",))
    assert not report.failed and report.embedded == 6 and len(index.models["dinov3"]["sha256s"]) == 6
    keys = batch_keys(server)
    assert len(keys) == 2 and len(set(keys)) == 1  # the unknown item is asked again under the key of the first batch
    assert round(funded - server.fake.balance["funded"], 6) == 0.3  # six images at 0.05, each charged once
    rows = [r for rs in report.rows.values() for r in rs]
    assert len(rows) == 6 and sum(1 for r in rows if r["replayed"]) == 1  # only the item whose event was lost comes back as a replay


def test_item_with_unknown_outcome_is_never_sent_under_a_new_key(server, make_library, tmp_path, monkeypatch):
    monkeypatch.setattr(embedder, "_pause", lambda _s: None)
    folder = make_library(4)
    # every send of the batch loses the event of item 1: the outcome stays unknown
    server.fake.reset({"rules": [{"path": "/v1/batch/*", "times": 1 + embedder.RECOVERY_SENDS, "disconnectBeforeItem": 1}]})
    funded = server.fake.balance["funded"]
    _, index, report = build(server, folder, tmp_path / "idx", batch_size=4, models=("dinov3",))
    assert len(index.models["dinov3"]["sha256s"]) == 1  # only item 0 arrived
    assert {v["code"] for v in report.failed.values()} == {"outcome_unknown"} and len(report.failed) == 3
    assert len(batch_keys(server)) == 1 + embedder.RECOVERY_SENDS and len(set(batch_keys(server))) == 1
    assert round(funded - server.fake.balance["funded"], 6) == 0.1  # item 0 and the item whose event was lost, once each


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


# region async
def async_build(server, folder, index_dir, key=KEY, **kw):
    return build(server, folder, index_dir, key=key, async_mode=True, poll_s=0, **kw)


def test_async_embeds_with_one_job_per_model(server, make_library, tmp_path):
    folder = make_library(8)
    client, index, report = async_build(server, folder, tmp_path / "idx")
    assert report.embedded == 16 and not report.stopped and not report.failed
    submits = [e for e in api_calls(server, "/v1/jobs") if e["path"] == "/v1/jobs"]
    assert len(submits) == 2 and all(e["idempotencyKey"] for e in submits)  # one job per model
    assert not api_calls(server, "/v1/batch")
    assert client.summary() == {"calls": 16, "listTotal": 0.08, "chargedTotal": 0.08}  # a tenth of the 0.8 of the batch run
    assert all(len(rows) == 2 and all(r["endpoint"] == "/v1/jobs" for r in rows) for rows in report.rows.values())
    assert index.dimensions("dinov3") == 16 and len(index.models["siglip2"]["sha256s"]) == 8


def test_async_matches_batch_vectors_at_a_tenth_of_the_price(server, make_library, tmp_path):
    folder = make_library(6)
    c1, batch_index, _ = build(server, folder, tmp_path / "b")
    c2, async_index, _ = async_build(server, folder, tmp_path / "a")
    for model in ("dinov3", "siglip2"):
        assert np.allclose(batch_index.matrix(model)[1], async_index.matrix(model)[1])
    assert c1.summary()["listTotal"] == 0.6 and c2.summary()["listTotal"] == 0.06


def test_async_second_run_submits_nothing(server, make_library, tmp_path):
    folder = make_library(5)
    async_build(server, folder, tmp_path / "idx")
    before = len(api_calls(server, "/v1/jobs"))
    _, _, report = async_build(server, folder, tmp_path / "idx")
    assert report.embedded == 0 and len(api_calls(server, "/v1/jobs")) == before


def test_async_failed_item_is_left_for_the_next_run(server, make_library, tmp_path):
    folder = make_library(5)
    server.fake.reset({"jobs": {"polls": 1, "itemErrors": {"1": "bad_model_output"}}})
    _, index, report = async_build(server, folder, tmp_path / "idx")
    assert len(report.failed) == 1 and next(iter(report.failed.values()))["code"] == "bad_model_output"
    assert len(index.models["dinov3"]["sha256s"]) == 4 and report.embedded == 8
    server.fake.reset({})
    _, index, report = async_build(server, folder, tmp_path / "idx")  # only the image that failed (with each model) is embedded
    assert report.embedded == 2 and not report.failed and len(index.models["dinov3"]["sha256s"]) == 5


def test_async_expired_items_stay_missing(server, make_library, tmp_path):
    folder = make_library(5)
    server.fake.reset({"jobs": {"polls": 1, "expireFrom": 3}})
    _, index, report = async_build(server, folder, tmp_path / "idx")
    assert report.embedded == 6 and {f["code"] for f in report.failed.values()} == {"expired"}
    assert len(index.models["dinov3"]["sha256s"]) == 3


def test_async_stops_when_the_balance_does_not_cover_a_job(server, make_library, tmp_path):
    folder = make_library(6)
    server.fake.reset({"balance": 0.04})  # a job needs 0.03
    _, index, report = async_build(server, folder, tmp_path / "idx")
    assert report.stopped["code"] == "insufficient_balance" and len(index.models["dinov3"]["sha256s"]) == 6
    assert not index.models.get("siglip2", {}).get("sha256s")  # the second job was refused; the first one is kept
    server.fake.reset({})
    _, index, report = async_build(server, folder, tmp_path / "idx")
    assert report.embedded == 6 and not report.stopped


def test_async_needs_a_key(server, make_library, tmp_path):
    folder = make_library(3)
    with pytest.raises(sc.MissingApiKey):
        async_build(server, folder, tmp_path / "idx", key=None)
    assert api_calls(server, "/v1/jobs") == []


# endregion
