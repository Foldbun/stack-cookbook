"""Click simulator of use case 02: start a backend (contract/02-backend.yaml), segment every object with simulated clicks.

The standard interactive-segmentation protocol: the first click is the point inside the ground-truth mask farthest
from its boundary; every later click is the point farthest from the boundary of the largest connected error region,
positive when that region was missed and negative when it is excess. It stops at IoU >= 0.90 or after `maxClicks`
(a manifest item parameter: smoke 4, full 10). Against the real API it clicks at most once per second; against the
fake API it does not wait.

`run()` writes the standard result.json (calls[] and ledger come from the backend's `GET /api/usage`, i.e. its real
ledger) and summary.json with the metrics of `summarise()`: NoC@85, NoC@90, mean IoU after k clicks, mean cost per
object and the cache hit rate. Network timing is deliberately not measured: it depends on the reader's path to the API.
"""

import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

THRESHOLDS = {"noc85": 0.85, "noc90": 0.90}
STOP_IOU = 0.90
LIVE_INTERVAL_S = 1.0
READY_TIMEOUT_S = 180
EXIT_TIER, EXIT_BUDGET = 3, 4
CONTENT_TYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


# ---- geometry ----
def deepest_point(mask):
    """(x, y) of the pixel farthest from the region's boundary; the image border counts as boundary.

    Erodes alternately with the 4- and the 8-neighbourhood (an octagonal distance, close to Euclidean) until nothing
    is left; of the last non-empty layer, the pixel nearest to its centroid wins, then the smallest (y, x).
    """
    layer = np.pad(mask.astype(bool), 1)
    k = 0
    while True:
        c = layer[1:-1, 1:-1]
        e = c & layer[:-2, 1:-1] & layer[2:, 1:-1] & layer[1:-1, :-2] & layer[1:-1, 2:]
        if k % 2:
            e &= layer[:-2, :-2] & layer[:-2, 2:] & layer[2:, :-2] & layer[2:, 2:]
        if not e.any():
            break
        layer, k = np.pad(e, 1), k + 1
    ys, xs = np.nonzero(layer[1:-1, 1:-1])
    d = (ys - ys.mean()) ** 2 + (xs - xs.mean()) ** 2
    i = np.lexsort((xs, ys, d))[0]
    return int(xs[i]), int(ys[i])


def components(mask):
    """Connected regions (4-neighbourhood) of a boolean mask, as lists of flat indices."""
    h, w = mask.shape
    todo = bytearray(mask.astype(np.uint8).ravel().tobytes())
    out = []
    for start in np.flatnonzero(mask).tolist():
        if not todo[start]:
            continue
        todo[start] = 0
        region, queue = [], deque([start])
        while queue:
            i = queue.popleft()
            region.append(i)
            x = i % w
            for j in (i - w, i + w, i - 1 if x else -1, i + 1 if x < w - 1 else -1):
                if 0 <= j < h * w and todo[j]:
                    todo[j] = 0
                    queue.append(j)
        out.append(region)
    return out


def next_click(truth, pred, first):
    """(x, y, positive) of the next simulated click, or None when the prediction is already exact."""
    if first:
        return (*deepest_point(truth), True)
    missed, excess = truth & ~pred, pred & ~truth
    regions = [(len(r), 0, min(r), r, True) for r in components(missed)] + [(len(r), 1, min(r), r, False) for r in components(excess)]
    if not regions:
        return None
    size, _, _, region, positive = min(regions, key=lambda t: (-t[0], t[1], t[2]))
    m = np.zeros(truth.size, dtype=bool)
    m[region] = True
    return (*deepest_point(m.reshape(truth.shape)), positive)


def rasterise(polygon, width, height):
    """Boolean mask of a Stack polygon (parts with outer rings and holes; vertices are pixel centres)."""
    img = Image.new("1", (width, height), 0)
    draw = ImageDraw.Draw(img)
    for part in polygon:
        o = part["outer"]
        draw.polygon(list(zip(o[0::2], o[1::2])), fill=1, outline=1)
    for part in polygon:
        for hole in part.get("holes", []):
            draw.polygon(list(zip(hole[0::2], hole[1::2])), fill=0, outline=1)  # ring vertices are object pixels
    return np.array(img, dtype=bool)


def iou(a, b):
    union = np.count_nonzero(a | b)
    return np.count_nonzero(a & b) / union if union else 1.0


# ---- HTTP ----
def http(method, url, body=None, headers=None, timeout=120):
    """(status, parsed JSON body, headers); never raises for HTTP errors."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null"), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), e.headers
        except ValueError:
            return e.code, {"error": {"code": "invalid_response", "message": raw[:200].decode(errors="replace")}}, e.headers


def call(base, method, path, session, body=None, ctype="application/json", tries=4):
    """One backend request; waits and repeats on `retryable` errors (429 rate_limited, 503 upstream_busy)."""
    headers = {"X-Session-Id": session}
    if body is not None:
        headers["Content-Type"] = ctype
        if ctype == "application/json":
            body = json.dumps(body).encode()
    for attempt in range(tries):
        status, doc, _ = http(method, base + path, body, headers)
        err = (doc or {}).get("error") if status >= 400 else None
        if not err or not err.get("retryable") or attempt == tries - 1:
            return status, doc
        time.sleep(min(60.0, max(1.0, err.get("retryAfterMs", 0) / 1000)))
    return status, doc


# ---- one object ----
def session_id(item_id):
    return ("cb-" + re.sub(r"[^A-Za-z0-9_-]", "_", item_id))[:64]


def segment(base, item, manifest_dir, truth, pace):
    """Segment one manifest item. Returns (result.json item, exit code or 0)."""
    sid, exit_code = session_id(item["id"]), 0
    img = manifest_dir / item["image"]
    entry = truth["items"][item["id"]]
    out = {"id": item["id"], "status": "ok", "calls": [], "output": None}

    def fail(status, doc):
        nonlocal exit_code
        err = (doc or {}).get("error") or {}
        out.update(status="error", output=None, error={"status": status, "code": err.get("code", "invalid_response"), "message": err.get("message", "")})
        if err.get("code") == "upstream_refused" and err.get("upstreamCode") == "model_locked":
            exit_code = EXIT_TIER
        elif err.get("code") == "credit_cap":
            exit_code = EXIT_BUDGET

    status, up = call(base, "POST", "/api/images", sid, img.read_bytes(), CONTENT_TYPE[img.suffix.lower()])
    if status != 200:
        fail(status, up)
    else:
        w, h = up["width"], up["height"]
        gt = np.array(Image.open(manifest_dir / entry["mask"]).convert("L")) > 127
        if gt.shape != (h, w):
            out.update(status="error", error={"status": 0, "code": "size_mismatch", "message": f"mask is {gt.shape[1]}x{gt.shape[0]}, image {w}x{h}"})
        else:
            limit = int(item.get("maxClicks", 10))
            pred, points, clicks, last = np.zeros_like(gt), [], [], 0.0
            for k in range(limit):
                nxt = next_click(gt, pred, first=k == 0)
                if nxt is None:
                    break
                x, y, positive = nxt
                points.append({"x": x, "y": y, "positive": positive})
                if pace:
                    time.sleep(max(0.0, last + LIVE_INTERVAL_S - time.monotonic()))
                last = time.monotonic()
                status, r = call(base, "POST", "/api/refine", sid, {"imageId": up["imageId"], "points": points})
                if status != 200:
                    fail(status, r)
                    break
                pred = rasterise(r["polygon"], w, h)
                clicks.append(
                    {
                        "x": x,
                        "y": y,
                        "positive": positive,
                        "iou": round(iou(pred, gt), 4),
                        "score": r["score"],
                        "cache": r["cache"],
                        "costCredits": r["costCredits"],
                        "listCredits": r["listCredits"],
                    }
                )
                if clicks[-1]["iou"] >= STOP_IOU:
                    break
            if out["status"] == "ok":
                if not clicks:
                    out.update(status="error", error={"status": 0, "code": "no_clicks", "message": "the truth mask is empty"})
                else:
                    noc = {name: next((i + 1 for i, c in enumerate(clicks) if c["iou"] >= t), None) for name, t in THRESHOLDS.items()}
                    out["output"] = {
                        "label": item.get("label", entry.get("label", "")),
                        "imageId": up["imageId"],
                        "width": w,
                        "height": h,
                        "maxClicks": limit,
                        "clicks": clicks,
                        **noc,
                        "finalIou": clicks[-1]["iou"],
                    }
    status, usage = call(base, "GET", "/api/usage", sid)
    if status == 200:
        out["calls"] = usage["calls"]
    elif out["status"] == "ok":
        fail(status, usage)
    return out, exit_code


# ---- metrics ----
ENCODE_FEE = 0.2  # credits per image for sam-lite, from the public price list; waived on a cache hit


def summarise(result):
    """Flat metrics of a result.json of use case 02 (also printed by its evaluate.py)."""
    outs = [it["output"] for it in result["items"] if it["status"] == "ok"]
    m = {"objects": len(result["items"]), "objects_ok": len(outs)}
    if not outs:
        return m
    k_max = max(o["maxClicks"] for o in outs)
    for name in THRESHOLDS:
        # NoC: an object that never reached the threshold counts as maxClicks (the usual convention)
        m[f"{name}_mean"] = round(sum(o[name] if o[name] is not None else o["maxClicks"] for o in outs) / len(outs), 3)
        m[f"{name}_reached"] = round(sum(o[name] is not None for o in outs) / len(outs), 3)
    for k in range(1, k_max + 1):
        # after a stop (IoU >= 0.90) the last IoU carries forward
        m[f"miou_at_{k}"] = round(sum(o["clicks"][min(k, len(o["clicks"])) - 1]["iou"] for o in outs) / len(outs), 4)
    clicks = [c for o in outs for c in o["clicks"]]
    m.update(
        clicks_per_object=round(len(clicks) / len(outs), 3),
        list_credits_per_object=round(sum(c["listCredits"] for c in clicks) / len(outs), 6),
        # the planning price: the image encoding fee on every click, also where this run hit the encoding cache
        planning_credits_per_object=round(sum(c["listCredits"] + (ENCODE_FEE if c["cache"] == "hit" else 0) for c in clicks) / len(outs), 6),
        charged_credits_per_object=round(sum(c["costCredits"] for c in clicks) / len(outs), 6),
        cache_hit_rate=round(sum(c["cache"] == "hit" for c in clicks) / len(clicks), 4),
    )
    return m


# ---- the backend process ----
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop(proc):
    """Stop the backend: SIGTERM to the process group we started (wrappers such as `uv run` or `go run` included)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


def wait_ready(base, proc, timeout=READY_TIMEOUT_S):
    """The /api/health body once the backend answers; None when it exited or did not come up in time."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            return None
        try:
            status, doc, _ = http("GET", base + "/api/health", timeout=2)
            if status == 200:
                return doc
        except OSError:
            pass
        time.sleep(0.1)
    return None


def run(cmd, cwd, env, manifest, out, usecase, language, live=False):
    """Start `cmd --port N`, drive it over every manifest item, write out/result.json and out/summary.json, stop it.

    Returns (exit code, output tail) like an implementation run: 0 when result.json was written, 2 / 3 / 4 as in the
    command-line contract, otherwise the backend's exit code or 1.
    """
    manifest, out = Path(manifest), Path(out)
    port = free_port()
    log_path = out / "backend.log"
    tail = lambda: log_path.read_text(errors="replace")[-3000:]
    with open(log_path, "w") as log:
        proc = subprocess.Popen(f"{cmd} --port {port}", shell=True, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        base = f"http://127.0.0.1:{port}"
        health = wait_ready(base, proc)
        if health is None:
            code = proc.poll()
            return (code if code else 1), "backend did not become ready:\n" + tail()
        doc = json.loads(manifest.read_text())
        truth = json.loads((manifest.parent / "truth.json").read_text())
        started, t0 = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), time.monotonic()
        items, code = [], 0
        for it in doc["items"]:
            item, code = segment(base, it, manifest.parent, truth, pace=live)
            items.append(item)
            if code:
                break
        rows = [c for it in items for c in it["calls"] if not c["replayed"]]
        result = {
            "usecase": usecase,
            "language": language,
            "client": health["client"],
            "startedAt": started,
            "elapsedMs": int((time.monotonic() - t0) * 1000),
            "items": items,
            "ledger": {
                "calls": sum(len(it["calls"]) for it in items),
                "listTotal": round(sum(c["list"] for c in rows), 6),
                "chargedTotal": round(sum(c["charged"] for c in rows), 6),
            },
        }
        (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        (out / "summary.json").write_text(json.dumps(summarise(result), indent=2) + "\n")
        return code, tail()
    finally:
        stop(proc)
