"""Quality, speed and cost of one run of use case 05, from result.json and truth.json (standard library only).

    python evaluate.py --result <result.json> --truth <truth.json> [--timing]

Prints one JSON object of metrics on the last line. It scores the full set too: pass assets/truth.json of the extracted
archive. stats.json next to result.json adds the local search time (no network involved) and the second-pass call count.
Indexing throughput and API-reported query time depend on the client's network path, so they are printed only with
--timing and are not published.

* Image to image: each near-duplicate variant is a query; recall@k is the share of variants whose original is among the
  k nearest images (the image itself is not counted).
* Text to image: each query ranks the library by siglip2. Near-duplicate variants are not relevant by definition, so they
  are removed from the ranking before scoring; precision@k, recall@k and mean average precision (over the ranking kept
  in result.json, 100 images) are computed against the human-verified labels of the originals.
* Near-duplicates: the candidate pairs are the nearest-neighbour lists of result.json. A pair belongs to split a or b when
  both images do. The threshold is chosen on split a (the lowest threshold that keeps precision at 0.99 or better, else
  the best F1) and applied unchanged to split b, where precision, recall and F1 are reported. A true pair that is in no
  candidate list counts as a miss at every threshold.
* Fake API runs: the vectors there carry no semantics, so the quality numbers of an offline run only show that the
  plumbing works. Bounds in usecase.toml [quality] apply to live runs only.
"""

import argparse
import json
import statistics
from pathlib import Path

TARGET_PRECISION = 0.99


def at_k(ranking, relevant, k):
    hits = sum(1 for x in ranking[:k] if x in relevant)
    return hits / k, hits / len(relevant)


def average_precision(ranking, relevant):
    hits, total = 0, 0.0
    for rank, x in enumerate(ranking, 1):
        if x in relevant:
            hits += 1
            total += hits / rank
    return total / len(relevant)


def image_to_image(items, truth):
    out = {}
    pairs = truth["near_duplicates"]
    for k in (1, 5, 10):
        hits = sum(1 for p in pairs if p["source"] in (items.get(p["variant"], {}).get("output") or {}).get("neighbors", [])[:k])
        out[f"i2i_recall_at_{k}"] = hits / len(pairs) if pairs else None
    return out


def text_to_image(items, truth):
    variants = {p["variant"] for p in truth["near_duplicates"]}
    p5, p10, r10, ap = [], [], [], []
    for q in truth["queries"]:
        out = (items.get(q["id"], {}).get("output")) or {}
        ranking = [x for x in out.get("ranking", []) if x not in variants]
        relevant = set(q["relevant"])
        p5.append(at_k(ranking, relevant, 5)[0])
        p10.append(at_k(ranking, relevant, 10)[0])
        r10.append(at_k(ranking, relevant, 10)[1])
        ap.append(average_precision(ranking, relevant))
    mean = lambda v: statistics.fmean(v) if v else None  # noqa: E731
    return {"text_precision_at_5": mean(p5), "text_precision_at_10": mean(p10), "text_recall_at_10": mean(r10), "text_map": mean(ap), "text_queries": len(ap)}


def duplicate_pairs(items, truth):
    """({pair: score}, {pair: split}, set of true pairs per split) over the candidate pairs of the neighbour lists."""
    split = {i: v["split"] for i, v in truth["items"].items()}
    scores = {}
    for item_id, it in items.items():
        out = it.get("output") or {}
        if out.get("type") != "image":
            continue
        for other, s in zip(out["neighbors"], out["neighborScores"]):
            key = frozenset((item_id, other))
            scores[key] = max(scores.get(key, -1.0), s)
    true = {frozenset((p["variant"], p["source"])): p["split"] for p in truth["near_duplicates"]}
    by_split = {"a": {}, "b": {}}
    for key, s in scores.items():
        a, b = (split.get(x) for x in key)
        if a == b and a in by_split:
            by_split[a][key] = s
    return by_split, {s: {k for k, v in true.items() if v == s} for s in ("a", "b")}


def prf(pred, positives):
    tp = len(pred & positives)
    precision = tp / len(pred) if pred else 1.0
    recall = tp / len(positives) if positives else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def choose_threshold(scores, positives):
    """The lowest threshold with precision >= TARGET_PRECISION on these pairs; when none reaches it, the best F1."""
    rows = []
    for t in sorted(set(scores.values()), reverse=True):
        precision, recall, f1 = prf({k for k, v in scores.items() if v >= t}, positives)
        rows.append((t, precision, recall, f1))
    precise = [r for r in rows if r[1] >= TARGET_PRECISION and r[2] > 0]
    if precise:
        return precise[-1][0]  # the rows run from high to low threshold: the last one has the most recall
    return max(rows, key=lambda r: r[3])[0] if rows else None


def near_duplicates(items, truth):
    by_split, positives = duplicate_pairs(items, truth)
    t = choose_threshold(by_split["a"], positives["a"])
    if t is None:
        return {"dup_threshold": None, "dup_precision": None, "dup_recall": None, "dup_f1": None}
    p, r, f = prf({k for k, v in by_split["b"].items() if v >= t}, positives["b"])
    return {
        "dup_threshold": round(t, 4),
        "dup_precision": p,
        "dup_recall": r,
        "dup_f1": f,
        "dup_pairs_a": len(positives["a"]),
        "dup_pairs_b": len(positives["b"]),
    }


def speed_and_cost(result, items, stats, timing=False):
    images = [it for it in items.values() if (it.get("output") or {}).get("type") == "image"]
    embedded = [it for it in images if it["calls"]]
    out = {"images": len(images), "images_embedded": len(embedded)}
    if embedded:
        # the list price of a call does not depend on whether this run replayed a stored answer for it
        out["cost_per_1000_images_list"] = sum(c["list"] for it in embedded for c in it["calls"]) / len(embedded) * 1000
        out["cost_per_1000_images_charged"] = sum(c["charged"] for it in embedded for c in it["calls"] if not c["replayed"]) / len(embedded) * 1000
    queries = [it for it in items.values() if (it.get("output") or {}).get("type") == "query"]
    if queries:
        out["cost_per_text_query_list"] = sum(c["list"] for it in queries for c in it["calls"]) / len(queries)
        if timing:
            out["text_query_api_ms"] = statistics.median(c["processingMs"] + c["queueMs"] for it in queries for c in it["calls"])
    if stats:
        models = 2  # dinov3 and siglip2: an image counts as indexed when both vectors exist
        seconds = stats.get("indexSeconds") or 0
        if seconds and timing:
            out["index_images_per_min"] = stats["library"]["vectorsEmbedded"] / models / seconds * 60
        out["search_ms_text"], out["search_ms_image"] = stats.get("textSearchMs"), stats.get("imageSearchMs")
        out["second_pass_api_calls"] = stats["reindex"]["apiCalls"]
    return out


def evaluate(result, truth, stats=None, timing=False):
    items = {it["id"]: it for it in result["items"]}
    metrics = {}
    for part in (image_to_image(items, truth), text_to_image(items, truth), near_duplicates(items, truth), speed_and_cost(result, items, stats, timing)):
        metrics.update(part)
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in metrics.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result", type=Path, required=True)
    ap.add_argument("--truth", type=Path, required=True)
    ap.add_argument("--timing", action="store_true", help="also print indexing throughput and API-reported query time (network dependent; not published)")
    args = ap.parse_args()
    stats_file = args.result.parent / "stats.json"
    stats = json.loads(stats_file.read_text()) if stats_file.exists() else None
    print(json.dumps(evaluate(json.loads(args.result.read_text()), json.loads(args.truth.read_text()), stats, args.timing)))


if __name__ == "__main__":
    main()
