"""Receipts and invoices to validated JSON: read with ppocr-v6, extract fields by rules, validate, fall back to paddleocr-vl.

Writes <out>/result.json (contract/result.schema.json), <out>/receipts.jsonl (validated receipts) and
<out>/needs_review.jsonl (items that no model could make pass validation). See README.md.
"""

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path

import stack_client as sc
from extract import extract_receipt, rows_from_blocks, rows_from_ocr
from validate import passed, validate

EXIT_FAILED, EXIT_NO_KEY, EXIT_TIER, EXIT_BUDGET = 1, 2, 3, 4
OCR_MODEL, FALLBACK_MODEL = "ppocr-v6", "paddleocr-vl"
PIPELINE = "2"  # changes when extraction or validation rules change; a resumed run only reuses items of the same version


# region idempotency
def idempotency_key(image_sha256: str, model: str) -> str:
    """Same file and model give the same key (at most 128 printable ASCII characters). Within 24 hours the API
    replays the stored answer for a repeated key without running or charging the call, so a restarted run is free.
    The key names this program ("py"): the API compares the bytes of the request, and another program that writes
    the same JSON with different spacing would get `409 idempotency_conflict` under a shared key."""
    return f"r03-py-{image_sha256}-{model}"


# endregion


def content_type(name: str) -> str:
    return {".png": "image/png", ".webp": "image/webp"}.get(Path(name).suffix.lower(), "image/jpeg")


# region attempt
def summary(a: dict) -> dict:
    """What an attempt keeps in the output: the receipt as that model read it and the names of the checks it failed."""
    failed = [c["name"] for c in a["checks"] if not c["passed"]]
    return {"model": a["model"], "passed": a["passed"], "failedChecks": failed, "receipt": a["receipt"]} | ({"note": a["note"]} if "note" in a else {})


def attempt(model: str, receipt: dict, today: date) -> dict:
    checks = validate(receipt, today)
    return {"model": model, "receipt": receipt, "passed": passed(checks), "checks": checks}


# endregion


def process_item(client: sc.StackClient, item: dict, base: Path, use_fallback: bool, today: date) -> dict:
    """One image through the whole pipeline. Returns the item entry of result.json plus a private `_sha256`."""
    data = (base / item["image"]).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    handle = client.upload(data, content_type(item["image"]))["id"]  # free; both models read the same handle
    attempts = []

    # 1. ppocr-v6: 0.25 credits, inside the public pool's daily allowance.
    # region ocr-call
    ocr = client.post(
        "/v1/ocr",
        {"model": OCR_MODEL, "image": sc.image_handle(handle), "options": {"words": False}},
        idempotency_key=idempotency_key(sha, OCR_MODEL),
        tag=item["id"],
    )
    attempts.append(attempt(OCR_MODEL, extract_receipt(rows_from_ocr(ocr["lines"]), OCR_MODEL, ocr["image"]["width"]), today))
    # endregion

    # 2. Only an item that failed validation is read again, by paddleocr-vl on the same handle: 2 credits, paid models only.
    # region fallback-call
    if use_fallback and not attempts[0]["passed"]:
        try:
            page = client.post(
                "/v1/documents/parse",
                {"model": FALLBACK_MODEL, "image": sc.image_handle(handle), "options": {"blocks": True}},
                idempotency_key=idempotency_key(sha, FALLBACK_MODEL),
                tag=item["id"],
            )
            attempts.append(attempt(FALLBACK_MODEL, extract_receipt(rows_from_blocks(page["blocks"]), FALLBACK_MODEL, page["image"]["width"]), today))
        except sc.StackError as err:
            if err.code == "budget_exceeded":
                raise
            # An account without the paid pool (model_locked) or a failed call: the item stays with what ppocr-v6 read.
            attempts.append({"model": FALLBACK_MODEL, "receipt": None, "passed": False, "checks": [], "note": err.code})
    # endregion

    # 3. The result is the first attempt that passed; otherwise the attempt with the most passed checks goes to review.
    good = next((a for a in attempts if a["passed"]), None)
    tried = [a for a in attempts if a["receipt"]]
    best = good or max(enumerate(tried), key=lambda p: (sum(c["passed"] for c in p[1]["checks"]), p[0]))[1]
    output = {
        "outcome": "validated" if good else "needs_review",
        "model": best["model"],
        "receipt": best["receipt"],
        "checks": best["checks"],
        "attempts": [summary(a) for a in attempts],
    }
    return {"id": item["id"], "status": "ok", "calls": client.calls(item["id"]), "output": output, "_sha256": sha}


# region resume
def item_file(out: Path, item_id: str) -> Path:
    return out / "items" / f"{item_id}.json"


def load_done(out: Path, item: dict, base: Path, use_fallback: bool) -> dict | None:
    """The saved entry of an item finished by an earlier run of this directory, when the file, the rules and the options are unchanged."""
    path = item_file(out, item["id"])
    if not path.exists():
        return None
    saved = json.loads(path.read_text())
    key = [hashlib.sha256((base / item["image"]).read_bytes()).hexdigest(), PIPELINE, use_fallback]
    return saved["entry"] if saved["key"] == key and saved["entry"]["status"] == "ok" else None


def save_done(out: Path, entry: dict, use_fallback: bool):
    """One small file per finished item, written atomically, so a killed run loses at most the items in flight."""
    path = item_file(out, entry["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in entry.items() if k != "_sha256"}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"key": [entry["_sha256"], PIPELINE, use_fallback], "entry": clean}))
    os.replace(tmp, path)


# endregion


def ledger_of(entries: list) -> dict:
    rows = [c for e in entries for c in e["calls"]]
    fresh = [c for c in rows if not c["replayed"]]
    round6 = lambda x: round(x + 1e-12, 6)  # noqa: E731
    return {"calls": len(rows), "listTotal": round6(sum(c["list"] for c in fresh)), "chargedTotal": round6(sum(c["charged"] for c in fresh))}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=Path(__file__).parent.parent / "assets/smoke/manifest.json")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--resume", action="store_true", help="reuse the items already finished in --out instead of reading them again")
    ap.add_argument("--no-fallback", action="store_true", help="OCR only: items that fail validation go straight to needs_review.jsonl")
    args = ap.parse_args()
    use_fallback = not args.no_fallback
    manifest = json.loads(args.manifest.read_text())
    base, today = args.manifest.parent, date.today()
    started, t0 = datetime.now(UTC), time.monotonic()
    try:
        client = sc.StackClient(max_concurrency=args.concurrency)  # FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
        client.models()  # free: checks the key and loads prices and the per-minute limits the client paces itself by
    except sc.MissingApiKey as err:
        print(err.message, file=sys.stderr)
        return EXIT_NO_KEY
    except sc.StackError as err:
        print(f"{err.status} {err.code}: {err.message}", file=sys.stderr)
        return EXIT_FAILED

    items = manifest["items"][: args.limit]
    entries, todo = {}, []
    for item in items:
        done = load_done(args.out, item, base, use_fallback) if args.resume else None
        if done:
            entries[item["id"]] = done
            print(f"{item['id']}: resumed")
        else:
            todo.append(item)

    # region concurrency
    # The client allows `--concurrency` requests in flight and spaces calls by the per-minute limit of the model's pool,
    # so the worker threads here only keep it busy. One item is one thread: its calls are sequential (OCR, then maybe parse).
    code, lock = 0, threading.Lock()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(process_item, client, item, base, use_fallback, today): item for item in todo}
        for future in as_completed(futures):
            item = futures[future]
            try:
                entry = future.result()
            except sc.StackError as err:
                print(f"{item['id']}: {err.status} {err.code}: {err.message}" + (f" (request {err.request_id})" if err.request_id else ""), file=sys.stderr)
                if err.code in ("model_locked", "budget_exceeded"):
                    code = EXIT_TIER if err.code == "model_locked" else EXIT_BUDGET
                    for f in futures:
                        f.cancel()
                    continue
                entry = {
                    "id": item["id"],
                    "status": "error",
                    "calls": client.calls(item["id"]),
                    "output": None,
                    "error": {"status": err.status, "code": err.code, "message": err.message},
                }
            entries[item["id"]] = entry
            if entry["status"] == "ok":
                save_done(args.out, entry, use_fallback)
                with lock:
                    print(f"{item['id']}: {entry['output']['outcome']} ({entry['output']['model']})")
    # endregion

    done = [{k: v for k, v in entries[i["id"]].items() if k != "_sha256"} for i in items if i["id"] in entries]
    args.out.mkdir(parents=True, exist_ok=True)
    # region needs-review
    # What no model could make pass goes to a person, with the best receipt as a starting point and the reasons.
    with (args.out / "needs_review.jsonl").open("w") as review, (args.out / "receipts.jsonl").open("w") as good:
        for entry in done:
            out = entry["output"]
            if out is None:
                continue
            if out["outcome"] == "validated":
                good.write(json.dumps({"id": entry["id"], "model": out["model"], "receipt": out["receipt"]}, ensure_ascii=False) + "\n")
            else:
                reasons = [c["detail"] for c in out["checks"] if not c["passed"]]
                review.write(json.dumps({"id": entry["id"], "reasons": reasons, "model": out["model"], "receipt": out["receipt"]}, ensure_ascii=False) + "\n")
    # endregion
    result = {
        "usecase": manifest["usecase"],
        "language": "python",
        "client": f"stack_client {sc.VERSION}",
        "startedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsedMs": int((time.monotonic() - t0) * 1000),
        "items": done,
        "ledger": ledger_of(done),
    }
    (args.out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
