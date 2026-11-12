"""Index the images of a manifest, then run its text queries; write result.json for the harness and the evaluator.

This is the entry point the cookbook harness runs (`--manifest`, `--out`). To use the same code on a folder of your
own photos, see imgsearch.py. Next to result.json this writes:

    index/        the vector index (format described in index.py)
    stats.json    timing, what was embedded, what the second (no-op) indexing pass did, the daily allowance
"""

import argparse
import json
import shutil
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import embedder
import search
import stack_client as sc
from index import LibraryIndex, file_sha256

EXIT_FAILED, EXIT_NO_KEY, EXIT_TIER, EXIT_BUDGET = 1, 2, 3, 4
MODELS = ("dinov3", "siglip2")
NEIGHBORS = 10  # nearest images kept per image: enough to evaluate duplicates and retrieval by example
TEXT_RESULTS = 100  # ranked images kept per text query


def r4(x: float) -> float:
    return round(float(x), 4)


def catalogue(manifest: dict, base: Path, limit: int | None) -> list[dict]:
    """Library items from the manifest: id, path, sha256 (checked against the manifest when it has one)."""
    items = []
    for it in manifest["items"][:limit]:
        path = base / it["image"]
        sha = file_sha256(path)
        if it.get("sha256") and it["sha256"] != sha:
            raise ValueError(f"{it['image']}: sha256 differs from the manifest")
        items.append({"id": it["id"], "path": it["image"], "sha256": sha, "bytes": path.stat().st_size})
    return items


def error_of(err: dict | None, default_code: str) -> dict:
    err = err or {"code": default_code, "message": "not embedded in this run"}
    return {"code": err["code"], "message": err["message"]}


# region image-outputs
def image_outputs(index: LibraryIndex, ids: list[str]) -> dict[str, dict]:
    """Per image: dimensions, its nearest neighbours by dinov3, and its near-duplicate group at the default threshold."""
    items, matrix = index.matrix("dinov3")
    row_of = {it["id"]: i for i, it in enumerate(items)}
    groups = {items[i]["id"]: group for group in search.duplicate_groups(matrix, search.DEFAULT_THRESHOLD) for i in group}
    by_id, out = {it["id"]: it for it in index.items}, {}
    for item_id in ids:
        it = by_id[item_id]
        row = row_of.get(item_id)
        ranked = search.top_k(matrix, matrix[row], NEIGHBORS, exclude=row) if row is not None else []
        group = groups.get(item_id)
        out[item_id] = {
            "type": "image",
            "sha256": it["sha256"],
            "dimensions": {m: index.dimensions(m) for m in MODELS if index.has(m, it["sha256"])},
            "neighbors": [items[i]["id"] for i, _ in ranked],
            "neighborScores": [r4(s) for _, s in ranked],
            "duplicateGroup": items[group[0]]["id"] if group else None,
            "groupSize": len(group) if group else 1,
        }
    return out


# endregion


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=Path(__file__).parent.parent / "assets/smoke/manifest.json")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--resume", action="store_true", help="continue from the index in <out>/index (without it the index is rebuilt from scratch)")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--free-only", action="store_true", help="stop when the included daily calls are used up instead of paying for more")
    args = ap.parse_args()
    manifest = json.loads(args.manifest.read_text())  # not resolved: the images are found next to it, also behind a symlink
    base = args.manifest.parent
    started, t0 = datetime.now(UTC), time.monotonic()
    try:
        client = sc.StackClient(max_concurrency=args.concurrency)
        client.models()  # free: checks the key
    except sc.MissingApiKey as err:
        print(err.message, file=sys.stderr)
        return EXIT_NO_KEY

    index_dir = args.out / "index"
    if not args.resume:
        shutil.rmtree(index_dir, ignore_errors=True)
    index = LibraryIndex.load(index_dir)
    index.set_items(catalogue(manifest, base, args.limit))
    index.commit()
    items = [it["id"] for it in index.items]
    first_with = {it["sha256"]: it["id"] for it in reversed(index.items)}  # an exact copy shares its twin's vector, and its calls

    code, stats = 0, {}
    try:
        # region run-index
        t_index = time.monotonic()
        report = embedder.index_library(client, index, base, MODELS, batch_size=args.batch_size, free_only=args.free_only, workers=args.concurrency)
        stats["indexSeconds"] = round(time.monotonic() - t_index, 3)
        # endregion
        # region reindex-check
        calls_before = len(client.ledger)
        again = embedder.index_library(client, index, base, MODELS, batch_size=args.batch_size, workers=args.concurrency, log=lambda *_: None)
        stats["reindex"] = {"apiCalls": len(client.ledger) - calls_before, "embedded": again.embedded}  # nothing is missing, so nothing is sent
        # endregion
        if not report.stopped and not report.failed and stats["reindex"]["apiCalls"]:
            print("second indexing pass made API calls: incremental indexing is broken", file=sys.stderr)
            code = EXIT_FAILED
    except sc.StackError as err:  # model_locked, or a failure no retry helps
        print(f"  {err.status} {err.code}: {err.message}" + (f" (request {err.request_id})" if err.request_id else ""), file=sys.stderr)
        return EXIT_TIER if err.code == "model_locked" else EXIT_FAILED

    outputs = image_outputs(index, items)
    by_id, result_items = {it["id"]: it for it in index.items}, []
    t_search = time.monotonic()
    for item_id in items:
        sha = by_id[item_id]["sha256"]
        entry = {"id": item_id, "calls": [], "output": None}
        done = all(index.has(m, sha) for m in MODELS)
        if done:
            entry["status"], entry["output"] = "ok", outputs[item_id]
        elif sha in report.failed:
            entry["status"], entry["error"] = "error", error_of(report.failed[sha], "failed")
        else:
            entry["status"], entry["error"] = "expired", error_of(report.stopped, "stopped")
        if first_with[sha] == item_id:
            entry["calls"] = report.rows.get(sha, [])
        result_items.append(entry)
    stats["imageSearchMs"] = round((time.monotonic() - t_search) * 1000 / max(len(items), 1), 3)

    # region text-queries
    text_items, text_ms = [], []
    siglip_items, siglip_matrix = index.matrix("siglip2")
    for q in manifest.get("queries", []):
        entry = {"id": q["id"], "calls": [], "output": None}
        try:
            vector, row = embedder.embed_text(client, q["text"], tag=q["id"])
            t = time.monotonic()
            ranked = search.top_k(siglip_matrix, vector, TEXT_RESULTS)
            text_ms.append((time.monotonic() - t) * 1000)
            entry["status"], entry["calls"] = "ok", [row]
            entry["output"] = {"type": "query", "text": q["text"], "ranking": [siglip_items[i]["id"] for i, _ in ranked], "scores": [r4(s) for _, s in ranked]}
        except sc.StackError as err:
            print(f"  {q['id']}: {err.status} {err.code}: {err.message}", file=sys.stderr)
            if err.code == "model_locked":
                return EXIT_TIER
            entry["status"] = "expired" if err.code in embedder.STOP_CODES else "error"
            entry["error"] = {"status": err.status, "code": err.code, "message": err.message}
            if err.code == "budget_exceeded":
                code = EXIT_BUDGET
        text_items.append(entry)
    # endregion
    stats["textSearchMs"] = round(statistics.median(text_ms), 3) if text_ms else None

    stats.update(
        library={"images": len(index.items), "uniqueImages": len({it["sha256"] for it in index.items}), "vectorsEmbedded": report.embedded},
        stopped=report.stopped,
        allowance=report.allowance,
        failed=len(report.failed),
    )
    result = {
        "usecase": manifest["usecase"],
        "language": "python",
        "client": f"stack_client {sc.VERSION}",
        "startedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsedMs": int((time.monotonic() - t0) * 1000),
        "items": result_items + text_items,
        "ledger": client.summary(),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    (args.out / "stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    if report.stopped:
        print(f"stopped early: {report.stopped['message']}; run again with --resume after 00:00 UTC", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
