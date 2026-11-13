"""Use case 08: the monitoring layer (metrics, spend guard, batch items, whoami) and evaluate.py, offline."""

import importlib.util
import json
import sys
import tomllib
import urllib.request
from pathlib import Path

import pytest
import stack_client as sc
from conftest import ROOT, png, scenario

UC = ROOT / "use-cases" / "08-cost-and-latency-monitoring"
sys.path.insert(0, str(UC / "python"))

from monitoring import Monitor, MonitoredClient, SpendGuard, SpendGuardTripped  # noqa: E402

spec = importlib.util.spec_from_file_location("uc08_evaluate", UC / "evaluate.py")
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


def answer(model="sam3", cost=0.55, cache="miss", lines=None, charged=True, pool=None, allowance=None, balance=90.0, queue=4, processing=600):
    """An answer in the shape of a real one: id, model, cost, balance, timing, billing{lines, total, charged, pool}."""
    lines = lines or [
        {"unit": "encode_image", "quantity": 1, "unitPrice": 0.5, "amount": 0 if cache == "hit" else 0.5, "waived": cache == "hit"},
        {"unit": "prompt", "quantity": 1, "unitPrice": 0.05, "amount": 0.05, "waived": False},
    ]
    billing = {"lines": lines, "total": round(sum(ln["amount"] for ln in lines), 6), "charged": charged, "pool": pool}
    if allowance:
        billing["allowance"] = allowance
    body = {"id": "call_x", "model": model, "cost": cost, "balance": balance, "timing": {"queueMs": queue, "processingMs": processing}, "billing": billing}
    if cache:
        body["cache"] = cache
    return body


class Stub:
    """A client whose `post` returns canned answers and keeps a ledger like StackClient."""

    def __init__(self, *answers, replayed=False):
        self.answers, self.ledger, self.replayed = list(answers), [], replayed
        self.models_called = 0

    def post(self, path, body, idempotency_key=None, *, tag=None, **kwargs):
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        self.ledger.append((tag, {"replayed": self.replayed}))
        return item

    def get(self, path):
        return {
            "balance": 12.5,
            "dailyAllowances": [{"pool": "free", "used": 130, "limit": 1000, "resetsAt": "2026-11-13T00:00:00Z"}],
            "keyId": "k",
            "accountId": "a",
            "scopes": [],
        }


def value(monitor, name, **labels):
    return next((v for lab, v in monitor.samples(name) if all(lab.get(k) == x for k, x in labels.items())), 0)


def test_metrics_labels_and_values():
    m = Monitor()
    c = MonitoredClient(
        Stub(
            answer(cost=0.55, cache="miss"),
            answer(cost=0.05, cache="hit"),
            answer(
                "ppocr-v6",
                cost=0,
                cache=None,
                charged=False,
                pool="public",
                lines=[{"unit": "image", "quantity": 1, "unitPrice": 0.25, "amount": 0.25, "waived": False}],
                allowance={"used": 7, "limit": 100},
            ),
        ),
        m,
    )
    for _ in range(3):
        c.post("/v1/segment", {"model": "sam3", "prompts": [{"text": "x"}]}, tag="t")
    assert value(m, "stack_credits_total", model="sam3", unit="encode_image", charged="true") == 0.5  # the hit's fee was waived: amount 0
    assert value(m, "stack_credits_total", model="sam3", unit="prompt", charged="true") == pytest.approx(0.1)
    assert value(m, "stack_credits_total", model="ppocr-v6", unit="image", charged="false") == 0.25  # inside the allowance: list price, not charged
    assert value(m, "stack_cache_hits_total", model="sam3") == 1 and value(m, "stack_cache_misses_total", model="sam3") == 1
    assert value(m, "stack_cache_saved_credits_total", model="sam3") == 0.5
    assert value(m, "stack_calls_total", model="sam3", status="ok", code="none") == 2
    assert value(m, "stack_latency_ms_count", model="sam3", phase="queue") == 2 and value(m, "stack_latency_ms_sum", model="sam3", phase="processing") == 1200
    assert value(m, "stack_latency_ms_count", model="sam3", phase="client_total") == 2
    assert value(m, "stack_balance_credits") == 90.0 and value(m, "stack_balance_updated_timestamp_seconds") > 0
    assert (value(m, "stack_daily_allowance_used", pool="public"), value(m, "stack_daily_allowance_limit", pool="public")) == (7, 100)
    text = m.exposition()
    assert 'stack_credits_total{charged="true",model="sam3",unit="prompt"}' in text and "stack_latency_ms_bucket" in text
    snap = m.snapshot(skip=lambda n, lab: lab.get("phase") == "client_total")
    assert not any("client_total" in k or "_bucket" in k or "_created" in k for k in snap)


def test_errors_and_replays_are_counted_not_charged():
    m = Monitor()
    c = MonitoredClient(Stub(sc.StackError(429, "daily_limit_reached", "x"), sc.BudgetExceeded("cap"), answer(), replayed=True), m)
    with pytest.raises(sc.StackError):
        c.post("/v1/ocr", {"image": {"id": "img_x"}})
    with pytest.raises(sc.BudgetExceeded):
        c.post("/v1/segment", {"model": "sam-lite"})
    c.post("/v1/segment", {"model": "sam3"})
    assert value(m, "stack_calls_total", model="ppocr-v6", status="error", code="daily_limit_reached") == 1  # model from the default of the path
    assert value(m, "stack_calls_total", model="sam-lite", status="refused", code="budget_exceeded") == 1
    assert value(m, "stack_calls_total", model="sam3", status="replayed", code="none") == 1
    assert m.samples("stack_credits_total") == [] and value(m, "stack_latency_ms_count", model="sam3", phase="queue") == 0


def test_guard_alert_mode_with_a_fake_clock():
    now, fired = [0.0], []
    g = SpendGuard(1.0, mode="alert", on_alert=fired.append, window_s=3600, clock=lambda: now[0])
    assert not g.record(0.4) and not g.record(0.6)  # exactly the limit is not over it
    now[0] = 600
    assert g.record(0.01) and g.tripped and [f["call"] for f in fired] == [3]
    assert not g.record(0.5) and len(fired) == 1  # one alert per crossing, not one per call
    now[0] = 3601  # the first two events (1.0 credits) have left the window: 0.51 left, under the limit
    assert g.spent() == pytest.approx(0.51)
    assert not g.record(0.0) and not g.tripped
    assert g.record(0.6) and len(fired) == 2  # armed again, so a second crossing alerts again
    assert fired[0]["spent"] == pytest.approx(1.01) and fired[0]["max_per_hour"] == 1.0


def test_guard_reject_mode_refuses_until_the_window_slides():
    now = [0.0]
    g = SpendGuard(1.0, mode="reject", clock=lambda: now[0])
    g.record(1.5)
    with pytest.raises(SpendGuardTripped) as exc:
        g.check()
    assert exc.value.code == "spend_guard_tripped" and exc.value.status == 0
    now[0] = 3600.0  # an event leaves the window exactly one window after it happened
    g.check()
    m = Monitor(guard=SpendGuard(0.1, mode="reject"))
    c = MonitoredClient(Stub(answer(cost=0.55)), m)
    c.post("/v1/segment", {"model": "sam3"})
    with pytest.raises(SpendGuardTripped):
        c.post("/v1/segment", {"model": "sam3"})  # refused before the stub is asked: it has no answer left
    assert value(m, "stack_calls_total", model="sam3", status="refused", code="spend_guard_tripped") == 1
    assert value(m, "stack_spend_guard_tripped") == 1 and value(m, "stack_spend_window_credits") == pytest.approx(0.55)


def test_guard_basis_list_counts_covered_calls():
    free = answer(
        "ppocr-v6",
        cost=0,
        cache=None,
        charged=False,
        pool="public",
        lines=[{"unit": "image", "quantity": 1, "unitPrice": 0.25, "amount": 0.25, "waived": False}],
    )
    for basis, tripped in (("charged", False), ("list", True)):
        m = Monitor(guard=SpendGuard(0.2, basis=basis))
        MonitoredClient(Stub(dict(free)), m).post("/v1/ocr", {"image": {"id": "img_x"}})
        assert m.guard.tripped is tripped


def test_batch_items_are_counted_one_by_one(server):
    client = sc.StackClient("fbs_fake", server.url, throttle=False)
    scenario(server, {"rules": [{"path": "/v1/batch/ocr", "itemErrors": {"1": "invalid_image"}}]})
    handles = [client.upload(png(i), "image/png")["id"] for i in range(3)]
    m = Monitor(guard=SpendGuard(10))
    c = MonitoredClient(client, m)
    events = [e for e, _ in c.batch("ocr", {"model": "ppocr-v6", "items": [{"image": {"id": h}} for h in handles]}, tag="b")]
    assert events.count("item") == 3 and events[-1] == "summary"
    assert value(m, "stack_calls_total", model="ppocr-v6", status="ok", code="none") == 2
    assert value(m, "stack_calls_total", model="ppocr-v6", status="error", code="invalid_image") == 1
    assert value(m, "stack_credits_total", model="ppocr-v6", unit="image", charged="false") == 0.5  # two ok items at 0.25 each, inside the allowance
    assert value(m, "stack_latency_ms_count", model="ppocr-v6", phase="processing") == 2
    assert value(m, "stack_latency_ms_count", model="ppocr-v6", phase="client_total") == 0  # a batch has no per-item client time
    assert m.guard.records == 3
    assert client.summary()["listTotal"] == 0.5 and m.samples("stack_credits_total")  # the ledger and the metrics see the same two items


def test_wrapped_client_against_the_fake_api(server):
    client = sc.StackClient("fbs_fake", server.url, throttle=False)
    m = Monitor()
    c = MonitoredClient(client, m)
    image = c.upload(png(), "image/png")["id"]  # forwarded to the wrapped client
    for text in ("a", "b"):
        c.post("/v1/segment", {"model": "sam3", "image": {"id": image}, "prompts": [{"text": text}]}, tag="x")
    assert value(m, "stack_cache_hits_total", model="sam3") == 1 and value(m, "stack_cache_misses_total", model="sam3") == 1
    assert value(m, "stack_credits_total", model="sam3", unit="encode_image", charged="true") == 0.5
    assert [r["cache"] for r in c.calls("x")] == ["miss", "hit"] and c.summary()["chargedTotal"] == pytest.approx(0.6)
    scenario(server, "not-retryable")
    with pytest.raises(sc.StackError):
        c.post("/v1/ocr", {"image": {"id": image}})
    assert value(m, "stack_calls_total", model="ppocr-v6", status="error", code="bad_model_output") == 1


def test_whoami_refresh(server):
    client = sc.StackClient("fbs_fake", server.url, throttle=False)
    m = Monitor()
    info = MonitoredClient(client, m).whoami()
    assert info["balance"] == 1000 and value(m, "stack_balance_credits") == 1000
    assert (value(m, "stack_daily_allowance_used", pool="free"), value(m, "stack_daily_allowance_limit", pool="free")) == (0, 1000)
    image = client.upload(png(), "image/png")["id"]
    client.post("/v1/ocr", {"image": {"id": image}})  # a call that the wrapper never saw: only a refresh knows about it
    MonitoredClient(client, m).whoami()
    assert value(m, "stack_daily_allowance_used", pool="free") == 1
    stub = MonitoredClient(Stub(), Monitor())
    assert stub.whoami()["balance"] == 12.5 and value(stub.monitor, "stack_daily_allowance_limit", pool="free") == 1000


def test_metrics_are_served(server):
    m = Monitor()
    MonitoredClient(Stub(answer()), m).post("/v1/segment", {"model": "sam3"})
    httpd, _ = m.serve(0, addr="127.0.0.1")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/metrics") as r:
            body = r.read().decode()
    finally:
        httpd.shutdown()
    assert 'stack_calls_total{code="none",model="sam3",status="ok"} 1.0' in body


def test_middleware_overhead_is_small():
    us = evaluate.overhead_us(n=1000, repeats=3)
    assert 0 < us < 500, f"{us} microseconds per call"  # about 20 on a laptop; the bound only catches an accidental O(n) or a lock fight


def test_whoami_of_the_fake_api_matches_the_contract(server):
    req = urllib.request.Request(server.url + "/v1/whoami", headers={"Authorization": "Bearer fbs_fake_public"})
    with urllib.request.urlopen(req) as r:
        info = json.loads(r.read())
    assert info["balance"] == 0 and [a["pool"] for a in info["dailyAllowances"]] == ["public"] and info["scopes"] == []


def test_evaluate_and_quality_floors_on_the_recorded_offline_run():
    result = json.loads((UC / "expected" / "offline.json").read_text())
    m = evaluate.metrics(result)
    assert m["ledger_consistent"] == 1 and m["credits_charged_gap"] == 0 and m["calls_total"] == 21
    assert m["cache_hits.sam3"] == 4 and m["cache_hits.sam-lite"] == 9 and m["cache_saved_credits"] == pytest.approx(3.8)
    assert m["guard_triggered"] == 1 and m["guard_trigger_call"] == 8  # the first sam3 call: its encoding alone is over 0.2 credits
    for name, bound in tomllib.loads((UC / "usecase.toml").read_text())["quality"].items():
        assert bound.get("min", float("-inf")) <= m[name] <= bound.get("max", float("inf")), name
    broken = json.loads(json.dumps(result))
    broken["ledger"]["chargedTotal"] += 0.01
    assert evaluate.metrics(broken)["ledger_consistent"] == 0
    assert "latency" not in json.dumps(m).lower() or all("_ms_" in k for k in m if "latency" in k)


def test_workload_fits_the_budget_and_matches_the_readme():
    meta = tomllib.loads((UC / "usecase.toml").read_text())
    lines = [json.loads(x) for x in (UC / "assets" / "workload.jsonl").read_text().splitlines()]
    assert (
        [ln["model"] for ln in lines].count("ppocr-v6") == 6
        and [ln["model"] for ln in lines].count("sam-lite") == 10
        and [ln["model"] for ln in lines].count("sam3") == 5
    )
    prices = {"ppocr-v6": (0.25, 0.0), "sam-lite": (0.2, 0.01), "sam3": (0.5, 0.05)}  # (encoding or per image, per prompt), list prices
    spent, seen = 0.0, set()
    for ln in lines:
        base, per = prices[ln["model"]]
        sam = ln["model"] != "ppocr-v6"
        worst = base + per if sam else base
        assert spent + worst <= meta["sizes"]["smoke"]["budget_credits"] + 1e-9, ln["id"]  # the client's cap counts the encoding fee every time
        spent += (per if (ln["model"], ln["image"]) in seen else base + per) if sam else base
        seen.add((ln["model"], ln["image"]))
    assert spent == pytest.approx(2.55)
    assert Path(UC / "assets" / "manifest.json").exists()
