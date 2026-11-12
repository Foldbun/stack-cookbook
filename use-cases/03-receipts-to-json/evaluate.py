"""Quality, cost and speed of a run of 03-receipts-to-json, from result.json and the ground truth.

    python evaluate.py --result out/result.json --truth assets/smoke/truth.json [--out expected/smoke-summary.json] [--timing]

The documents are split by number: odd ones are the development half, even ones the held-out half. The metrics of the
held-out half carry the prefix `heldout_` (`heldout_validation_pass_rate`, ...); the `[quality]` floors apply to them.

Prints one flat JSON object of metrics on the last line of stdout (the harness reads that line). Standard library only.
It scores the 80-document full set as well as the smoke set: pass assets/truth.json of the extracted archive. Quality and
cost only; throughput depends on the client's network path, so it is printed only with --timing and never published.

Two strategies are scored from the same run. `ocr_only` takes what ppocr-v6 alone produced (output.attempts[0]); items that
fail validation would go to a person. `hybrid` is the pipeline: the receipt of output.receipt, which is the paddleocr-vl
reading for the items that failed validation and were read again.
"""

import argparse
import json
from pathlib import Path

ROUND = 4
HALF_MIN = 10  # smallest held-out half that gets its own metrics


def same_amount(a, b):
    return a is not None and b is not None and abs(a - b) < 0.005


def norm(text):
    return " ".join((text or "").lower().split())


def scores(receipt, truth):
    """Booleans per field; a field is absent when the truth does not have it (CORD has no date and no merchant)."""
    out = {"total": same_amount(receipt["total"], truth["total"]), "line_item_count": len(receipt["lineItems"]) == truth["line_item_count"]}
    if truth.get("date"):
        out["date"] = receipt["date"] == truth["date"]
    if truth.get("vendor"):
        out["merchant"] = norm(receipt["merchant"]) == norm(truth["vendor"])
    return out


def rate(values):
    values = list(values)
    return round(sum(values) / len(values), ROUND) if values else None


def held_out(item_id):
    """The held-out half: documents whose number is even (cord-002, inv-004, ...). The rules were developed on the odd ones."""
    tail = item_id.rsplit("-", 1)[-1]
    return tail.isdigit() and int(tail) % 2 == 0


def evaluate(result, truth, timing=True):
    """Metrics for every document, and with the prefix `heldout_` for the held-out half alone."""
    metrics = metrics_of(result["items"], truth, timing, result["elapsedMs"])
    half = [i for i in result["items"] if held_out(i["id"])]
    if len(half) >= HALF_MIN and len(half) < len(result["items"]):  # the 4 documents of the smoke set are too few to split
        metrics.update({f"heldout_{k}": v for k, v in metrics_of(half, truth, False, 0).items() if k not in ("items", "items_scored")})
    return metrics


def metrics_of(entries, truth, timing, elapsed_ms):
    items = [i for i in entries if i["status"] == "ok" and i["id"] in truth["items"]]
    n = len(items)
    metrics = {"items": len(entries), "items_scored": n}

    def accuracy(prefix, pick, only=lambda o: True):
        fields = {}
        for item in items:
            receipt = pick(item["output"])
            if receipt is not None and only(item["output"]):
                for name, ok in scores(receipt, truth["items"][item["id"]]).items():
                    fields.setdefault(name, []).append(ok)
        for name in ("total", "date", "line_item_count", "merchant"):
            metrics[f"{prefix}{name}_accuracy"] = rate(fields.get(name, []))

    # The pipeline as run: every item, and only the items that were accepted without review.
    accuracy("", lambda o: o["receipt"])
    accuracy("accepted_", lambda o: o["receipt"], lambda o: o["outcome"] == "validated")
    accuracy("ocr_only_", lambda o: o["attempts"][0]["receipt"])

    first = [i["output"]["attempts"][0]["passed"] for i in items]
    fell_back = [len([a for a in i["output"]["attempts"] if a["receipt"]]) > 1 for i in items]
    final = [i["output"]["outcome"] == "validated" for i in items]
    metrics.update(
        validation_pass_rate=rate(final),  # accepted without review, after the fallback
        ocr_only_validation_pass_rate=rate(first),
        fallback_rate=rate(fell_back),
        needs_review_rate=rate(not f for f in final),
        ocr_only_needs_review_rate=rate(not f for f in first),
    )

    # Cost: list price of every call of the run, per 1,000 documents. `charged` leaves out what the daily allowance covered.
    calls = [c for i in entries for c in i["calls"]]
    ocr_list = sum(c["list"] for c in calls if c["model"] == "ppocr-v6")
    all_list = sum(c["list"] for c in calls)
    charged = sum(c["charged"] for c in calls if not c["replayed"])
    per_1000 = lambda x: round(x / max(len(entries), 1) * 1000, 2)  # noqa: E731
    metrics.update(
        ocr_only_cost_per_1000_list=per_1000(ocr_list),
        hybrid_cost_per_1000_list=per_1000(all_list),
        hybrid_cost_per_1000_charged=per_1000(charged),
    )
    if timing:  # not for offline results: against the fake API the time says nothing about Stack
        metrics["throughput_items_per_minute"] = round(len(entries) / max(elapsed_ms, 1) * 60000, 1)
    return metrics


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result", type=Path, required=True)
    ap.add_argument("--truth", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="also write the metrics to this file")
    ap.add_argument("--timing", action="store_true", help="also print throughput (client-side, depends on the network; not published)")
    args = ap.parse_args()
    metrics = evaluate(json.loads(args.result.read_text()), json.loads(args.truth.read_text()), args.timing)
    if args.out:
        args.out.write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics))


if __name__ == "__main__":
    main()
