"""Embed a library through the API: batches or single calls, retries, and a ledger row per embedded image.

Two modes, chosen by the client (`client.free`, true when there is no API key):

* With an API key an image is uploaded once and embedded by a batch request that refers to its handle, charged at
  list price. Items of a batch run one after another on the server, so a batch is cut into chunks (50 by default):
  a chunk finishes well inside the 600 s limit of a batch, and the index is committed after every chunk.
* Without a key (the free tier) there are no uploads, handles or batches: each image is sent inline as base64 in a
  single `/v1/embeddings` call. The free tier is meant for trying the recipe, so a library of more than
  FREE_MAX_IMAGES images needs a key. Calls are spaced by the free tier's per-minute limits; the client waits and
  retries on 429 by itself, so this code does not pace anything.

Either way the run can stop cleanly (the credit cap, or the free tier's daily limit): what was embedded is committed,
the rest stays missing, and the same command run again continues where it stopped, because only missing images are
embedded.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import stack_client as sc
from index import CONTENT_TYPES, LibraryIndex

STOP_CODES = {"daily_limit_reached", "insufficient_balance", "budget_exceeded"}  # no point in trying the next image
RETRY_CODES = {
    "image_not_found",
    "model_busy",
    "model_unavailable",
    "unavailable",
    "upstream_error",
    "upstream_timeout",
    "internal_error",
    "rate_limited",
    "free_busy",
}
ATTEMPTS = 3  # a chunk is sent at most this many times; only the images that failed are sent again
# The library of the smoke run has 25 images, so 25 is also what the free tier accepts: 25 images x 2 models = 50 light-class
# calls. A bigger library is real work and needs a key (it would also run into the daily limit of the free tier).
FREE_MAX_IMAGES = 25


@dataclass
class Report:
    """What one indexing run did. `rows[sha256]` holds the ledger rows of the calls that embedded that image."""

    embedded: int = 0
    rows: dict[str, list[dict]] = field(default_factory=dict)
    failed: dict[str, dict] = field(default_factory=dict)  # sha256 -> {code, message}
    stopped: dict | None = None  # {code, message, model} when the run stopped before the end


def _upload(client, path: Path) -> str:
    """Upload one file (free of charge) and return its handle. The same bytes always give the same handle."""
    return client.upload(path.read_bytes(), CONTENT_TYPES.get(path.suffix.lower(), "image/jpeg"))["id"]


def _key(model: str, handles: list[str], attempt: int) -> str:
    """Idempotency key of a batch, derived from its content: a rerun of an interrupted chunk replays what finished."""
    return hashlib.sha256("|".join([model, str(attempt), *handles]).encode()).hexdigest()[:40]


# region embed-chunk
def run_batch(client, model, shas, handles, attempt, report):
    """One batch request (needs a key). Returns ([(sha256, vector)] that succeeded, [sha256] to send again)."""
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
                elif data["status"] == "expired" or data["error"]["code"] in RETRY_CODES:
                    retry.append(sha)  # not billed; goes into the next attempt (an evicted handle is uploaded again)
                elif data["error"]["code"] in STOP_CODES:
                    report.stopped = {**data["error"], "model": model}
                    break  # closing the stream stops the batch; the items that already finished are kept
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


def run_singles(client, model, paths, shas, report, workers):
    """The free tier: one `/v1/embeddings` call per image, the bytes inline as base64 (at most 4 MiB). Same return value as run_batch."""

    def one(sha):
        if report.stopped:
            return sha, "retry", None  # another call already hit a limit: send nothing more
        tag = f"{model}:{sha[:12]}"
        body = {"model": model, "image": sc.image_base64(paths[sha].read_bytes())}
        try:
            result = client.post("/v1/embeddings", body, tag=tag)  # the client waits and retries 429 by itself
        except sc.MissingApiKey:
            raise
        except sc.StackError as err:
            if err.code == "model_locked":
                raise
            kind = "stop" if err.code in STOP_CODES else "retry" if err.code in RETRY_CODES else "fail"
            return sha, kind, {"code": err.code, "message": err.message}
        return sha, "ok", (result["embeddings"][0]["vector"], client.calls(tag)[-1])

    done, retry = [], []
    with ThreadPoolExecutor(workers) as pool:
        for sha, kind, value in pool.map(one, shas):
            if kind == "ok":
                done.append((sha, value[0]))
                report.rows.setdefault(sha, []).append(value[1])
            elif kind == "retry":
                retry.append(sha)
            elif kind == "stop":
                report.stopped = report.stopped or {**value, "model": model}
            else:
                report.failed[sha] = value
    return done, retry


def embed_chunk(client, index: LibraryIndex, model: str, paths: dict[str, Path], report: Report, *, workers=4) -> None:
    """Embed the images `paths` (sha256 -> file) with `model`, commit them, and record the outcome in `report`."""
    todo = list(paths)
    for attempt in range(ATTEMPTS):
        if not todo or report.stopped:
            break
        if client.free:
            done, retry = run_singles(client, model, paths, todo, report, workers)
        else:
            with ThreadPoolExecutor(workers) as pool:
                handles = list(pool.map(lambda sha: _upload(client, paths[sha]), todo))  # free of charge; also re-uploads evicted images
            done, retry = run_batch(client, model, todo, handles, attempt, report)
        if done:
            index.add(model, [s for s, _ in done], [v for _, v in done])
            index.commit()
            report.embedded += len(done)
        todo = retry
    for sha in [] if report.stopped else todo:
        report.failed.setdefault(sha, {"code": "gave_up", "message": f"not embedded after {ATTEMPTS} attempts"})


# endregion


def cost_line(client) -> str:
    """What the calls of this run cost at list price, and what was charged (nothing on the free tier)."""
    s = client.summary()
    if client.free:
        return f"list price {s['listTotal']:g} credits (free tier, not charged), in {s['calls']} calls"
    return f"cost: {s['listTotal']:g} credits at list price, {s['chargedTotal']:g} charged, in {s['calls']} calls"


def check_free_size(client, images: int) -> None:
    """Free mode is for trying the recipe: a library of more than FREE_MAX_IMAGES images needs an API key (exit code 2)."""
    if client.free and images > FREE_MAX_IMAGES:
        client.require_key(f"the free tier is meant for trying a recipe on a small library of up to {FREE_MAX_IMAGES} images, and this one has {images}")


# region index-library
def index_library(client, index: LibraryIndex, root: Path, models=("dinov3", "siglip2"), *, batch_size=50, workers=4, log=print) -> Report:
    """Embed every library image that is missing from the index, with every model, a chunk at a time."""
    check_free_size(client, len(index.items))
    report = Report()
    paths = {it["sha256"]: root / it["path"] for it in index.items}
    for model in models:
        pending = index.missing(model)  # sha256 of the images that lack this model's vector, in catalogue order
        log(f"{len(index.items)} images in the library, {len(pending)} to embed with {model}")
        for start in range(0, len(pending), batch_size):
            if report.stopped:
                return report
            chunk = {sha: paths[sha] for sha in pending[start : start + batch_size]}
            embed_chunk(client, index, model, chunk, report, workers=workers)
            log(f"  {min(start + batch_size, len(pending))}/{len(pending)}")
    return report


# endregion


def embed_text(client, text: str, model="siglip2", tag=None) -> tuple[list[float], dict]:
    """One query text, one call (up to 64 texts fit in one call; one per call keeps the cost per query)."""
    result = client.post("/v1/embeddings", {"model": model, "texts": [text]}, tag=tag)
    return result["embeddings"][0]["vector"], client.calls(tag)[-1]


def embed_image(client, path: Path, model="dinov3", tag=None) -> tuple[list[float], dict]:
    """One image that is not in the library (a query by example): inline base64 on the free tier, an uploaded handle with a key."""
    image = sc.image_base64(path.read_bytes()) if client.free else sc.image_handle(_upload(client, path))
    result = client.post("/v1/embeddings", {"model": model, "image": image}, tag=tag)
    return result["embeddings"][0]["vector"], client.calls(tag)[-1]
