"""Document pages to Markdown chunks for a search index: parse each page with paddleocr-vl, drop repeated headers and footers, cut chunks.

Writes <out>/result.json (contract/result.schema.json), <out>/chunks.jsonl (one chunk per line, ready for any embedding model and
vector store) and <out>/documents/<document>.md (the whole document as Markdown). See README.md.
"""

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import stack_client as sc
from chunker import TARGET, block_text, chunk_document, mark_repeated

EXIT_FAILED, EXIT_NO_KEY, EXIT_TIER, EXIT_BUDGET = 1, 2, 3, 4
MODEL = "paddleocr-vl"
PIPELINE = "1"  # changes when what a page entry holds changes; a resumed run only reuses entries of the same version
PAGE_ATTEMPTS = 3


# region idempotency
def idempotency_key(image_sha256: str) -> str:
    """Same file, same key (at most 128 printable ASCII characters). Within 24 hours the API replays the stored answer for a
    repeated key without running or charging the call, so a restarted run is free. The key names this program ("py")."""
    return f"r04-py-{image_sha256}-parse"


# endregion


def content_type(name: str) -> str:
    return {".png": "image/png", ".webp": "image/webp"}.get(Path(name).suffix.lower(), "image/jpeg")


# region parse-call
def parse_page(client: sc.StackClient, handle: str, sha: str, tag: str) -> dict:
    """One page, one call: layout blocks in reading order and the page as Markdown (2 credits)."""
    return client.post(
        "/v1/documents/parse",
        {"model": MODEL, "image": sc.image_handle(handle), "options": {"blocks": True}},
        idempotency_key=idempotency_key(sha),
        tag=tag,
    )


# endregion


# region page-retry
def read_page(client: sc.StackClient, item: dict, base: Path) -> dict:
    """Read one page. The client already retries what the API marks `retryable`; a page that still fails after that gets
    two more attempts here, one page at a time, so one bad page never stops the others. `bad_model_output` is not retried
    (the same image gives the same failure): it is recorded as the page's error and the page is skipped."""
    data = (base / item["image"]).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    handle = client.upload(data, content_type(item["image"]))["id"]  # free
    for attempt in range(1, PAGE_ATTEMPTS + 1):
        try:
            page = parse_page(client, handle, sha, item["id"])
            blocks = [{k: b[k] for k in ("type", "order", "bbox", "text", "markdown") if k in b} for b in page.get("blocks", [])]
            return {
                "id": item["id"],
                "status": "ok",
                "calls": client.calls(item["id"]),
                "_sha256": sha,
                "_page": item["page"],
                "_document": item["document"],
                "output": {"markdown": page["markdown"], "blocks": blocks},
            }
        except sc.StackError as err:
            if err.code in ("model_locked", "budget_exceeded") or err.code == "bad_model_output" or attempt == PAGE_ATTEMPTS or not err.retryable:
                raise
            time.sleep(min(2.0 * attempt, 10.0))
    raise AssertionError("unreachable")


# endregion


# region resume
def item_file(out: Path, item_id: str) -> Path:
    return out / "items" / f"{item_id}.json"


def load_done(out: Path, item: dict, base: Path) -> dict | None:
    """The saved entry of a page read by an earlier run of this directory, when the file and the entry format are unchanged."""
    path = item_file(out, item["id"])
    if not path.exists():
        return None
    saved = json.loads(path.read_text())
    key = [hashlib.sha256((base / item["image"]).read_bytes()).hexdigest(), PIPELINE]
    return saved["entry"] if saved["key"] == key and saved["entry"]["status"] == "ok" else None


def save_done(out: Path, entry: dict):
    """One small file per page read, written atomically, so a killed run loses at most the pages in flight."""
    path = item_file(out, entry["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"key": [entry["_sha256"], PIPELINE], "entry": entry}))
    os.replace(tmp, path)


# endregion


def ledger_of(entries: list) -> dict:
    rows = [c for e in entries for c in e["calls"]]
    fresh = [c for c in rows if not c["replayed"]]
    round6 = lambda x: round(x + 1e-12, 6)  # noqa: E731
    return {"calls": len(rows), "listTotal": round6(sum(c["list"] for c in fresh)), "chargedTotal": round6(sum(c["charged"] for c in fresh))}


# region pdf-pages
def render_pdf(pdf: Path, out: Path, dpi: int = 150) -> tuple[list[dict], Path]:
    """Optional input: a PDF, one page image per page (the API takes one page image per call). Needs `pip install pypdfium2`.
    Returns manifest items and the directory the images are in."""
    import pypdfium2 as pdfium

    pages_dir = out / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    items = []
    doc = pdfium.PdfDocument(str(pdf))
    for n in range(len(doc)):
        name = f"{pdf.stem}-p{n + 1}.jpg"
        doc[n].render(scale=dpi / 72).to_pil().convert("RGB").save(pages_dir / name, "JPEG", quality=90)
        items.append({"id": f"{pdf.stem}-p{n + 1}", "image": name, "document": pdf.stem, "page": n + 1})
    doc.close()
    return items, pages_dir


# endregion


def document_markdown(pages: list[dict]) -> str:
    parts = []
    for page in sorted(pages, key=lambda p: p["page"]):
        parts.append(f"<!-- page {page['page']} -->")
        parts += [t for t in (block_text(b) for b in sorted(page["blocks"], key=lambda b: b["order"]) if not b.get("dropped")) if t]
    return "\n\n".join(parts) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=Path(__file__).parent.parent / "assets/smoke/manifest.json")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--concurrency", type=int, default=2, help="pages in flight at once (default 2)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--resume", action="store_true", help="reuse the pages already read in --out instead of reading them again")
    ap.add_argument("--chunk-size", type=int, default=TARGET, help="target characters per chunk (default 800)")
    ap.add_argument("--pdf", type=Path, help="read this PDF instead of --manifest: each page is rendered to an image first (needs pypdfium2)")
    args = ap.parse_args()
    started, t0 = datetime.now(UTC), time.monotonic()
    if args.pdf:
        items, base = render_pdf(args.pdf, args.out)
        usecase = "04-documents-to-rag-chunks"
    else:
        manifest = json.loads(args.manifest.read_text())
        items, base, usecase = manifest["items"], args.manifest.parent, manifest["usecase"]
    items = [{**it, "document": it.get("document", it["id"]), "page": it.get("page", 1)} for it in items[: args.limit]]
    try:
        client = sc.StackClient(max_concurrency=args.concurrency)  # FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
        client.models()  # free: checks the key and loads prices and per-minute limits
    except sc.MissingApiKey as err:
        print(err.message, file=sys.stderr)
        return EXIT_NO_KEY
    except sc.StackError as err:
        print(f"{err.status} {err.code}: {err.message}", file=sys.stderr)
        return EXIT_FAILED

    entries, todo = {}, []
    for item in items:
        done = load_done(args.out, item, base) if args.resume else None
        if done:
            entries[item["id"]] = done
            print(f"{item['id']}: resumed")
        else:
            todo.append(item)

    # region concurrency
    # `--concurrency` pages are in flight (default 2); the client also spaces calls by the model's per-minute limit.
    code, lock = 0, threading.Lock()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(read_page, client, item, base): item for item in todo}
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
                save_done(args.out, entry)
                with lock:
                    print(f"{item['id']}: {len(entry['output']['blocks'])} blocks")
    # endregion

    # Documents: the pages that were read, grouped by document, in page order.
    ok = {i["id"]: entries[i["id"]] for i in items if i["id"] in entries and entries[i["id"]]["status"] == "ok"}
    meta = {i["id"]: i for i in items}
    by_doc: dict[str, list[dict]] = {}
    for pid, e in ok.items():
        by_doc.setdefault(meta[pid]["document"], []).append({"id": pid, "page": meta[pid]["page"], "blocks": e["output"]["blocks"], "sha256": e["_sha256"]})
    chunks_of: dict[str, list[dict]] = {}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "documents").mkdir(exist_ok=True)
    dropped = 0
    for doc, pages in by_doc.items():
        dropped += mark_repeated(pages)  # region dedup, see chunker.py
        chunks_of[doc] = chunk_document(doc, pages, {p["page"]: p["sha256"] for p in pages}, args.chunk_size)
        (args.out / "documents" / f"{doc}.md").write_text(document_markdown(pages))

    # region write-chunks
    with (args.out / "chunks.jsonl").open("w") as f:
        for doc in by_doc:
            for chunk in chunks_of[doc]:
                f.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    # endregion

    done = []
    for item in items:
        e = entries.get(item["id"])
        if e is None:
            continue
        e = {k: v for k, v in e.items() if not k.startswith("_")}
        if e["status"] == "ok":
            mine = [c for c in chunks_of[meta[item["id"]]["document"]] if c["page"] == meta[item["id"]]["page"]]
            e["output"] = {
                "document": meta[item["id"]]["document"],
                "page": meta[item["id"]]["page"],
                "sha256": ok[item["id"]]["_sha256"],
                **e["output"],
                "chunks": mine,
            }
        done.append(e)
    result = {
        "usecase": usecase,
        "language": "python",
        "client": f"stack_client {sc.VERSION}",
        "startedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsedMs": int((time.monotonic() - t0) * 1000),
        "items": done,
        "ledger": ledger_of(done),
    }
    (args.out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"{sum(len(c) for c in chunks_of.values())} chunks, {dropped} repeated header/footer blocks dropped")
    return code


if __name__ == "__main__":
    sys.exit(main())
