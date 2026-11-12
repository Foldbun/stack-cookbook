"""Embed a library through the API: batches, retries, the daily allowance, and a ledger row per embedded image.

Two rules shape this file:

* Items of a batch run one after another on the server, so a batch is cut into chunks (50 by default). A chunk is
  short enough to finish well inside the 600 s limit of a batch, and the index is committed after every chunk.
* The public pool includes 100 calls per UTC day, and every item of a batch is a call. When the allowance is used up
  the run stops cleanly: what was embedded is committed, the rest stays missing, and the same command run after
  00:00 UTC continues where it stopped, because only missing images are embedded.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import stack_client as sc
from index import CONTENT_TYPES, LibraryIndex

STOP_CODES = {"daily_limit_reached", "insufficient_balance", "budget_exceeded"}  # no point in trying the next image
RETRY_CODES = {"image_not_found", "model_busy", "model_unavailable", "unavailable", "upstream_error", "upstream_timeout", "internal_error"}
ATTEMPTS = 3  # a chunk is sent at most this many times; only the images that failed are sent again


@dataclass
class Report:
    """What one indexing run did. `rows[sha256]` holds the ledger rows of the calls that embedded that image."""

    embedded: int = 0
    rows: dict[str, list[dict]] = field(default_factory=dict)
    failed: dict[str, dict] = field(default_factory=dict)  # sha256 -> {code, message}
    stopped: dict | None = None  # {code, message, model} when the run stopped before the end
    allowance: dict = field(default_factory=dict)  # model -> {pool, used, limit} as last seen in a response


def _upload(client, path: Path) -> str:
    """Upload one file (free) and return its handle. The same bytes always give the same handle."""
    return client.upload(path.read_bytes(), CONTENT_TYPES.get(path.suffix.lower(), "image/jpeg"))["id"]


def _key(model: str, handles: list[str], attempt: int) -> str:
    """Idempotency key of a batch, derived from its content: a rerun of an interrupted chunk replays what finished."""
    return hashlib.sha256("|".join([model, str(attempt), *handles]).encode()).hexdigest()[:40]


# region embed-chunk
def run_batch(client, model, shas, handles, attempt, report, *, free_only, keep_free):
    """One batch request. Returns ([(sha256, vector)] that succeeded, [sha256] to send again)."""
    tag = f"{model}:{shas[0][:12]}:{attempt}"
    body = {"model": model, "items": [{"image": sc.image_handle(h)} for h in handles]}
    done, retry = [], []
    try:
        with closing(client.batch("embeddings", body, _key(model, handles, attempt), tag=tag)) as events:
            for event, data in events:
                if event != "item":
                    continue  # progress heartbeats; the summary ends the stream
                sha = shas[data["index"]]
                if data["status"] == "ok":
                    done.append((sha, data["result"]["embeddings"][0]["vector"]))
                    report.rows.setdefault(sha, []).append(client.calls(tag)[-1])  # the ledger row of the item that just arrived
                    billing = data["result"].get("billing") or {}
                    if billing.get("allowance"):
                        report.allowance[model] = {"pool": billing["pool"], **billing["allowance"]}
                        used, limit = billing["allowance"]["used"], billing["allowance"]["limit"]
                        # On the public pool the next call would be refused. With `free_only` a funded account stops
                        # here too, instead of paying for the calls that come after the included ones.
                        if (billing["pool"] == "public" or free_only) and limit - used <= keep_free:
                            report.stopped = {"code": "daily_limit_reached", "message": f"{used} of {limit} included calls used today", "model": model}
                            break  # closing the stream stops the batch; the items that already finished are kept
                elif data["status"] == "expired" or data["error"]["code"] in RETRY_CODES:
                    retry.append(sha)  # not billed; goes into the next attempt (an evicted handle is uploaded again)
                elif data["error"]["code"] in STOP_CODES:
                    report.stopped = {**data["error"], "model": model}
                    break
                else:
                    report.failed[sha] = data["error"]  # for example an image the API cannot read: no retry helps
    except sc.StackError as err:
        if err.code in STOP_CODES:  # refused before the stream started, or by the credit cap
            report.stopped = {"code": err.code, "message": err.message, "model": model}
        elif not err.retryable:
            raise
    if not report.stopped:
        # A stream can end early (a dropped connection, a timeout) without a summary event: whatever never got an
        # outcome is sent again. The finished items are kept, so nothing is embedded or paid twice.
        seen = {s for s, _ in done} | set(retry) | set(report.failed)
        retry += [s for s in shas if s not in seen]
    return done, retry


def embed_chunk(client, index: LibraryIndex, model: str, paths: dict[str, Path], report: Report, *, free_only=False, keep_free=0, workers=4) -> None:
    """Embed the images `paths` (sha256 -> file) with `model`, commit them, and record the outcome in `report`."""
    todo = list(paths)
    for attempt in range(ATTEMPTS):
        if not todo or report.stopped:
            break
        with ThreadPoolExecutor(workers) as pool:
            handles = list(pool.map(lambda sha: _upload(client, paths[sha]), todo))  # free; also re-uploads evicted images
        done, retry = run_batch(client, model, todo, handles, attempt, report, free_only=free_only, keep_free=keep_free)
        if done:
            index.add(model, [s for s, _ in done], [v for _, v in done])
            index.commit()
            report.embedded += len(done)
        todo = retry
    for sha in [] if report.stopped else todo:
        report.failed.setdefault(sha, {"code": "gave_up", "message": f"not embedded after {ATTEMPTS} attempts"})


# endregion


# region index-library
def index_library(
    client, index: LibraryIndex, root: Path, models=("dinov3", "siglip2"), *, batch_size=50, free_only=False, keep_free=0, workers=4, log=print
) -> Report:
    """Embed every library image that is missing from the index, with every model, a chunk at a time.

    Chunks alternate between the models, so that when the daily allowance ends the first part of the library has
    both vectors instead of one model having used all of it.
    """
    report = Report()
    paths = {it["sha256"]: root / it["path"] for it in index.items}
    # sha256 of every image that lacks a vector of some model, in catalogue order
    pending = list(dict.fromkeys(sha for model in models for sha in index.missing(model)))
    log(f"{len(index.items)} images in the library, {len(pending)} to embed")
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        for model in models:
            need = {sha: paths[sha] for sha in chunk if not index.has(model, sha)}
            if need and not report.stopped:
                embed_chunk(client, index, model, need, report, free_only=free_only, keep_free=keep_free, workers=workers)
        log(f"  {min(start + batch_size, len(pending))}/{len(pending)}")
        if report.stopped:
            break
    return report


# endregion


def embed_text(client, text: str, model="siglip2", tag=None) -> tuple[list[float], dict]:
    """One query text. Up to 64 texts fit in one call (and count as one call of the daily allowance); one per call keeps the cost per query."""
    result = client.post("/v1/embeddings", {"model": model, "texts": [text]}, tag=tag)
    return result["embeddings"][0]["vector"], client.calls(tag)[-1]


def embed_image(client, path: Path, model="dinov3", tag=None) -> tuple[list[float], dict]:
    """One image that is not in the library (a query by example)."""
    handle = _upload(client, path)
    result = client.post("/v1/embeddings", {"model": model, "image": sc.image_handle(handle)}, tag=tag)
    return result["embeddings"][0]["vector"], client.calls(tag)[-1]
