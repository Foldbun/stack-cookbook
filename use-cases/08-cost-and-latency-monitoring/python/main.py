"""Replay a workload against the Foldbun Stack API with monitoring on: Prometheus metrics at /metrics, a spend guard, and a summary.

python main.py --manifest ../assets/manifest.json --out out [--concurrency N] [--limit N] [--resume]
               [--metrics-port 9108] [--hold SECONDS] [--guard-max 0.2] [--guard-mode alert|reject] [--guard-basis charged|list]

The manifest lists the images and names a workload file (JSON lines: id, endpoint, model, image, request). The replay runs
the calls in order, one at a time by default, so "the call that tripped the guard" has a stable number.
"""

import argparse
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import stack_client as sc
from monitoring import Monitor, MonitoredClient, SpendGuard

EXIT_FAILED, EXIT_NO_KEY, EXIT_TIER, EXIT_BUDGET = 1, 2, 3, 4
CONTENT_TYPES = {".webp": "image/webp", ".png": "image/png"}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=Path(__file__).parent.parent / "assets/manifest.json")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--concurrency", type=int, default=1, help="calls in flight (default 1: the order, and so the call that trips the guard, is then fixed)")
    ap.add_argument("--limit", type=int, help="only the first N calls of the workload")
    ap.add_argument("--resume", action="store_true", help="accepted for the common interface; a replay measures one session, so it always starts over")
    ap.add_argument("--metrics-port", type=int, default=9108, help="serve GET /metrics here while the replay runs (0: do not serve; default 9108)")
    ap.add_argument("--hold", type=float, default=0.0, help="keep serving /metrics this many seconds after the replay, so that a scrape can see the end state")
    ap.add_argument("--guard-max", type=float, default=0.2, help="spend limit in credits per sliding hour (default 0.2)")
    ap.add_argument(
        "--guard-mode",
        choices=["alert", "reject"],
        default="alert",
        help="alert: call back and carry on (default); reject: refuse new calls while over the limit",
    )
    ap.add_argument("--guard-basis", choices=["charged", "list"], default="charged", help="count what is debited (default) or the list price")
    return ap.parse_args(argv)


def percentile(values, q):
    """Nearest-rank percentile of a list of numbers; None for an empty list."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def timing_stats(rows):
    return {k: {"p50": percentile([r[k] for r in rows], 0.5), "p95": percentile([r[k] for r in rows], 0.95)} for k in ("queueMs", "processingMs")} | {
        "count": len(rows)
    }


def summarise(monitor, client, guard, alerts, balance_spent):
    """The `output` of the last item: everything the monitoring layer saw, from its own metrics and the client's ledger."""
    snap = monitor.snapshot(skip=lambda name, labels: labels.get("phase") == "client_total" or name.startswith(("stack_balance_", "stack_daily_allowance_")))
    by_model_unit = {}
    for labels, value in monitor.samples("stack_credits_total"):
        row = by_model_unit.setdefault((labels["model"], labels["unit"]), {"model": labels["model"], "unit": labels["unit"], "list": 0.0, "charged": 0.0})
        row["list"] += value
        if labels["charged"] == "true":
            row["charged"] += value
    credits = [{**r, "list": round(r["list"], 6), "charged": round(r["charged"], 6)} for _, r in sorted(by_model_unit.items())]
    calls, per_model = {}, {}
    for labels, value in monitor.samples("stack_calls_total"):
        calls[labels["status"]] = calls.get(labels["status"], 0) + int(value)
        per_model[labels["model"]] = per_model.get(labels["model"], 0) + int(value)
    cache = {}
    for labels, value in monitor.samples("stack_cache_hits_total"):
        cache.setdefault(labels["model"], {"hits": 0, "misses": 0})["hits"] = int(value)
    for labels, value in monitor.samples("stack_cache_misses_total"):
        cache.setdefault(labels["model"], {"hits": 0, "misses": 0})["misses"] = int(value)
    cache = dict(sorted(cache.items()))
    hits, misses = sum(c["hits"] for c in cache.values()), sum(c["misses"] for c in cache.values())
    rows = [r for r in client.calls() if not r["replayed"]]
    return {
        "kind": "summary",
        "calls": {
            "total": sum(calls.values()),
            "ok": calls.get("ok", 0),
            "failed": sum(v for k, v in calls.items() if k not in ("ok", "replayed")),
            "byModel": dict(sorted(per_model.items())),
        },
        "credits": {
            "list": round(sum(r["list"] for r in credits), 6),
            "charged": round(sum(r["charged"] for r in credits), 6),
            "byModelUnit": credits,
        },
        "cache": {
            "hits": hits,
            "misses": misses,
            "hitRate": round(hits / (hits + misses), 4) if hits + misses else 0.0,
            "savedCredits": round(sum(v for _, v in monitor.samples("stack_cache_saved_credits_total")), 6),
            "byModel": cache,
        },
        "latency": {m: timing_stats([r for r in rows if r["model"] == m]) for m in sorted({r["model"] for r in rows})},
        "guard": {
            "maxPerHour": guard.max_per_hour,
            "mode": guard.mode,
            "basis": guard.basis,
            "triggered": bool(alerts) or guard.tripped,
            "triggeredAtCall": alerts[0]["call"] if alerts else None,
            "alerts": guard.alerts,
        },
        "balanceSpent": balance_spent,
        "metrics": snap,
    }


# region replay
def run_call(mc, entry, handles):
    """One workload line -> one result item. Failures are data (status `error`); only a locked model or the credit cap stop the run."""
    body = {"model": entry["model"], "image": sc.image_handle(handles[entry["image"]]), **entry["request"]}
    try:
        result = mc.post(entry["endpoint"], body, tag=entry["id"])
    except sc.StackError as err:
        print(f"  {entry['id']}: {err.status} {err.code}: {err.message}" + (f" (request {err.request_id})" if err.request_id else ""), file=sys.stderr)
        if err.code in ("model_locked", "budget_exceeded"):
            raise
        return {"id": entry["id"], "status": "error", "calls": [], "output": None, "error": {"status": err.status, "code": err.code, "message": err.message}}
    billing = result.get("billing") or {}
    waived = sum(ln["unitPrice"] * ln["quantity"] for ln in billing.get("lines", []) if ln.get("waived"))
    output = {
        "kind": "call",
        "model": entry["model"],
        "endpoint": entry["endpoint"],
        "cache": result.get("cache"),
        "listCredits": billing.get("total", result.get("cost", 0)),
        "chargedCredits": result.get("cost", 0),
        "waivedCredits": round(waived, 6),
    }
    return {"id": entry["id"], "status": "ok", "calls": mc.calls(entry["id"]), "output": output}


# endregion


def main(argv=None):
    args = parse_args(argv)
    manifest = json.loads(args.manifest.read_text())
    base = args.manifest.parent
    workload = [json.loads(line) for line in (base / manifest["params"]["workload"]).read_text().splitlines() if line.strip()][: args.limit]
    started, t0 = datetime.now(UTC), time.monotonic()
    # region wire
    try:
        client = sc.StackClient(max_concurrency=args.concurrency)  # FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
        client.models()  # free: checks the key
    except sc.MissingApiKey as err:
        print(err.message, file=sys.stderr)
        return EXIT_NO_KEY
    alerts = []
    guard = SpendGuard(
        args.guard_max, mode=args.guard_mode, basis=args.guard_basis, on_alert=lambda info: (alerts.append(info), print(f"ALERT: {info}", file=sys.stderr))
    )
    monitor = Monitor(guard=guard)
    mc = MonitoredClient(client, monitor)
    if args.metrics_port:
        try:
            monitor.serve(args.metrics_port)
            print(f"metrics: http://localhost:{args.metrics_port}/metrics")
        except OSError as err:
            print(f"metrics not served: {err}", file=sys.stderr)
    # endregion
    start_balance = account_balance(mc)
    handles = {}
    for item in manifest["items"]:
        path = base / item["image"]
        handles[item["id"]] = mc.upload(path.read_bytes(), CONTENT_TYPES.get(path.suffix, "image/jpeg"))["id"]  # free
    items, code = {}, 0

    def one(entry):
        nonlocal code
        try:
            items[entry["id"]] = run_call(mc, entry, handles)
        except sc.StackError as err:
            code = EXIT_TIER if err.code == "model_locked" else EXIT_BUDGET

    if args.concurrency > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(args.concurrency) as pool:
            list(pool.map(one, workload))
    else:
        for entry in workload:
            one(entry)
            if code:
                break
    if code == EXIT_TIER:
        return code  # the account cannot use a model of this workload: nothing meaningful to write
    end_balance = account_balance(mc)
    balance_spent = round(start_balance - end_balance, 6) if start_balance is not None and end_balance is not None else None
    ordered = [items[e["id"]] for e in workload if e["id"] in items]
    ordered.append({"id": "summary", "status": "ok", "calls": [], "output": summarise(monitor, client, guard, alerts, balance_spent)})
    result = {
        "usecase": manifest["usecase"],
        "language": "python",
        "client": f"stack_client {sc.VERSION}",
        "startedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsedMs": int((time.monotonic() - t0) * 1000),
        "items": ordered,
        "ledger": client.summary(),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    (args.out / "metrics.prom").write_text(monitor.exposition())
    s = ordered[-1]["output"]
    print(
        f"{s['calls']['total']} calls, list {s['credits']['list']:g}, charged {s['credits']['charged']:g}, "
        f"cache hits {s['cache']['hits']}, guard triggered at call {s['guard']['triggeredAtCall']}"
    )
    if args.hold and args.metrics_port:
        print(f"serving /metrics for {args.hold:g} more seconds")
        time.sleep(args.hold)
    return code


def account_balance(mc):
    """Balance from GET /v1/whoami, which also fills the balance and allowance gauges. None when the API has no such endpoint."""
    try:
        return mc.whoami()["balance"]
    except sc.StackError as err:
        print(f"whoami: {err.status} {err.code}: {err.message}", file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())
