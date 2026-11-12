"""Scan, upload and batch execution: the part that makes a long run resumable and safe to repeat.

A run is a list of units (one image plus its prompts). Units are cut into batches of at most `--batch-size` and sent
as `POST /v1/batch/segment` event streams. Every step can be repeated without paying twice:

* uploads are free and the handle is `img_` + the sha256 of the image, so uploading again returns the same handle;
* a batch has an idempotency key derived from its round, its index and the sha256 of its request body, so sending
  the same batch again replays the items that finished (`replayed: true`, no charge) and runs the rest;
* items that came back `expired`, or failed with a retryable error, go into a new batch in the next round;
* every finished item is written to `<out>/raw/<id>.json`, so `--resume` does not even ask the API again.
"""

import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import stack_client as sc

CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
RETRYABLE_ITEM_ERRORS = {"model_busy", "model_unavailable", "upstream_error", "upstream_timeout", "internal_error", "unavailable", "rate_limited", "no_result"}
STREAM_ATTEMPTS = 3
MAX_PROMPTS = 50  # per segment item


@dataclass
class Unit:
    """One image to segment: the manifest entry, its file, and what the API needs and returns for it."""

    item: dict
    path: Path
    sha256: str = ""
    size: int = 0
    handle: str = ""
    width: int = 0
    height: int = 0
    prompts: list = field(default_factory=list)  # API prompt objects
    labels: list = field(default_factory=list)  # class name per prompt, same order
    body: dict = field(default_factory=dict)  # the SegmentItem sent in the batch
    digest: str = ""  # sha256 of `body`: identifies the request in checkpoints and batch keys

    @property
    def id(self):
        return self.item["id"]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# region scan
def scan(items, base):
    """Read every image once: sha256 (checked against the manifest when it has one), size and content type."""
    units = []
    for item in items:
        path = base / item["image"]
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if item.get("sha256") and item["sha256"] != digest:
            raise ValueError(f"{item['id']}: sha256 of {item['image']} does not match the manifest")
        units.append(Unit(item, path, digest, len(data)))
    return units


# endregion


# region upload
def upload_all(client, units, workers):
    """Upload every distinct file once (free). The handle is `img_<sha256>`, so repeated files share one upload."""
    by_sha = {}
    for u in units:
        by_sha.setdefault(u.sha256, []).append(u)

    def one(group):
        first = group[0]
        info = client.upload(first.path.read_bytes(), CONTENT_TYPES.get(first.path.suffix.lower(), "image/jpeg"))
        for u in group:
            u.handle, u.width, u.height = info["id"], info["width"], info["height"]

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, by_sha.values()))


# endregion


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# region batch-key
def batch_key(round_no, index, body):
    """Idempotency key of a batch: round, index and the sha256 of the request body (which holds image sha256 handles,
    prompts and options). The same manifest and options give the same keys, so a repeated run replays finished items."""
    digest = hashlib.sha256(canonical(body).encode()).hexdigest()
    return f"prelabel-r{round_no}-{index:04d}-{digest[:32]}"


# endregion


def local_error(code, message, status=0):
    return {"status": status, "code": code, "message": message}


def run_batch(client, model, units, key, tag, on_ok):
    """Send one batch and collect an outcome per unit. A stream that ends without its `summary` event was cut: the same
    body is sent again under the same key, which replays what finished and runs the rest."""
    body = {"model": model, "items": [u.body for u in units]}
    out = {u.id: {"status": "no_result", "result": None, "error": local_error("no_result", "the stream ended before this item"), "calls": []} for u in units}
    # region batch-run
    for attempt in range(1, STREAM_ATTEMPTS + 1):
        summary = None
        try:
            for event, data in client.batch("segment", body, key, tag=tag):
                if event == "progress":
                    log(f"  batch {tag}: {data['done']} done, {data['remaining']} remaining")
                elif event == "summary":
                    summary = data
                elif event == "item":
                    unit = units[data["index"]]
                    cur = out[unit.id]
                    if data["status"] == "ok":
                        row = client.calls(tag)[-1]  # the client records the item's ledger row just before it yields the event
                        cur["calls"].append(row)
                        if cur["result"] is None:  # a replay after a cut stream repeats items we already hold
                            cur.update(status="ok", result=data["result"], error=None)
                            on_ok(unit, cur)  # checkpoint the item now, not at the end of the batch
                    elif cur["result"] is None:
                        if (data.get("error") or {}).get("code") == "insufficient_balance":
                            raise sc.StackError(402, "insufficient_balance", data["error"]["message"])
                        err = data.get("error") or {"code": "expired", "message": "the batch time limit passed before this item finished"}
                        cur.update(status=data["status"], error=local_error(err["code"], err["message"]))
        except sc.StackError as err:
            if err.code in ("model_locked", "budget_exceeded", "insufficient_balance") or not (err.retryable or err.code == "batch_timeout"):
                raise
            log(f"  batch {tag}: {err.code}, attempt {attempt} of {STREAM_ATTEMPTS}")
        if summary is not None:
            break
        time.sleep(min(2**attempt, 10))
    # endregion
    return out


def retryable(outcome):
    return (
        outcome["status"] == "expired"
        or outcome["status"] == "no_result"
        or (outcome["status"] == "error" and outcome["error"]["code"] in RETRYABLE_ITEM_ERRORS)
    )


def request_digest(unit):
    return hashlib.sha256(canonical(unit.body).encode()).hexdigest()


def load_checkpoint(raw_dir, unit):
    """A finished item of an earlier run, when its request is unchanged. Its calls are reported as replayed."""
    path = raw_dir / f"{unit.id}.json"
    if not path.is_file():
        return None
    saved = json.loads(path.read_text())
    if saved.get("digest") != unit.digest:
        return None
    calls = [dict(c, charged=0, replayed=True) for c in saved["calls"]]
    return {"status": "ok", "result": saved["result"], "error": None, "calls": calls}


def save_checkpoint(raw_dir, unit, outcome):
    """Write the finished item atomically; the balance is account data and is not kept."""
    result = {k: v for k, v in outcome["result"].items() if k != "balance"}
    path = raw_dir / f"{unit.id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"digest": unit.digest, "result": result, "calls": outcome["calls"]}))
    tmp.replace(path)


def run_round(client, cfg, pending, round_no, raw_dir):
    """Send `pending` as batches, up to `cfg.concurrency` at a time; returns the outcome of every unit."""
    batches = [pending[i : i + cfg.batch_size] for i in range(0, len(pending), cfg.batch_size)]
    log(f"round {round_no}: {len(pending)} items in {len(batches)} batches (up to {cfg.concurrency} at once)")

    def send(args):
        index, batch = args
        key = batch_key(round_no, index, {"model": cfg.model, "items": [u.body for u in batch]})
        return run_batch(client, cfg.model, batch, key, f"r{round_no}b{index}", lambda u, o: save_checkpoint(raw_dir, u, o))

    outcomes = {}
    with ThreadPoolExecutor(cfg.concurrency) as pool:
        for batch_outcomes in pool.map(send, enumerate(batches)):
            outcomes.update(batch_outcomes)
    return outcomes


# region requeue
def run_all(client, units, cfg, raw_dir):
    """Run every unit to an outcome: checkpoint (with --resume), then batches, then new rounds for what expired or failed."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    outcomes = {}
    if cfg.resume:
        for u in units:
            done = load_checkpoint(raw_dir, u)
            if done:
                outcomes[u.id] = done
        log(f"resume: {len(outcomes)} of {len(units)} items are already finished")
    pending = [u for u in units if u.id not in outcomes]
    for round_no in range(1, cfg.rounds + 1):
        if not pending:
            break
        outcomes.update(run_round(client, cfg, pending, round_no, raw_dir))
        pending = [u for u in pending if retryable(outcomes[u.id])]  # expired or failed with a retryable code: next round
        if pending and round_no < cfg.rounds:
            time.sleep(min(cfg.retry_wait * round_no, 30))
    return outcomes


# endregion
