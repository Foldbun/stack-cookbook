"""The canonical Python client against the fake API. The scenarios come from harness/scenarios.json, which the
TypeScript and Go client tests use as well."""

import time

import pytest
import stack_client as sc
from conftest import fake_log, png, scenario

KEY = "fbs_fake"


def client(srv, **kw):
    return sc.StackClient(kw.pop("api_key", KEY), srv.url, **kw)


def calls(srv, path):
    return [e for e in fake_log(srv)["log"] if e["path"] == path]


def test_missing_key(monkeypatch):
    monkeypatch.delenv("FOLDBUN_API_KEY", raising=False)
    with pytest.raises(sc.MissingApiKey) as exc:
        sc.StackClient()
    assert exc.value.message == sc.MISSING_KEY


def test_env_configuration(server, monkeypatch):
    monkeypatch.setenv("FOLDBUN_API_KEY", KEY)
    monkeypatch.setenv("FOLDBUN_API_BASE", server.url)
    monkeypatch.setenv("FOLDBUN_MAX_CREDITS", "1.5")
    c = sc.StackClient()
    assert c.max_credits == 1.5 and "ppocr-v6" in c.models()


def test_upload_ocr_and_ledger(server):
    c = client(server)
    first, again = c.upload(png(), "image/png"), c.upload(png(), "image/png")
    assert first == again and first["id"].startswith("img_") and (first["width"], first["height"]) == (64, 48)
    r1 = c.post("/v1/ocr", {"image": sc.image_handle(first["id"])}, tag="a")
    r2 = c.post("/v1/ocr", {"image": sc.image_base64(png())}, tag="b")
    assert r1["image"]["id"] == r2["image"]["id"] == first["id"]
    assert r1["billing"]["pool"] == "free" and r1["cost"] == 0
    rows = c.calls("a")
    assert rows == [
        {"endpoint": "/v1/ocr", "model": "ppocr-v6", "list": 0.25, "charged": 0, "cache": None, "queueMs": 4, "processingMs": 700, "replayed": False}
    ]
    assert c.summary() == {"calls": 2, "listTotal": 0.5, "chargedTotal": 0}
    log = fake_log(server)["log"]
    assert all(e["userAgent"] == sc.USER_AGENT for e in log)
    assert [e["status"] for e in log if e["path"] == "/v1/images"] == [201, 200]
    keys = [e["idempotencyKey"] for e in log if e["path"] == "/v1/ocr"]
    assert len(set(keys)) == 2 and all(keys)


def test_billed_model_and_encode_cache(server):
    c = client(server)
    h = c.upload(png(), "image/png")["id"]
    body = {"model": "sam3", "image": {"id": h}, "prompts": [{"text": "cat"}, {"text": "dog"}]}
    a, b = c.post("/v1/segment", body), c.post("/v1/segment", body)
    assert (a["cache"], a["imageFee"], a["cost"]) == ("miss", 0.5, 0.6)
    assert (b["cache"], b["imageFee"], b["cost"]) == ("hit", 0, 0.1)
    assert [r["cache"] for r in c.calls()] == ["miss", "hit"] and c.summary()["chargedTotal"] == 0.7


def test_retry_honours_retry_after_and_reuses_key(server):
    scenario(server, "retry-model-busy")
    c = client(server)
    t = time.monotonic()
    c.post("/v1/ocr", {"image": sc.image_base64(png())})
    assert time.monotonic() - t >= 1.2
    attempts = calls(server, "/v1/ocr")
    assert [e["status"] for e in attempts] == [503, 200]
    assert attempts[0]["idempotencyKey"] == attempts[1]["idempotencyKey"]


def test_retry_floor_one_second(server):
    scenario(server, "rate-limited")
    c = client(server)
    h = c.upload(png(), "image/png")["id"]
    t = time.monotonic()
    c.post("/v1/segment", {"model": "sam-lite", "image": {"id": h}, "prompts": [{"box": [1, 1, 20, 20]}]})
    assert time.monotonic() - t >= 2.0
    assert [e["status"] for e in calls(server, "/v1/segment")] == [429, 429, 200]


def test_not_retryable_raises_typed_error(server):
    scenario(server, "not-retryable")
    c = client(server)
    with pytest.raises(sc.StackError) as exc:
        c.post("/v1/ocr", {"image": sc.image_base64(png())})
    e = exc.value
    assert (e.status, e.code, e.retryable) == (502, "bad_model_output", False)
    assert len(e.request_id) == 16 and len(calls(server, "/v1/ocr")) == 1


def test_idempotent_replay_and_conflict(server):
    c = client(server)
    h = c.upload(png(), "image/png")["id"]
    body = {"model": "sam3", "image": {"id": h}, "prompts": [{"text": "cat"}]}
    first = c.post("/v1/segment", body, "same-key")
    again = c.post("/v1/segment", body, "same-key")
    assert first == again
    rows = c.calls()
    assert [r["replayed"] for r in rows] == [False, True] and rows[1]["charged"] == 0
    assert c.summary()["chargedTotal"] == 0.55
    with pytest.raises(sc.StackError) as exc:
        c.post("/v1/segment", dict(body, prompts=[{"text": "dog"}]), "same-key")
    assert exc.value.code == "idempotency_conflict"


def test_reupload_after_eviction(server):
    scenario(server, "image-evicted")
    c = client(server)
    h = c.upload(png(), "image/png")["id"]
    c.post("/v1/ocr", {"image": {"id": h}})
    c.post("/v1/ocr", {"image": {"id": h}})
    assert [e["status"] for e in calls(server, "/v1/ocr")] == [200, 404, 200]
    assert len(calls(server, "/v1/images")) == 2


def test_budget_refuses_before_sending(server):
    c = client(server, max_credits=0.3)
    c.post("/v1/ocr", {"image": sc.image_base64(png())})
    with pytest.raises(sc.BudgetExceeded):
        c.post("/v1/ocr", {"image": sc.image_base64(png())})
    assert len(calls(server, "/v1/ocr")) == 1
    h = c.upload(png(), "image/png")["id"]
    with pytest.raises(sc.BudgetExceeded):  # 0.5 + 0.05 > 0.3, counted before anything is sent
        c.post("/v1/segment", {"model": "sam3", "image": {"id": h}, "prompts": [{"text": "x"}]})


def test_estimate_matches_billing_examples():
    models = {m[0]: {"prices": [{"unit": u, "price": p} for u, p in m[5].items()]} for m in __import__("harness.fakeapi").fakeapi.MODELS}
    seg = lambda model, prompts: sc.estimate(models, "/v1/segment", {"prompts": prompts}, model)
    assert seg("sam3", [{"text": "a"}, {"text": "b"}, {"text": "c"}]) == 0.65
    assert seg("sam3", [{"text": "a"}, {"text": "b"}] + [{"box": [0, 0, 1, 1]}] * 3) == 0.6
    assert seg("sam3", [{"box": [0, 0, 1, 1]}] * 4) == 0.55
    assert seg("sam3", [{"text": "a"}, {"point": {"x": 1, "y": 1}}, {"point": {"x": 2, "y": 2}}]) == 0.59
    assert seg("sam-lite", [{"box": [0, 0, 1, 1]}] * 4) == 0.24
    assert sc.estimate(models, "/v1/embeddings", {"texts": ["a", "b"]}, "siglip2") == 0.02
    assert sc.estimate(models, "/v1/documents/parse", {}, "paddleocr-vl") == 2


def test_public_tier_model_locked(server):
    c = client(server, api_key="fbs_fake_public")
    assert not c.models()["sam3"]["available"] and c.models()["ppocr-v6"]["perMinute"] == 10
    h = c.upload(png(), "image/png")["id"]
    with pytest.raises(sc.StackError) as exc:
        c.post("/v1/segment", {"model": "sam3", "image": {"id": h}, "prompts": [{"text": "cat"}]})
    assert (exc.value.status, exc.value.code) == (403, "model_locked")
    r = c.post("/v1/ocr", {"image": {"id": h}})
    assert r["billing"]["pool"] == "public" and r["billing"]["allowance"] == {"used": 1, "limit": 100}


def test_unauthorized(server):
    c = client(server, api_key="not-a-key")
    with pytest.raises(sc.StackError) as exc:
        c.models()
    assert (exc.value.status, exc.value.code) == (401, "unauthorized")


def _batch_body(c, n):
    return {"model": "sam3", "items": [{"image": {"id": c.upload(png(i), "image/png")["id"]}, "prompts": [{"text": "cat"}]} for i in range(n)]}


def test_batch_events_and_partial_failure(server):
    scenario(server, "batch-partial")
    c = client(server)
    events = list(c.batch("segment", _batch_body(c, 4), tag="b1"))
    names = [e for e, _ in events]
    assert names[0] == "progress" and names[-1] == "summary" and names.count("item") == 4
    items = [d for e, d in events if e == "item"]
    assert [i["status"] for i in items] == ["ok", "error", "ok", "expired"]
    assert items[1]["error"]["code"] == "bad_model_output"
    assert events[-1][1]["counts"] == {"ok": 2, "error": 1, "expired": 1}
    assert [r["endpoint"] for r in c.calls("b1")] == ["/v1/batch/segment"] * 2
    assert calls(server, "/v1/batch/segment")[0]["accept"] == "text/event-stream"


def test_batch_resume_replays_finished_items(server):
    scenario(server, "batch-dropped")
    c = client(server)
    body = _batch_body(c, 3)
    first = list(c.batch("segment", body, "batch-key"))
    assert [e for e, _ in first] == ["progress", "item", "item"]
    second = [d for e, d in c.batch("segment", body, "batch-key") if e == "item"]
    assert [bool(d.get("replayed")) for d in second] == [True, True, False]
    assert [r["replayed"] for r in c.calls()] == [False, False, True, True, False]
    assert c.summary()["chargedTotal"] == 1.65


def test_batch_budget(server):
    c = client(server, max_credits=1.0)
    with pytest.raises(sc.BudgetExceeded):
        list(c.batch("segment", _batch_body(c, 2)))
    assert calls(server, "/v1/batch/segment") == []


def test_network_errors_retry_only_with_key(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    c = sc.StackClient(KEY, "http://127.0.0.1:9")  # nothing listens on the discard port
    with pytest.raises(sc.StackError) as exc:
        c._send("POST", "/v1/ocr", b"{}", retry_network=False)
    assert exc.value.code == "network_error"


def test_token_bucket_paces():
    b = sc._Bucket(60)
    for _ in range(60):
        b.take()
    t = time.monotonic()
    b.take()
    assert 0.8 <= time.monotonic() - t <= 1.5
