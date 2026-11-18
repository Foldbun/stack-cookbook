"""Local fake of the Foldbun Stack API, for developing and testing every implementation offline.

Run it with `uv run python -m harness fakeapi --port 8787` and point FOLDBUN_API_BASE at it. Two kinds of caller:
a request with `Authorization: Bearer fbs_...` is a funded account (every call is charged at list price; an
`X-Foldbun-Project` header names its source project), and a request with only `X-Foldbun-Project: fbp_...` is the
free tier: nothing is charged, images must be inline base64 (up to 4 MiB), and uploads, image handles, batches
and `Idempotency-Key` are refused; `GET /v1/models` works with a project ID alone. Free calls carry `X-RateLimit-*` headers; a scenario with
`"free": {"enforce": true}` also enforces the per-class limits (GCRA per project and per client address, daily caps).
Behaviour follows harness/vendor/openapi.yaml: prices and billing, the SAM encoding cache, idempotency with replay,
image handles with eviction, batch as Server-Sent Events or JSON, and the error envelope with `retryAfterMs` and
`Retry-After`. Inference results come from fixtures
(use-cases/*/fixtures/*.json) or are synthesised deterministically. Scripted scenarios inject errors, delays,
evictions, per-item batch failures, expiry and dropped streams. Test-only control paths live under `/_fake/`.
"""

import argparse
import base64
import hashlib
import json
import math
import re
import secrets
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# id, name, task, endpoints, license, prices
MODELS = [
    ("sam3", "SAM 3", "segment", ["/v1/segment", "/v1/refine"], "SAM License", {"encode_image": 0.5, "prompt": 0.05, "point": 0.02, "refine": 0.02}),
    ("sam-lite", "SAM 2.1 Hiera-S", "segment", ["/v1/segment", "/v1/refine"], "Apache-2.0", {"encode_image": 0.2, "point": 0.01, "box": 0.01, "refine": 0.01}),
    ("ppocr-v6", "PP-OCRv6 small", "ocr", ["/v1/ocr"], "Apache-2.0", {"image": 0.25}),
    ("paddleocr-vl", "PaddleOCR-VL", "parse", ["/v1/documents/parse"], "Apache-2.0", {"page": 2}),
    ("dinov3", "DINOv3", "embed", ["/v1/embeddings"], "DINOv3 License", {"image": 0.05}),
    ("siglip2", "SigLIP2", "embed", ["/v1/embeddings"], "Apache-2.0", {"image": 0.05, "text": 0.01}),
    ("birefnet", "BiRefNet", "remove-background", ["/v1/remove-background"], "MIT", {"image": 1}),
]
MODEL = {m[0]: m for m in MODELS}
# Free tier: weight class of each model and the limits of the Cookbook's project (requests per minute, burst, per day).
WEIGHT = {"siglip2": "light", "dinov3": "light", "sam-lite": "light", "sam3": "medium", "birefnet": "medium", "ppocr-v6": "medium", "paddleocr-vl": "heavy"}
FREE_LIMITS = {
    "light": {"rpm": 300, "burst": 3, "daily": 50000},
    "medium": {"rpm": 120, "burst": 2, "daily": 10000},
    "heavy": {"rpm": 20, "burst": 2, "daily": 1000},
}
FREE_MAX_IMAGE = 4 * 1024 * 1024  # decoded size of an inline image on the free tier
KEY_PROJECT = "fbp_0192f3c47a1e7b2d9c4f1a2b3c4d5e70"  # the project every fake API key belongs to
PROJECT_ID = re.compile(r"fbp_[A-Za-z0-9]{8,64}")
DEFAULT_MODEL = {"/v1/ocr": "ppocr-v6", "/v1/documents/parse": "paddleocr-vl", "/v1/remove-background": "birefnet", "/v1/refine": "sam3"}
INFERENCE = {"/v1/segment", "/v1/refine", "/v1/ocr", "/v1/documents/parse", "/v1/embeddings", "/v1/remove-background"}
BATCHABLE = {"segment", "ocr", "documents/parse", "embeddings", "remove-background"}
STATUS = {
    "invalid_request": 400,
    "invalid_image": 400,
    "unsupported": 400,
    "unauthorized": 401,
    "insufficient_balance": 402,
    "model_locked": 403,
    "free_tier_unavailable": 403,
    "not_found": 404,
    "image_not_found": 404,
    "async_unsupported": 400,
    "idempotency_conflict": 409,
    "job_finished": 409,
    "free_tier_unsupported": 403,
    "too_many_jobs": 429,
    "async_unavailable": 503,
    "request_in_progress": 409,
    "payload_too_large": 413,
    "rate_limited": 429,
    "daily_limit_reached": 429,
    "free_busy": 429,
    "too_many_batches": 429,
    "internal_error": 500,
    "upstream_error": 502,
    "bad_model_output": 502,
    "model_unavailable": 503,
    "model_busy": 503,
    "unavailable": 503,
    "upstream_timeout": 504,
}
RETRYABLE = {
    "rate_limited",
    "free_busy",
    "too_many_batches",
    "too_many_jobs",
    "async_unavailable",
    "upstream_error",
    "model_unavailable",
    "model_busy",
    "unavailable",
    "upstream_timeout",
}
# Async jobs (POST /v1/jobs): embedding models only, at a tenth of the list price.
ASYNC_FACTOR = 0.1
ASYNC_MODELS = ("siglip2", "dinov3")
JOB_MAX_ITEMS = 10000
JOB_PATH = re.compile(r"/v1/jobs/(job_[0-9a-f]{32})(/results|/cancel)?")
PROCESSING_MS = {"segment": 600, "ocr": 700, "parse": 3000, "embed": 80, "remove-background": 900}
EMBED_DIMS = 16  # the real models return longer vectors; implementations must read `dimensions`
MAX_BODY = 28 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, code, message, retry_after_ms=0, status=None, retryable=None, headers=None):
        super().__init__(message)
        self.code, self.message, self.retry_after_ms, self.headers = code, message, retry_after_ms, headers or {}
        self.status = status or STATUS.get(code, 400)
        self.retryable = code in RETRYABLE if retryable is None else retryable

    def body(self):
        return {"error": {"code": self.code, "message": self.message, "retryable": self.retryable, "retryAfterMs": self.retry_after_ms}}


def r6(x):
    return math.floor(x * 1e6 + 0.5) / 1e6


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def image_size(data):
    """(width, height, content type) of a PNG, JPEG or WebP file, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h, "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        kind = data[12:16]
        if kind == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF, "image/webp"
        if kind == b"VP8L":
            b = int.from_bytes(data[21:25], "little")
            return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1, "image/webp"
        if kind == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"), "image/webp"
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return w, h, "image/jpeg"
            i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    return None


def tiny_png(width, height):
    raw = b"".join(b"\x00" + b"\x00" * (4 * width) for _ in range(height))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def unit_vector(seed, dims=EMBED_DIMS):
    h = hashlib.sha256(seed.encode()).digest()
    v = [(h[i % 32] - 127.5) / 127.5 for i in range(dims)]
    n = math.sqrt(sum(x * x for x in v)) or 1
    return [round(x / n, 6) for x in v]


def load_fixtures(dirs):
    """Fixture files: JSON lists of {endpoint, model, image (sha256 of the file), request?, response, timing?, cache?, error?}."""
    table = {}
    for d in dirs:
        for f in sorted(Path(d).glob("*.json")):
            for fx in json.loads(f.read_text(encoding="utf-8")):
                req = canon(fx["request"]) if "request" in fx else "*"
                table[(fx["endpoint"], fx["model"], fx["image"], req)] = fx
    return table


class Fake:
    def __init__(self, fixtures=None, scenario=None, validate=False, upstream=None, record=None):
        self.fixtures = load_fixtures(fixtures or [])
        self.validate, self.upstream, self.record = validate, upstream, record
        self.lock = threading.Lock()
        self.request = threading.local()  # per request thread: `source` is the X-Foldbun-Project of a keyed call
        self.reset(scenario)

    def reset(self, scenario=None):
        with self.lock:
            sc = scenario or {}
            self.rules = [dict(r, _seen=0, _used=0) for r in sc.get("rules", [])]
            self.balance = {"funded": float(sc.get("balance", 1000))}
            free = sc.get("free") or {}
            self.free_enforce, self.ip_share = bool(free.get("enforce")), max(1, int(free.get("ipShare", 4)))
            self.free_limits = {c: dict(v, **(free.get("classes") or {}).get(c, {})) for c, v in FREE_LIMITS.items()}
            self.tat, self.free_used = {}, {}
            self.images, self.encoded, self.idem, self.running = {}, set(), {}, set()
            self.daily, self.open_batches, self.log, self.violations = {}, {}, [], []
            self.recorded, self.upstream_ids, self.calls = [], {}, 0
            self.jobs, self.job_idem, self.job_hold = {}, {}, {}
            self.jobs_lock = threading.RLock()  # serialises job operations; taken before `lock`, never inside it
            self.job_cfg = sc.get("jobs") or {}

    # ---- scenario rules ----
    def rule(self, method, path):
        with self.lock:
            for r in self.rules:
                p = r.get("path", "*")
                if (p.endswith("*") and path.startswith(p[:-1])) or p == path:
                    if r.get("method", method) != method:
                        continue
                    r["_seen"] += 1
                    if r["_seen"] <= r.get("skip", 0):
                        continue
                    if r.get("times", 1) and r["_used"] >= r.get("times", 1):
                        continue
                    r["_used"] += 1
                    return r
        return None

    # ---- accounts, free tier, billing ----
    @staticmethod
    def tier(who):
        """`who` is the API key of a funded account or the project ID of a free caller."""
        return "free" if who.startswith("fbp_") else "funded"

    def model_info(self, mid, who):
        """One entry of GET /v1/models. A key adds the account fields (retired pool fields included); a project ID alone gets none."""
        _, name, task, endpoints, lic, prices = MODEL[mid]
        with self.lock:
            free = {"class": WEIGHT[mid], **self.free_limits[WEIGHT[mid]]}
        info = {
            "id": mid,
            "name": name,
            "task": task,
            "endpoints": endpoints,
            "license": lic,
            "prices": [{"unit": u, "price": p} for u, p in prices.items()],
            "available": True,
            "free": free,
            "asyncPrices": [{"unit": u, "price": r6(p * ASYNC_FACTOR)} for u, p in prices.items()] if mid in ASYNC_MODELS else [],
        }
        if self.tier(who) == "funded":
            info.update(pool="paid", freeDailyCalls=None, perMinute=None, unlockedBy=None)
        return info

    def whoami(self, who):
        """GET /v1/whoami: the account behind a key, or the project of a free caller."""
        if self.tier(who) == "free":
            with self.lock:
                used = {c: self.free_used.get((who, c), 0) for c in FREE_LIMITS}
                limits = {c: dict(v) for c, v in self.free_limits.items()}
            tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
            return {
                "tier": "free",
                "project": {"id": who, "name": "Default project", "freeTier": "featured"},
                "limits": limits,
                "usedToday": used,
                "resetsAt": tomorrow,
            }
        with self.lock:
            balance = self.balance["funded"]
        d = hashlib.sha256(who.encode()).hexdigest()
        account = f"{d[:8]}-{d[8:12]}-7{d[13:16]}-8{d[17:20]}-{d[20:32]}"
        body = {
            "keyId": f"key_{account}",
            "accountId": account,
            "scopes": [],
            "balance": balance,
            "dailyAllowances": [],
            "tier": "paid",
            "project": {"id": KEY_PROJECT, "name": "Default project"},
        }
        if getattr(self.request, "source", None):
            body["sourceProject"] = {"id": self.request.source}
        return body

    def free_gate(self, project, ip, mid):
        """The limits of the free tier for one call: raises 429 `rate_limited` / `daily_limit_reached` when a scenario
        enforces them, and returns the X-RateLimit-* headers of the response."""
        cls = WEIGHT[mid]
        with self.lock:
            lim = self.free_limits[cls]
            now = time.monotonic()
            if self.free_enforce and self.free_used.get((project, cls), 0) >= lim["daily"]:
                midnight = (datetime.now(UTC) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                wait_ms = int((midnight - datetime.now(UTC)).total_seconds() * 1000)
                raise ApiError(
                    "daily_limit_reached",
                    "the daily limit of this project is used up",
                    wait_ms,
                    headers=self.rate_headers(lim["rpm"], 0, wait_ms / 1000, "project"),
                )
            scopes = [("project", (project, cls), lim["rpm"]), ("ip", (project, ip, cls), max(1, lim["rpm"] // self.ip_share))]
            seen = []
            for scope, k, rpm in scopes:
                t = 60 / rpm
                tat = max(self.tat.get(k, now), now)
                new = tat + t
                allow_in = new - lim["burst"] * t - now
                if self.free_enforce and allow_in > 0:
                    raise ApiError(
                        "rate_limited", f"too many requests for this {scope}", math.ceil(allow_in * 1000), headers=self.rate_headers(rpm, 0, allow_in, scope)
                    )
                if self.free_enforce:
                    self.tat[k] = new
                remaining = max(0, int((lim["burst"] * t - (new - now)) / t + 1e-9))
                seen.append((remaining, scope, rpm, 0 if remaining else new - now - (lim["burst"] - 1) * t))
            self.free_used[(project, cls)] = self.free_used.get((project, cls), 0) + 1
            remaining, scope, rpm, reset = min(seen, key=lambda x: x[0])
            return self.rate_headers(rpm, remaining, reset, scope)

    @staticmethod
    def rate_headers(limit, remaining, reset_s, scope):
        return {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(math.ceil(reset_s)),
            "X-RateLimit-Scope": scope,
        }

    def price_lines(self, path, body, mid, cache_hit, factor=1, cache_unit="encode_image"):
        prices, units = MODEL[mid][5], []
        if path in ("/v1/segment", "/v1/refine"):
            units.append(("encode_image", 1))
            if path == "/v1/refine":
                units.append(("refine", 1))
            else:
                ps = body["prompts"]
                texts, boxes, points = (sum(1 for p in ps if k in p) for k in ("text", "box", "point"))
                if mid == "sam-lite":
                    units += [("box", boxes), ("point", points)]
                else:
                    units += [("prompt", texts or (1 if boxes else 0)), ("point", points)]
        elif path == "/v1/embeddings":
            units.append(("text", len(body["texts"])) if "texts" in body else ("image", 1))
        else:
            units.append(("page" if path == "/v1/documents/parse" else "image", 1))
        lines = []
        for unit, q in units:
            if q:
                waived = unit == cache_unit and cache_hit
                lines.append(
                    {
                        "unit": unit,
                        "quantity": q,
                        "unitPrice": r6(prices[unit] * factor),
                        "amount": 0 if waived else r6(prices[unit] * factor * q),
                        "waived": waived,
                    }
                )
        return lines

    def bill(self, who, mid, lines, job=None):
        """Settle one call; returns (cost, billing) or raises 402. A free call is priced but not charged."""
        total = r6(sum(ln["amount"] for ln in lines))
        if self.tier(who) == "free":
            return 0, {"tier": "free", "project": who, "listPrice": total, "lines": lines, "total": total, "charged": False, "pool": None}
        with self.lock:
            others = sum(v for k, v in self.job_hold.items() if k != job)  # the hold of the job being charged is its own
            if self.balance["funded"] - others < total:
                raise ApiError("insufficient_balance", f"insufficient balance: this call can cost up to {total:g} credits")
            self.balance["funded"] = r6(self.balance["funded"] - total)
            source = getattr(self.request, "source", None)
            billing = {"tier": "paid", "project": KEY_PROJECT, "sourceProject": source, "lines": lines, "total": total, "charged": True, "pool": "paid"}
            return total, billing

    # ---- images ----
    def store_image(self, data):
        size = image_size(data)
        if not size:
            raise ApiError("invalid_request", "the image is not a JPEG, PNG or static WebP file")
        handle = "img_" + hashlib.sha256(data).hexdigest()
        with self.lock:
            new = handle not in self.images
            self.images[handle] = {"id": handle, "width": size[0], "height": size[1], "bytes": len(data), "contentType": size[2]}
        return self.images[handle], new

    def resolve(self, ref, free=False):
        if free and "base64" not in ref:
            raise ApiError("unsupported", "the free tier takes inline base64 images only: no image handles or URLs")
        if "url" in ref:
            raise ApiError("unsupported", "image.url is not available yet; upload the image or send base64")
        if "id" in ref:
            with self.lock:
                if ref["id"] not in self.images:
                    raise ApiError("image_not_found", f"image {ref['id']} not found; upload it again")
                return self.images[ref["id"]]
        b64 = ref["base64"].split(",", 1)[1] if ref["base64"].startswith("data:") else ref["base64"]
        try:
            data = base64.b64decode(b64, validate=True)
            if free and len(data) > FREE_MAX_IMAGE:
                raise ApiError("payload_too_large", "an inline image on the free tier can be 4 MiB at most")
            return self.store_image(data)[0]
        except ValueError as exc:
            raise ApiError("invalid_request", "image.base64 is not valid base64") from exc

    # ---- one inference call ----
    def infer(self, key, path, body, idem_key, rule=None):
        """Returns (status, response body, replayed)."""
        mid = body.get("model") or DEFAULT_MODEL.get(path)
        if mid not in MODEL or path not in MODEL[mid][3]:
            raise ApiError("invalid_request", f"model {mid} does not serve {path}")
        if path == "/v1/embeddings" and ("texts" in body) == ("image" in body):
            raise ApiError("invalid_request", "give exactly one of image and texts")
        if path == "/v1/embeddings" and "texts" in body and mid != "siglip2":
            raise ApiError("invalid_request", f"{mid} embeds images only")
        if rule and rule.get("error"):
            e = rule["error"]
            raise ApiError(e["code"], e.get("message", f"scripted {e['code']}"), e.get("retryAfterMs", 0), e.get("status"), e.get("retryable"))
        ik = (key, idem_key) if idem_key else None
        h = hashlib.sha256(canon({"p": path, "b": body}).encode()).hexdigest()
        if ik:
            with self.lock:
                if ik in self.running:
                    raise ApiError("request_in_progress", "a request with this Idempotency-Key is still running")
                if ik in self.idem:
                    stored_h, status, stored = self.idem[ik]
                    if stored_h != h:
                        raise ApiError("idempotency_conflict", "this Idempotency-Key was used with a different request body")
                    return status, stored, True
                self.running.add(ik)
        try:
            try:
                status, out = 200, self.run_call(key, path, body, mid, rule)
            except ApiError as err:
                if ik and err.status < 500 and err.status not in (402, 429):
                    with self.lock:
                        self.idem[ik] = (h, err.status, err.body())
                raise
            if ik:
                with self.lock:
                    self.idem[ik] = (h, status, out)
            return status, out, False
        finally:
            if ik:
                with self.lock:
                    self.running.discard(ik)

    def run_call(self, key, path, body, mid, rule, job=None):
        asyncjob = job is not None
        image = self.resolve(body["image"], self.tier(key) == "free") if "image" in body else None
        family = MODEL[mid][2] == "segment"
        cache_key = (key, mid, image["id"]) if image else None
        sha = image["id"][4:] if image else ""
        fx = self.fixtures.get((path, mid, sha, canon({k: v for k, v in body.items() if k not in ("image", "model")}))) or self.fixtures.get(
            (path, mid, sha, "*")
        )
        if fx and fx.get("error"):
            e = fx["error"]
            raise ApiError(e["code"], e.get("message", e["code"]), e.get("retryAfterMs", 0))
        track = bool(family or (asyncjob and image))  # SAM encodings, and the embeddings of async image items, are cached per image
        with self.lock:
            hit = bool(track and cache_key in self.encoded)
        if rule and rule.get("cache"):
            hit = rule["cache"] == "hit"
        lines = self.price_lines(path, body, mid, hit, ASYNC_FACTOR if asyncjob else 1, "image" if asyncjob else "encode_image")
        cost, billing = self.bill(key, mid, lines, job)
        if track:
            with self.lock:
                self.encoded.add(cache_key)
        out = self.synthesise(path, body, mid, image) if not fx else json.loads(json.dumps(fx["response"]))
        task = MODEL[mid][2]
        timing = {"queueMs": 4, "processingMs": PROCESSING_MS[task]}
        timing.update((fx or {}).get("timing", {}))
        timing.setdefault("inferenceMs", max(0, timing["processingMs"] - 40))
        timing.setdefault("imageFetchMs", 0)
        timing.setdefault("serverMs", timing["queueMs"] + timing["processingMs"] + 6)
        with self.lock:
            self.calls += 1
            n = self.calls
        meta = {"id": f"call_00000000-0000-7000-8000-{n:012d}", "model": mid}
        if image:
            meta["image"] = image
        if track:
            out["cache"] = "hit" if hit else "miss"
        if family:
            out["imageFee"] = next((ln["amount"] for ln in lines if ln["unit"] == "encode_image"), 0)
        meta.update(out)
        meta.update(cost=cost, timing=timing, billing=billing)
        if self.tier(key) == "funded":  # a free call has no balance
            with self.lock:
                meta["balance"] = self.balance["funded"]
        return meta

    @staticmethod
    def refine_box(points, w, h):
        """Refinement with several clicks: the box of the positive clicks grown by 10 % of the image on each side, then
        for each negative click inside it the nearest side moved past the click. Deterministic and responsive, so a
        click simulator sees its clicks change the answer (one click gives the same box as before)."""
        pos = [p for p in points if p.get("positive", True)] or points[:1]
        x1, y1 = min(p["x"] for p in pos) - w * 0.1, min(p["y"] for p in pos) - h * 0.1
        x2, y2 = max(p["x"] for p in pos) + w * 0.1, max(p["y"] for p in pos) + h * 0.1
        for p in points:
            if p.get("positive", True) or not (x1 <= p["x"] < x2 and y1 <= p["y"] < y2):
                continue
            side = min((p["x"] - x1, 0), (x2 - p["x"], 1), (p["y"] - y1, 2), (y2 - p["y"], 3))[1]
            cut = [(p["x"] + 1, y1, x2, y2), (x1, y1, p["x"], y2), (x1, p["y"] + 1, x2, y2), (x1, y1, x2, p["y"])][side]
            if cut[2] - cut[0] >= 2 and cut[3] - cut[1] >= 2:
                x1, y1, x2, y2 = cut
        return [x1, y1, x2, y2]

    def synthesise(self, path, body, mid, image):
        w, h = (image["width"], image["height"]) if image else (0, 0)
        clip = lambda b: [max(0, min(w, int(b[0]))), max(0, min(h, int(b[1]))), max(1, min(w, int(math.ceil(b[2])))), max(1, min(h, int(math.ceil(b[3]))))]  # noqa: E731
        if path == "/v1/ocr":
            box = clip([w * 0.1, h * 0.1, w * 0.5, h * 0.1 + 24])
            quad = [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
            line = {"text": "Sample text", "confidence": 0.95, "bbox": box, "quad": quad}
            if (body.get("options") or {}).get("words", True):
                line["words"] = [{"text": "Sample text", "confidence": 0.95, "bbox": box, "quad": quad}]
            return {"lines": [line]}
        if path == "/v1/documents/parse":
            blocks = [
                {"type": "title", "order": 0, "bbox": clip([w * 0.1, h * 0.05, w * 0.9, h * 0.12]), "text": "Sample page", "markdown": "# Sample page"},
                {"type": "text", "order": 1, "bbox": clip([w * 0.1, h * 0.15, w * 0.9, h * 0.5]), "text": "Sample paragraph.", "markdown": "Sample paragraph."},
            ]
            out = {"pages": 1, "markdown": "# Sample page\n\nSample paragraph."}
            if (body.get("options") or {}).get("blocks", True):
                out["blocks"] = blocks
            return out
        if path == "/v1/embeddings":
            seeds = [f"{mid}:text:{t}" for t in body["texts"]] if "texts" in body else [f"{mid}:{image['id']}"]
            return {"dimensions": EMBED_DIMS, "embeddings": [{"index": i, "vector": unit_vector(s)} for i, s in enumerate(seeds)]}
        if path == "/v1/remove-background":
            opts = body.get("options") or {}
            if opts.get("format", "png") != "png":
                raise ApiError("unsupported", "the fake API returns png only; add a fixture for webp output")
            scale = min(1.0, opts.get("size", 2048) / max(w, h))
            rw, rh = max(1, round(w * scale)), max(1, round(h * scale))
            return {"format": "png", "width": rw, "height": rh, "data": base64.b64encode(tiny_png(rw, rh)).decode()}
        # segment / refine: one rectangle instance per prompt
        instances = []
        prompts = body.get("prompts") or [{"box": body["box"]} if body.get("box") else {"point": (body.get("points") or [{"x": w / 2, "y": h / 2}])[0]}]
        for i, p in enumerate(prompts):
            if "box" in p and p.get("positive", True) is False:
                continue
            if path == "/v1/refine" and not body.get("box") and len(body.get("points") or []) > 1:
                bb = clip(self.refine_box(body["points"], w, h))
            elif "box" in p:
                bb = clip(p["box"])
            elif "point" in p:
                bb = clip([p["point"]["x"] - w * 0.1, p["point"]["y"] - h * 0.1, p["point"]["x"] + w * 0.1, p["point"]["y"] + h * 0.1])
            else:
                bb = clip([w * 0.25, h * 0.25, w * 0.75, h * 0.75])
            x1, y1, x2, y2 = bb
            score = round(0.5 + int(hashlib.sha256(canon([image["id"], p]).encode()).hexdigest()[:4], 16) / 0xFFFF * 0.49, 4)
            inst = {"score": score, "bbox": bb, "area": (x2 - x1) * (y2 - y1), "polygon": [{"outer": [x1, y1, x2 - 1, y1, x2 - 1, y2 - 1, x1, y2 - 1]}]}
            if path == "/v1/segment":
                inst["prompt"] = i
                if "text" in p:
                    inst["label"] = p["text"]
                elif "box" in p and mid == "sam3":
                    inst["label"] = "_exemplar"
                if p.get("group"):
                    inst["group"] = p["group"]
            instances.append(inst)
        out = {"instances": instances}
        if path == "/v1/segment":
            out["dropStats"] = []
        return out

    # ---- async jobs ----
    @staticmethod
    def stamp(t):
        return None if t is None else t.strftime("%Y-%m-%dT%H:%M:%SZ")

    def job_json(self, job):
        n = len(job["items"])
        count = lambda st: sum(1 for r in job["results"].values() if r["status"] == st)  # noqa: E731
        counts = {
            "total": n,
            "pending": n - len(job["results"]),
            "running": 0,
            "ok": count("ok"),
            "error": count("error"),
            "expired": count("expired"),
            "cancelled": count("cancelled"),
        }
        return {
            "id": job["id"],
            "object": "job",
            "endpoint": "embeddings",
            "model": job["model"],
            "status": job["status"],
            "counts": counts,
            "estimate": job["estimate"],
            "cost": r6(job["cost"]),
            "createdAt": self.stamp(job["created"]),
            "startedAt": self.stamp(job["started"]),
            "finishedAt": self.stamp(job["finished"]),
            "expiresAt": self.stamp(job["created"] + timedelta(hours=24)),
            "resultsExpireAt": self.stamp(job["finished"] + timedelta(days=7)) if job["finished"] else None,
            "metadata": job["metadata"],
        }

    def set_hold(self, jid, amount):
        with self.lock:
            if amount:
                self.job_hold[jid] = amount
            else:
                self.job_hold.pop(jid, None)

    def create_job(self, key, body, idem_key, rule):
        """Returns (job JSON, replayed). Scenario `jobs`: `polls` (polls that still see the job unfinished, default 1),
        `itemErrors` ({"index": code}), `expireFrom` (items from this index expire), `maxUnfinished`, `maxQueuedItems`."""
        if rule and rule.get("error"):
            e = rule["error"]
            raise ApiError(e["code"], e.get("message", f"scripted {e['code']}"), e.get("retryAfterMs", 0), e.get("status"), e.get("retryable"))
        model, items = body.get("model"), body.get("items")
        if body.get("endpoint") != "embeddings" or model not in ASYNC_MODELS:
            raise ApiError("async_unsupported", f"{body.get('endpoint')} with model {model} is not open for async jobs")
        if not isinstance(items, list) or not 1 <= len(items) <= JOB_MAX_ITEMS:
            raise ApiError("invalid_request", f"items: give 1 to {JOB_MAX_ITEMS} items")
        prices, est = MODEL[model][5], 0.0
        for i, it in enumerate(items):
            if not isinstance(it, dict) or ("texts" in it) == ("image" in it) or set(it) - {"texts", "image"}:
                raise ApiError("invalid_request", f"items[{i}]: give exactly one of image and texts, and no model")
            if "image" in it:
                ref = it["image"]
                if "id" not in ref:
                    raise ApiError("invalid_request", f"items[{i}]: async jobs take image handles only (upload the image first)")
                with self.lock:
                    known = ref["id"] in self.images
                if not known:
                    raise ApiError("image_not_found", f"items[{i}]: image {ref['id']} not found; upload it again", status=400)
                est += prices["image"] * ASYNC_FACTOR
            else:
                if model != "siglip2":
                    raise ApiError("invalid_request", f"items[{i}]: {model} embeds images only")
                est += prices["text"] * ASYNC_FACTOR * len(it["texts"])
        est = r6(est)
        h = hashlib.sha256(canon(body).encode()).hexdigest()
        with self.jobs_lock:
            ik = (key, idem_key) if idem_key else None
            if ik and ik in self.job_idem:
                stored_h, jid = self.job_idem[ik]
                if stored_h != h:
                    raise ApiError("idempotency_conflict", "this Idempotency-Key was used with a different request body")
                return self.job_json(self.jobs[jid]), True
            open_jobs = [j for j in self.jobs.values() if j["key"] == key and j["status"] in ("queued", "running")]
            queued = sum(len(j["items"]) - len(j["results"]) for j in open_jobs)
            if len(open_jobs) >= self.job_cfg.get("maxUnfinished", 10) or queued + len(items) > self.job_cfg.get("maxQueuedItems", 100000):
                raise ApiError("too_many_jobs", "too many unfinished jobs or queued items; retry when some finish", 30000)
            with self.lock:
                available = self.balance["funded"] - sum(self.job_hold.values())
            if available < est:
                raise ApiError("insufficient_balance", f"insufficient balance: this job can cost up to {est:g} credits")
            jid = "job_" + f"{len(self.jobs) + 1:032x}"
            job = {
                "id": jid,
                "key": key,
                "model": model,
                "items": items,
                "metadata": body.get("metadata"),
                "estimate": est,
                "created": datetime.now(UTC),
                "started": None,
                "finished": None,
                "results": {},
                "status": "queued",
                "polls": 0,
                "done": 0,
                "cost": 0.0,
            }
            self.jobs[jid] = job
            self.set_hold(jid, est)
            if ik:
                self.job_idem[ik] = (h, jid)
            return self.job_json(job), False

    def own_job(self, key, jid):
        job = self.jobs.get(jid)
        if not job or job["key"] != key:
            raise ApiError("not_found", f"job {jid} not found")
        return job

    def advance(self, job):
        """One poll: the first `polls` polls process a growing share of the items, the next one finishes the job."""
        if job["status"] not in ("queued", "running"):
            return
        steps = int(self.job_cfg.get("polls", 1))
        job["polls"] += 1
        n = len(job["items"])
        upto = n if job["polls"] > steps else max(1, n * job["polls"] // (steps + 1))
        item_errors = {int(k): v for k, v in (self.job_cfg.get("itemErrors") or {}).items()}
        expire_from = self.job_cfg.get("expireFrom")
        if job["started"] is None:
            job["started"] = datetime.now(UTC)
        job["status"] = "running"
        for i in range(job["done"], upto):
            if i in job["results"]:
                continue
            if expire_from is not None and i >= expire_from:
                job["results"][i] = {"index": i, "status": "expired"}
            elif i in item_errors:
                job["results"][i] = {"index": i, "status": "error", "error": {"code": item_errors[i], "message": f"scripted {item_errors[i]}"}}
            else:
                try:
                    out = self.run_call(job["key"], "/v1/embeddings", dict(job["items"][i], model=job["model"]), job["model"], None, job["id"])
                    job["results"][i] = {"index": i, "status": "ok", "result": out}
                    job["cost"] += out["cost"]
                except ApiError as err:
                    job["results"][i] = {"index": i, "status": "error", "error": {"code": err.code, "message": err.message}}
                    if err.code == "insufficient_balance":
                        job["stopped"] = True
        job["done"] = upto
        if job.get("stopped"):
            for i in range(n):
                job["results"].setdefault(
                    i, {"index": i, "status": "error", "error": {"code": "insufficient_balance", "message": "the balance no longer covers the job"}}
                )
        if len(job["results"]) == n:
            expired = any(r["status"] == "expired" for r in job["results"].values())
            job["status"] = "stopped" if job.get("stopped") else "expired" if expired else "completed"
            job["finished"] = datetime.now(UTC)
            self.set_hold(job["id"], 0)

    def get_job(self, key, jid):
        with self.jobs_lock:
            job = self.own_job(key, jid)
            self.advance(job)
            return self.job_json(job)

    def cancel_job(self, key, jid):
        with self.jobs_lock:
            job = self.own_job(key, jid)
            if job["status"] not in ("queued", "running"):
                raise ApiError("job_finished", f"job {jid} has already ended")
            for i in range(len(job["items"])):
                job["results"].setdefault(i, {"index": i, "status": "cancelled"})
            job["status"], job["finished"] = "cancelled", datetime.now(UTC)
            self.set_hold(jid, 0)
            return self.job_json(job)

    def job_results(self, key, jid, cursor, limit):
        with self.jobs_lock:
            job = self.own_job(key, jid)
            try:
                start = int(cursor) if cursor else 0
                if start < 0 or not 1 <= limit <= 1000:
                    raise ValueError
            except ValueError as exc:
                raise ApiError("invalid_request", "invalid cursor or limit") from exc
            done = [job["results"][i] for i in sorted(job["results"]) if i >= start][:limit]
            nxt = str(done[-1]["index"] + 1) if len(done) == limit else None
            return {"items": json.loads(json.dumps(done)), "next": nxt}

    # ---- batch ----
    def batch_items(self, key, endpoint, body, idem_key, rule):
        """Yields BatchItem dicts, one per item, in order."""
        single = "/v1/" + endpoint
        expire_from = (rule or {}).get("expireFrom")
        item_errors = {int(k): v for k, v in ((rule or {}).get("itemErrors") or {}).items()}
        for i, item in enumerate(body["items"]):
            if expire_from is not None and i >= expire_from:
                yield {"index": i, "status": "expired"}
                continue
            full = dict(item)
            if body.get("model"):
                full["model"] = body["model"]
            try:
                if i in item_errors:
                    raise ApiError(item_errors[i], f"scripted {item_errors[i]}")
                _, out, replayed = self.infer(key, single, full, f"b {idem_key} {i}" if idem_key else None)
                if "error" in out:
                    yield {"index": i, "status": "error", "error": {"code": out["error"]["code"], "message": out["error"]["message"]}, "replayed": True}
                    continue
                entry = {"index": i, "status": "ok", "result": out}
                if replayed:
                    entry["replayed"] = True
                yield entry
            except ApiError as err:
                yield {"index": i, "status": "error", "error": {"code": err.code, "message": err.message}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fakeapi"
    sys_version = ""

    def log_message(self, *args):
        pass

    @property
    def fake(self) -> Fake:
        return self.server.fake

    def send_json(self, status, body, extra=None):
        data = json.dumps(body).encode()
        self.status = status
        self.record()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-Id", secrets.token_hex(8))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_error_body(self, err: ApiError):
        extra = dict(err.headers)
        if err.status in (429, 503):
            extra["Retry-After"] = str(max(1, math.ceil(err.retry_after_ms / 1000)))
        self.check_response(err.status, err.body())
        self.send_json(err.status, err.body(), extra)

    def check_response(self, status, body):
        if self.fake.validate:
            from harness import openapi

            path = self.path.split("?")[0]
            for e in openapi.response_errors(path, status, body):
                with self.fake.lock:
                    self.fake.violations.append(f"response {self.command} {self.path} {status}: {e}")

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def handle_any(self, method):
        path = self.path.split("?")[0]
        self.status, replayed, self.free_request = 0, False, False
        self.logged, self.arrived, self.replayed, self.method, self.req_path = False, time.monotonic(), False, method, path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path.startswith("/_fake/"):
            return self.control(method, path, raw)
        if self.fake.upstream:
            return self.proxy(method, path, raw)
        rule = self.fake.rule(method, path)
        try:
            if rule and rule.get("delayMs"):
                time.sleep(rule["delayMs"] / 1000)
            if rule and rule.get("evictImages"):
                with self.fake.lock:
                    self.fake.images.clear()
                    self.fake.encoded.clear()
            auth, project = self.headers.get("Authorization", ""), self.headers.get("X-Foldbun-Project")
            key = auth[7:] if auth.startswith("Bearer fbs_") else None
            if (auth and not key) or (project is not None and not PROJECT_ID.fullmatch(project)):
                raise ApiError("unauthorized", "invalid API key or project ID")
            if not key and not project:
                raise ApiError("unauthorized", "missing API key or project ID")
            who = key or project
            self.fake.request.source = project if key else None
            self.free_request = not key
            if self.free_request and self.headers.get("Idempotency-Key") is not None:
                raise ApiError("unsupported", "the free tier does not take an Idempotency-Key")
            if method == "GET" and path == "/v1/models":
                if rule and rule.get("error"):
                    raise ApiError(rule["error"]["code"], "scripted", rule["error"].get("retryAfterMs", 0))
                body = {"data": [self.fake.model_info(m[0], who) for m in MODELS]}
                self.check_response(200, body)
                return self.send_json(200, body)
            if method == "GET" and path == "/v1/whoami":
                body = self.fake.whoami(who)
                self.check_response(200, body)
                return self.send_json(200, body)
            if method == "POST" and path == "/v1/images":
                if self.free_request:
                    raise ApiError("unsupported", "the free tier takes inline base64 images only: no uploads")
                if rule and rule.get("error"):
                    raise ApiError(rule["error"]["code"], "scripted", rule["error"].get("retryAfterMs", 0))
                ctype = self.headers.get("Content-Type", "")
                if ctype.startswith("multipart/form-data"):
                    raise ApiError("unsupported", "the fake API accepts the raw-body upload form only")
                if len(raw) > 20 * 1024 * 1024:
                    raise ApiError("payload_too_large", "the image exceeds 20 MiB")
                info, new = self.fake.store_image(raw)
                self.check_response(201 if new else 200, info)
                return self.send_json(201 if new else 200, info)
            if path == "/v1/jobs" or JOB_PATH.fullmatch(path):
                return self.jobs_route(method, path, key, raw, rule)
            if method != "POST" or not (path in INFERENCE or path[len("/v1/batch/") :] in BATCHABLE and path.startswith("/v1/batch/")):
                raise ApiError("not_found", f"no such endpoint: {method} {path}")
            if len(raw) > MAX_BODY:
                raise ApiError("payload_too_large", "the request body exceeds 28 MiB")
            try:
                body = json.loads(raw)
            except ValueError as exc:
                raise ApiError("invalid_request", "the body is not valid JSON") from exc
            if self.fake.validate:
                from harness import openapi

                errs = openapi.request_errors(path, body)
                if errs:
                    with self.fake.lock:
                        self.fake.violations.append(f"request {path}: {errs[0]}")
                    raise ApiError("invalid_request", f"invalid request: {errs[0]}")
            idem = self.headers.get("Idempotency-Key")
            if idem is not None and not re.fullmatch(r"[\x21-\x7e]{1,128}", idem):
                raise ApiError("invalid_request", "Idempotency-Key must be 1 to 128 printable ASCII characters")
            if path.startswith("/v1/batch/"):
                if self.free_request:
                    raise ApiError("unsupported", "the free tier does not have batch requests")
                return self.batch(key, path, body, idem, rule)
            extra = {}
            mid = body.get("model") or DEFAULT_MODEL.get(path)
            if self.free_request and mid in MODEL and path in MODEL[mid][3] and not (rule and rule.get("error")):
                extra.update(self.fake.free_gate(project, self.client_address[0], mid))
            if rule and rule.get("responseHeaders"):
                extra.update(rule["responseHeaders"])
            status, out, replayed = self.fake.infer(who, path, body, idem, rule)
            self.replayed = replayed
            if replayed:
                extra["Idempotent-Replayed"] = "true"
            self.check_response(status, out)
            self.send_json(status, out, extra)
        except ApiError as err:
            self.send_error_body(err)
        finally:
            self.record()

    def jobs_route(self, method, path, key, raw, rule):
        if self.free_request:
            raise ApiError("free_tier_unsupported", "async jobs need an API key; the free tier does not offer them")
        extra = {}
        m = JOB_PATH.fullmatch(path)
        if path == "/v1/jobs" and method == "POST":
            try:
                body = json.loads(raw)
            except ValueError as exc:
                raise ApiError("invalid_request", "the body is not valid JSON") from exc
            if self.fake.validate:
                from harness import openapi

                errs = openapi.request_errors(path, body)
                if errs:
                    with self.fake.lock:
                        self.fake.violations.append(f"request {path}: {errs[0]}")
                    raise ApiError("invalid_request", f"invalid request: {errs[0]}")
            idem = self.headers.get("Idempotency-Key")
            if idem is not None and not re.fullmatch(r"[\x21-\x7e]{1,128}", idem):
                raise ApiError("invalid_request", "Idempotency-Key must be 1 to 128 printable ASCII characters")
            out, replayed = self.fake.create_job(key, body, idem, rule)
            self.replayed = replayed
            extra["Location"] = f"/v1/jobs/{out['id']}"
            if replayed:
                extra["Idempotent-Replayed"] = "true"
            status = 202
        elif m and not m.group(2) and method == "GET":
            out, status = self.fake.get_job(key, m.group(1)), 200
        elif m and m.group(2) == "/cancel" and method == "POST":
            out, status = self.fake.cancel_job(key, m.group(1)), 200
        elif m and m.group(2) == "/results" and method == "GET":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            out, status = self.fake.job_results(key, m.group(1), (q.get("cursor") or [""])[0], int((q.get("limit") or ["100"])[0])), 200
        else:
            raise ApiError("not_found", f"no such endpoint: {method} {path}")
        self.check_response(status, out)
        if self.fake.validate and path.endswith("/results"):
            from harness import openapi

            for it in out["items"]:
                for err in openapi.errors(openapi.schema("BatchItem"), it):
                    self.fake.violations.append(f"job item: {err}")
                if it["status"] == "ok":
                    for err in openapi.errors(openapi.schema("EmbedResponse"), it["result"]):
                        if ("/v1/embeddings", err) not in openapi.KNOWN_SPEC_ISSUES:
                            self.fake.violations.append(f"job item result: {err}")
        self.send_json(status, out, extra)

    def record(self):
        """Write the log entry of this request (once). It goes in before the response is sent, never after."""
        if self.logged or self.req_path.startswith("/_fake/"):
            return
        self.logged = True
        with self.fake.lock:
            self.fake.log.append(
                {
                    "method": self.method,
                    "path": self.req_path,
                    "status": self.status,
                    "replayed": self.replayed,
                    "userAgent": self.headers.get("User-Agent"),
                    "idempotencyKey": self.headers.get("Idempotency-Key"),
                    "accept": self.headers.get("Accept"),
                    "contentType": self.headers.get("Content-Type"),
                    "project": self.headers.get("X-Foldbun-Project"),
                    "hasAuthorization": "Authorization" in self.headers,
                    "t": self.arrived,
                }
            )

    def batch(self, key, path, body, idem, rule):
        endpoint = path[len("/v1/batch/") :]
        model = body.get("model") or DEFAULT_MODEL.get("/v1/" + endpoint)
        if model not in MODEL or "/v1/" + endpoint not in MODEL[model][3]:
            raise ApiError("invalid_request", f"model {model} does not serve /v1/{endpoint}")
        for i, item in enumerate(body["items"]):
            if "model" in item or "id" not in (item.get("image") or {"id": 1}):
                raise ApiError("invalid_request", f"items[{i}]: no model inside items, and images only as handles")
        if rule and rule.get("error"):
            e = rule["error"]
            raise ApiError(e["code"], e.get("message", "scripted"), e.get("retryAfterMs", 0))
        limit = 4
        with self.fake.lock:
            if self.fake.open_batches.get(key, 0) >= limit:
                raise ApiError("too_many_batches", "too many open batch requests", 5000)
            self.fake.open_batches[key] = self.fake.open_batches.get(key, 0) + 1
        try:
            start, items, n = time.monotonic(), [], len(body["items"])
            sse = "text/event-stream" in (self.headers.get("Accept") or "")
            if sse:
                self.status = 200
                self.record()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Request-Id", secrets.token_hex(8))
                self.send_header("Connection", "close")
                self.end_headers()
                self.status, self.close_connection = 200, True
                self.event(path, "progress", {"done": 0, "remaining": n})
            drop_after = (rule or {}).get("disconnectAfterItems")
            for entry in self.fake.batch_items(key, endpoint, body, idem, rule):
                items.append(entry)
                if sse:
                    self.event(path, "item", entry)
                    if drop_after is not None and len(items) >= drop_after:
                        return
            counts = {s: sum(1 for e in items if e["status"] == s) for s in ("ok", "error", "expired")}
            cost = r6(sum(e["result"]["cost"] for e in items if e["status"] == "ok" and not e.get("replayed")))
            with self.fake.lock:
                balance = self.fake.balance["funded"]
            summary = {"counts": counts, "cost": cost, "balance": balance, "elapsedMs": int((time.monotonic() - start) * 1000)}
            if sse:
                self.event(path, "summary", summary)
            else:
                out = {"items": items, "summary": summary}
                self.check_response(200, out)
                if self.fake.validate:
                    from harness import openapi

                    for e in items:
                        for err in openapi.event_errors(path, "item", e):
                            self.fake.violations.append(f"batch item {path}: {err}")
                self.send_json(200, out)
        finally:
            with self.fake.lock:
                self.fake.open_batches[key] -= 1

    def event(self, path, name, data):
        if self.fake.validate:
            from harness import openapi

            for e in openapi.event_errors(path, name, data):
                with self.fake.lock:
                    self.fake.violations.append(f"event {name} {path}: {e}")
        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def control(self, method, path, raw):
        f = self.fake
        if path == "/_fake/reset" and method == "POST":
            f.reset(json.loads(raw) if raw else None)
            return self.send_json(200, {"ok": True})
        if path == "/_fake/log":
            with f.lock:
                return self.send_json(200, {"log": sorted(f.log, key=lambda e: e["t"]), "violations": f.violations})
        return self.send_json(404, ApiError("not_found", "unknown control path").body())

    # ---- record mode: forward to the real API and keep what it answered as fixtures ----
    def proxy(self, method, path, raw):
        f = self.fake
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() in ("authorization", "x-foldbun-project", "content-type", "accept", "idempotency-key", "user-agent")
        }
        req = urllib.request.Request(f.upstream + path, data=raw if method == "POST" else None, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as err:
            resp = err
        self.send_response(resp.status)
        for k in (
            "Content-Type",
            "Retry-After",
            "X-Request-Id",
            "Idempotent-Replayed",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "X-RateLimit-Scope",
        ):
            if resp.headers.get(k):
                self.send_header(k, resp.headers[k])
        self.send_header("Connection", "close")
        self.end_headers()
        self.status, self.close_connection = resp.status, True
        if (resp.headers.get("Content-Type") or "").startswith("text/event-stream"):
            body, event = json.loads(raw), None
            for line in resp:
                self.wfile.write(line)
                self.wfile.flush()
                text = line.decode().strip()
                if text.startswith("event:"):
                    event = text[6:].strip()
                elif text.startswith("data:") and event == "item":
                    item = json.loads(text[5:])
                    if item.get("status") == "ok":
                        req_item = dict(body["items"][item["index"]], **({"model": body["model"]} if "model" in body else {}))
                        self.keep("/v1/" + path[len("/v1/batch/") :], req_item, item["result"])
            return
        data = resp.read()
        self.wfile.write(data)
        if resp.status < 300 and path == "/v1/images":
            f.upstream_ids[json.loads(data)["id"]] = hashlib.sha256(raw).hexdigest()
        elif resp.status < 300 and path in INFERENCE:
            self.keep(path, json.loads(raw), json.loads(data))

    def keep(self, path, req, result):
        f = self.fake
        ref = req.get("image") or {}
        if "base64" in ref:
            sha = hashlib.sha256(base64.b64decode(ref["base64"].split(",")[-1])).hexdigest()
        else:
            sha = f.upstream_ids.get(ref.get("id"), (ref.get("id") or "img_")[4:])
        drop = {"id", "model", "image", "cost", "balance", "billing", "timing", "cache", "imageFee"}
        fx = {
            "endpoint": path,
            "model": result.get("model"),
            "image": sha,
            "request": {k: v for k, v in req.items() if k not in ("image", "model")},
            "response": {k: v for k, v in result.items() if k not in drop},
            "timing": {k: result["timing"][k] for k in ("queueMs", "processingMs") if k in result.get("timing", {})},
        }
        with f.lock:
            f.recorded.append(fx)
            if f.record:
                Path(f.record).mkdir(parents=True, exist_ok=True)
                (Path(f.record) / "recorded.json").write_text(json.dumps(f.recorded, indent=1) + "\n", encoding="utf-8")


class FakeServer:
    """In-process server: `with FakeServer(...) as srv: srv.url`."""

    def __init__(self, port=0, **kw):
        self.fake = Fake(**kw)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.fake = self.fake
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m harness fakeapi", description="Run the fake Stack API on a local port.")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--fixtures", action="append", default=None, help="fixture directory (repeatable; default: use-cases/*/fixtures)")
    ap.add_argument("--scenario", help="JSON scenario file with scripted rules")
    ap.add_argument("--no-validate", action="store_true", help="do not check requests and responses against the API description")
    ap.add_argument("--upstream", help="record mode: forward every request to this base URL")
    ap.add_argument("--record", help="record mode: directory for recorded.json")
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    fixtures = args.fixtures or [str(p) for p in sorted(root.glob("use-cases/*/fixtures"))]
    scenario = json.loads(Path(args.scenario).read_text()) if args.scenario else None
    srv = FakeServer(args.port, fixtures=fixtures, scenario=scenario, validate=not args.no_validate, upstream=args.upstream, record=args.record)
    print(f"fake API listening on {srv.url}  (export FOLDBUN_API_BASE={srv.url} FOLDBUN_API_KEY=fbs_fake)", flush=True)
    try:
        srv.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
