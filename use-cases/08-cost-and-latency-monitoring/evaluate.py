"""Do the books of use case 08 agree, did the cache and the guard do their job, and what does the monitoring layer cost per call.

    python evaluate.py --result out/result.json [--truth ignored] [--overhead]

Prints one JSON object of flat metrics on its last line (standard library only, except --overhead, which imports
python/monitoring.py and needs prometheus-client). Language independent: it reads only `result.json`, whose last item
(`summary`) holds what the implementation's monitoring layer counted, and compares that with the client's ledger.

  ledger_consistent            1 when the credits the metrics counted equal the ledger (list and charged) and so do the call counts
  credits_list_gap, credits_charged_gap    absolute differences behind it
  credits_charged.<model>, credits_list.<model>    metrics, summed over units
  cache_hits[.<model>], cache_misses, cache_hit_rate, cache_saved_credits    encoding cache; saved = list price of the waived fees
  guard_triggered, guard_trigger_call      whether the spend guard fired and after how many calls
  queue_ms_p50.<model>, queue_ms_p95.<model>, processing_ms_p50.<model>, processing_ms_p95.<model>    server timing of the answers
  middleware_overhead_us       with --overhead: microseconds one call costs with monitoring on, against the bare stub client

No client-side latency is measured or printed: it depends on the network path between a client and the API.
"""

import argparse
import json
import sys
import time
from pathlib import Path

TOLERANCE = 1e-6


def metrics(result):
    summary = next(it["output"] for it in result["items"] if it["id"] == "summary")
    ledger, credits, cache, guard = result["ledger"], summary["credits"], summary["cache"], summary["guard"]
    out = {
        "ledger_consistent": int(
            abs(credits["list"] - ledger["listTotal"]) <= TOLERANCE
            and abs(credits["charged"] - ledger["chargedTotal"]) <= TOLERANCE
            and summary["calls"]["total"] == ledger["calls"]
        ),
        "credits_list_gap": round(abs(credits["list"] - ledger["listTotal"]), 6),
        "credits_charged_gap": round(abs(credits["charged"] - ledger["chargedTotal"]), 6),
        "calls_total": summary["calls"]["total"],
        "calls_failed": summary["calls"]["failed"],
        "cache_hits": cache["hits"],
        "cache_misses": cache["misses"],
        "cache_hit_rate": cache["hitRate"],
        "cache_saved_credits": cache["savedCredits"],
        "guard_triggered": int(guard["triggered"]),
        "guard_trigger_call": guard["triggeredAtCall"],
    }
    for row in credits["byModelUnit"]:
        for key in ("list", "charged"):
            name = f"credits_{key}.{row['model']}"
            out[name] = round(out.get(name, 0) + row[key], 6)
    for model, c in cache["byModel"].items():
        out[f"cache_hits.{model}"] = c["hits"]
    for model, lat in summary["latency"].items():
        for key, label in (("queueMs", "queue_ms"), ("processingMs", "processing_ms")):
            out[f"{label}_p50.{model}"], out[f"{label}_p95.{model}"] = lat[key]["p50"], lat[key]["p95"]
    return out


def overhead_us(n=3000, repeats=5):
    """Microseconds per call that MonitoredClient adds to a client whose `post` returns a canned answer at once.

    Measured with the guard on, a SAM-shaped answer (3 billing lines, timing, balance, allowance) and the best of `repeats`
    rounds of `n` calls, which leaves out scheduler noise. Everything else (network, JSON, retries) is the same with and without."""
    sys.path.insert(0, str(Path(__file__).parent / "python"))
    from monitoring import Monitor, MonitoredClient, SpendGuard

    answer = {
        "id": "call_00000000-0000-7000-8000-000000000001",
        "model": "sam3",
        "cost": 0.05,
        "balance": 100.0,
        "cache": "hit",
        "timing": {"queueMs": 4, "processingMs": 600},
        "billing": {
            "total": 0.05,
            "charged": True,
            "pool": None,
            "lines": [
                {"unit": "encode_image", "quantity": 1, "unitPrice": 0.5, "amount": 0, "waived": True},
                {"unit": "prompt", "quantity": 1, "unitPrice": 0.05, "amount": 0.05, "waived": False},
            ],
        },
    }

    class Stub:
        ledger = []

        def post(self, path, body, idempotency_key=None, *, tag=None, **kwargs):
            return answer

    stub, body = Stub(), {"model": "sam3", "prompts": [{"text": "x"}]}
    wrapped = MonitoredClient(stub, Monitor(guard=SpendGuard(1e9)))
    best_bare = best_wrapped = float("inf")
    for _ in range(repeats):
        t = time.perf_counter()
        for _ in range(n):
            stub.post("/v1/segment", body)
        best_bare = min(best_bare, time.perf_counter() - t)
        t = time.perf_counter()
        for _ in range(n):
            wrapped.post("/v1/segment", body)
        best_wrapped = min(best_wrapped, time.perf_counter() - t)
    return round((best_wrapped - best_bare) / n * 1e6, 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result", type=Path, required=True)
    ap.add_argument("--truth", type=Path, help="accepted for the common interface; there is no ground truth in this use case")
    ap.add_argument("--overhead", action="store_true", help="also measure the per-call cost of the monitoring layer")
    args = ap.parse_args()
    out = metrics(json.loads(args.result.read_text()))
    if args.overhead:
        out["middleware_overhead_us"] = overhead_us()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
