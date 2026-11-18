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


PROJECT = "fbp_testProject0001"


def free(srv, **kw):
    return sc.StackClient("", srv.url, project=kw.pop("project", PROJECT), **kw)


def test_no_key_means_free_tier_with_the_cookbook_project(monkeypatch):
    monkeypatch.delenv("FOLDBUN_API_KEY", raising=False)
    monkeypatch.delenv("FOLDBUN_PROJECT_ID", raising=False)
    c = sc.StackClient()
    assert c.free and c.project == sc.COOKBOOK_PROJECT == "fbp_2kIj7bWkULQkLAbceHhfLW4eY6O0"
    monkeypatch.setenv("FOLDBUN_PROJECT_ID", PROJECT)
    monkeypatch.setenv("FOLDBUN_API_KEY", KEY)
    c = sc.StackClient()
    assert not c.free and c.project == PROJECT


def test_free_tier_lists_models_with_the_project_alone(server):
    c = free(server)
    models = c.models()
    assert "ppocr-v6" in models and models["sam3"]["free"] == {"class": "medium", "rpm": 120, "burst": 2, "daily": 10000}
    (entry,) = calls(server, "/v1/models")
    assert (entry["project"], entry["hasAuthorization"]) == (PROJECT, False)
    assert "pool" not in models["sam3"] and client(server).models()["sam3"]["pool"] == "paid"


def test_free_call_sends_only_the_project_header(server):
    c = free(server)
    r = c.post("/v1/ocr", {"image": sc.image_base64(png())}, tag="a")
    assert r["cost"] == 0 and "balance" not in r
    assert r["billing"]["tier"] == "free" and r["billing"]["project"] == PROJECT and r["billing"]["listPrice"] == 0.25 and r["billing"]["charged"] is False
    (entry,) = calls(server, "/v1/ocr")
    assert (entry["project"], entry["hasAuthorization"], entry["idempotencyKey"], entry["status"]) == (PROJECT, False, None, 200)
    assert c.calls("a") == [
        {"endpoint": "/v1/ocr", "model": "ppocr-v6", "list": 0.25, "charged": 0, "cache": None, "queueMs": 4, "processingMs": 700, "replayed": False}
    ]
    assert c.summary() == {"calls": 1, "listTotal": 0.25, "chargedTotal": 0}
    assert c.whoami()["tier"] == "free" and c.whoami()["project"]["id"] == PROJECT


def test_keyed_call_is_paid_and_names_the_source_project(server):
    c = client(server, project=PROJECT)
    r = c.post("/v1/ocr", {"image": sc.image_base64(png())})
    assert r["billing"]["charged"] is True and r["cost"] == 0.25 and r["balance"] == 999.75
    (entry,) = calls(server, "/v1/ocr")
    assert (entry["project"], entry["hasAuthorization"]) == (PROJECT, True) and entry["idempotencyKey"]


def test_free_tier_refuses_locally_what_it_does_not_have(server):
    c = free(server)
    for fn in (
        lambda: c.upload(png(), "image/png"),
        lambda: c.post("/v1/ocr", {"image": sc.image_handle("img_" + "0" * 64)}),
        lambda: list(c.batch("ocr", {"items": []})),
    ):
        with pytest.raises(sc.MissingApiKey) as exc:
            fn()
        assert exc.value.code == "missing_api_key" and exc.value.message.startswith("FOLDBUN_API_KEY is not set") and "\n" not in exc.value.message
    assert fake_log(server)["log"] == []


def test_fake_free_tier_rules(server):
    """The fake itself refuses what the free tier does not have, so a client that sends it gets caught."""
    import json
    import urllib.request

    def call(method, path, body=None, headers=None):
        req = urllib.request.Request(server.url + path, data=body, method=method, headers={"X-Foldbun-Project": PROJECT, **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    ocr = json.dumps({"image": sc.image_base64(png())}).encode()
    ctype = {"Content-Type": "application/json"}
    assert call("POST", "/v1/ocr", ocr, ctype)[0] == 200
    status, body = call("POST", "/v1/ocr", ocr, {**ctype, "Idempotency-Key": "k"})
    assert (status, body["error"]["code"]) == (400, "unsupported")
    assert call("POST", "/v1/images", png(), {"Content-Type": "image/png"})[0] == 400
    assert call("POST", "/v1/batch/ocr", json.dumps({"items": [{"image": {"id": "img_" + "0" * 64}}]}).encode(), ctype)[0] == 400
    assert call("GET", "/v1/models")[0] == 200
    big = json.dumps({"image": {"base64": __import__("base64").b64encode(b"\0" * (4 * 1024 * 1024 + 1)).decode()}}).encode()
    assert call("POST", "/v1/ocr", big, ctype)[1]["error"]["code"] == "payload_too_large"
    assert call("GET", "/v1/whoami", headers={"X-Foldbun-Project": "nope"})[0] == 401


def test_free_limits_are_enforced_end_to_end(server):
    """Burst 2 at 60 per minute: the remaining-zero header holds the third call back, the daily cap ends the run."""
    scenario(server, {"free": {"enforce": True, "ipShare": 1, "classes": {"medium": {"rpm": 60, "burst": 2, "daily": 3}}}})
    c = free(server)
    t = time.monotonic()
    for _ in range(3):
        c.post("/v1/ocr", {"image": sc.image_base64(png())})
    assert time.monotonic() - t >= 0.9  # the second answer said remaining 0, reset 1
    assert [e["status"] for e in calls(server, "/v1/ocr")] == [200, 200, 200]  # never refused: the client waited
    with pytest.raises(sc.StackError) as exc:
        c.post("/v1/ocr", {"image": sc.image_base64(png())})
    e = exc.value
    assert (e.status, e.code, e.retryable) == (429, "daily_limit_reached", False) and "FOLDBUN_API_KEY" in e.message
    assert len(calls(server, "/v1/ocr")) == 4
    assert free(server, project="fbp_otherProject01").post("/v1/ocr", {"image": sc.image_base64(png())})["cost"] == 0


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
    assert r1["billing"]["pool"] == "paid" and r1["cost"] == 0.25
    rows = c.calls("a")
    assert rows == [
        {"endpoint": "/v1/ocr", "model": "ppocr-v6", "list": 0.25, "charged": 0.25, "cache": None, "queueMs": 4, "processingMs": 700, "replayed": False}
    ]
    assert c.summary() == {"calls": 2, "listTotal": 0.5, "chargedTotal": 0.5}
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
    assert free(server, max_credits=0.01).post("/v1/ocr", {"image": sc.image_base64(png())})["cost"] == 0  # nothing is charged: no guard
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


def test_free_tier_retries_busy_and_gives_up_on_final_codes(server):
    scenario(server, "free-busy")
    c = free(server)
    t = time.monotonic()
    c.post("/v1/ocr", {"image": sc.image_base64(png())})
    assert time.monotonic() - t >= 1.2 and [e["status"] for e in calls(server, "/v1/ocr")] == [429, 200]
    for name, code, status in (("free-daily-limit", "daily_limit_reached", 429), ("free-unavailable", "free_tier_unavailable", 403)):
        scenario(server, name)
        with pytest.raises(sc.StackError) as exc:
            free(server).post("/v1/ocr", {"image": sc.image_base64(png())})
        e = exc.value
        assert (e.status, e.code, e.retryable) == (status, code, False) and "FOLDBUN_API_KEY" in e.message
        assert len(calls(server, "/v1/ocr")) == 1


def test_remaining_zero_holds_the_next_call(server):
    scenario(server, "free-remaining-zero")
    c = free(server)
    c.post("/v1/ocr", {"image": sc.image_base64(png())})
    c.post("/v1/ocr", {"image": sc.image_base64(png())})
    first, second = calls(server, "/v1/ocr")
    assert second["t"] - first["t"] >= 1.0


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


# region async jobs
def _job_items(c, n):
    return [{"image": sc.image_handle(c.upload(png(i), "image/png")["id"])} for i in range(n)]


def test_job_runs_to_completion_and_lands_in_the_ledger(server):
    c = client(server)
    items = _job_items(c, 5)
    job = c.create_job("siglip2", items, "job-key", metadata={"dataset": "t"})
    assert (job["status"], job["counts"]["total"], job["estimate"], job["metadata"]) == ("queued", 5, 0.025, {"dataset": "t"})
    (entry,) = calls(server, "/v1/jobs")
    assert (entry["status"], entry["idempotencyKey"], entry["hasAuthorization"]) == (202, "job-key", True)
    assert c._reserved == pytest.approx(0.025)  # the worst case is held until the job is final
    running = c.get_job(job["id"])
    assert running["status"] == "running" and 0 < running["counts"]["ok"] < 5 and c._reserved > 0
    done = c.get_job(job["id"])
    assert (done["status"], done["counts"]["ok"], done["cost"]) == ("completed", 5, 0.025)
    assert c._reserved == pytest.approx(0) and c._spent == pytest.approx(0.025)
    got = list(c.job_results(job["id"], tag="j"))
    assert [i["index"] for i in got] == [0, 1, 2, 3, 4] and {i["status"] for i in got} == {"ok"}
    assert len(got[0]["result"]["embeddings"][0]["vector"]) == got[0]["result"]["dimensions"]
    rows = c.calls("j")
    assert len(rows) == 5 and rows[0] == {
        "endpoint": "/v1/jobs",
        "model": "siglip2",
        "list": 0.005,
        "charged": 0.005,
        "cache": "miss",
        "queueMs": 4,
        "processingMs": 80,
        "replayed": False,
    }
    assert c.summary() == {"calls": 5, "listTotal": 0.025, "chargedTotal": 0.025}
    assert c._spent == pytest.approx(0.025)  # reading the results does not count the spend twice
    assert len(list(c.job_results(job["id"]))) == 5 and c.summary()["calls"] == 5  # nor does reading them again


def test_job_results_page_by_cursor_and_cache_hits_are_free(server):
    c = client(server)
    items = _job_items(c, 4)
    first = c.create_job("siglip2", items)
    while c.get_job(first["id"])["status"] != "completed":
        pass
    list(c.job_results(first["id"]))
    second = c.create_job("siglip2", items)  # the same four images again: cached, so free
    while c.get_job(second["id"])["status"] != "completed":
        pass
    got = list(c.job_results(second["id"], limit=2, tag="again"))
    assert [i["index"] for i in got] == [0, 1, 2, 3]
    assert len(calls(server, f"/v1/jobs/{second['id']}/results")) == 3  # 2 + 2 + the empty last page
    assert [r["cache"] for r in c.calls("again")] == ["hit"] * 4 and c.summary()["chargedTotal"] == 0.02


def test_job_with_failed_items(server):
    scenario(server, "job-partial")
    c = client(server)
    job = c.create_job("siglip2", _job_items(c, 4))
    while c.get_job(job["id"])["status"] != "completed":
        pass
    items = list(c.job_results(job["id"], tag="p"))
    assert [i["status"] for i in items] == ["ok", "error", "ok", "ok"] and items[1]["error"]["code"] == "bad_model_output"
    assert len(c.calls("p")) == 3 and c.get_job(job["id"])["counts"] == {
        "total": 4,
        "pending": 0,
        "running": 0,
        "ok": 3,
        "error": 1,
        "expired": 0,
        "cancelled": 0,
    }


def test_job_text_items_and_idempotent_replay(server):
    c = client(server)
    items = [{"texts": ["a dog", "a cat"]}, {"texts": ["a bird"]}]
    a = c.create_job("siglip2", items, "same")
    b = c.create_job("siglip2", items, "same")
    assert a["id"] == b["id"] and [e["replayed"] for e in calls(server, "/v1/jobs")] == [False, True]
    assert c._reserved == pytest.approx(a["estimate"]) == pytest.approx(0.003)  # one hold, not two
    with pytest.raises(sc.StackError) as exc:
        c.create_job("siglip2", items[:1], "same")
    assert (exc.value.status, exc.value.code) == (409, "idempotency_conflict")
    c.cancel_job(a["id"])
    assert c._reserved == pytest.approx(0)


def test_cancel_job(server):
    c = client(server)
    job = c.create_job("dinov3", _job_items(c, 3))
    c.get_job(job["id"])  # one poll: part of the items ran
    cancelled = c.cancel_job(job["id"])
    assert cancelled["status"] == "cancelled" and cancelled["counts"]["cancelled"] > 0
    assert [i["status"] for i in c.job_results(job["id"])].count("cancelled") == cancelled["counts"]["cancelled"]
    assert c._reserved == pytest.approx(0) and c._spent == pytest.approx(cancelled["cost"])
    with pytest.raises(sc.StackError) as exc:
        c.cancel_job(job["id"])
    assert (exc.value.status, exc.value.code) == (409, "job_finished")


def test_job_is_refused_locally_without_a_key_or_over_the_cap(server):
    f = free(server)
    gone = "job_" + "0" * 32
    for fn in (lambda: f.create_job("siglip2", []), lambda: f.get_job(gone), lambda: list(f.job_results(gone)), lambda: f.cancel_job(gone)):
        with pytest.raises(sc.MissingApiKey):
            fn()
    c = client(server, max_credits=0.01)
    items = _job_items(c, 3)  # the worst case is 3 x 0.005
    with pytest.raises(sc.BudgetExceeded) as exc:
        c.create_job("siglip2", items)
    assert exc.value.message == "call refused: up to 0.015 credits would pass FOLDBUN_MAX_CREDITS=0.01 (spent 0)"
    assert calls(server, "/v1/jobs") == []


def test_job_submit_retries_too_many_jobs(server, monkeypatch):
    scenario(server, "job-too-many")
    c = client(server)
    items = _job_items(c, 1)
    slept = []
    monkeypatch.setattr(sc.time, "sleep", slept.append)
    job = c.create_job("siglip2", items, "retry-key")
    assert job["status"] == "queued" and len(slept) == 1 and slept[0] >= 1.2
    first, second = calls(server, "/v1/jobs")
    assert (first["status"], second["status"]) == (429, 202) and first["idempotencyKey"] == second["idempotencyKey"] == "retry-key"


def test_job_submit_without_balance_is_402(server):
    scenario(server, {"balance": 0.001})
    c = client(server)
    items = _job_items(c, 2)
    with pytest.raises(sc.StackError) as exc:
        c.create_job("siglip2", items)
    assert (exc.value.status, exc.value.code) == (402, "insufficient_balance") and c._reserved == 0


# endregion
