# Copied from clients/python/ in github.com/Foldbun/stack-cookbook. Edit the original there.
"""Minimal client for the Foldbun Stack API (Python standard library only).

Semantics shared with the TypeScript and Go client files:

- Free tier: without FOLDBUN_API_KEY a call carries only `X-Foldbun-Project` (FOLDBUN_PROJECT_ID, default: the
  Cookbook's shared project) and is not charged. The free tier takes inline base64 images and single calls only.
- Paid: with FOLDBUN_API_KEY a call carries `Authorization` and the project header (the call's source project), is
  charged at list price and gets an idempotency key that is reused on retry.
- Limits: a 429 (`Retry-After`, `retryAfterMs`) is retried a bounded number of times with jitter; when
  `X-RateLimit-Remaining` is 0 the next call waits `X-RateLimit-Reset` seconds. `daily_limit_reached` and
  `free_tier_unavailable` are final.
- Also: base URL from FOLDBUN_API_BASE, a concurrency cap, a credit cap for keyed runs (FOLDBUN_MAX_CREDITS) and a
  ledger of every call.
"""

import base64
import http.client
import json
import math
import os
import random
import threading
import time
import urllib.parse
import uuid

VERSION = "1.1.0"
DEFAULT_BASE = "https://api.stack.foldbun.com"
USER_AGENT = f"foldbun-stack-cookbook/{VERSION} (python; +https://github.com/Foldbun/stack-cookbook)"
# The Cookbook's own project. A project ID is not a secret: it names who is calling and authorises nothing beyond
# the free tier, whose limits are shared by every reader of the Cookbook. To go beyond trying the recipes, create
# your own project in the console and set FOLDBUN_PROJECT_ID (or use an API key).
COOKBOOK_PROJECT = "fbp_2kIj7bWkULQkLAbceHhfLW4eY6O0"
DEFAULT_MODEL = {"/v1/ocr": "ppocr-v6", "/v1/documents/parse": "paddleocr-vl", "/v1/remove-background": "birefnet", "/v1/refine": "sam3"}
# (connect, read) timeouts in seconds
T_MODELS, T_UPLOAD, T_CALL, T_BATCH = (10, 10), (10, 60), (10, 90), (10, 45)
BATCH_MAX_S, MAX_RETRIES, MAX_WAIT_S = 600, 5, 60
# Codes that are retried even when the error does not say `retryable`, and codes that are never retried.
RETRY_CODES, FINAL_CODES = {"rate_limited", "free_busy"}, {"daily_limit_reached", "free_tier_unavailable"}
FREE_HINT = {
    "daily_limit_reached": (
        " The daily limit of the free tier is shared by everyone using this project and resets at 00:00 UTC;"
        " set FOLDBUN_API_KEY to continue now, or FOLDBUN_PROJECT_ID to use your own project."
    ),
    "free_tier_unavailable": " The free tier is not available here; set FOLDBUN_API_KEY to use an API key.",
}


class StackError(Exception):
    """An API error (`status`, `code`, ...) or a local refusal (`status` 0)."""

    def __init__(self, status, code, message, retryable=False, retry_after_ms=0, request_id=None):
        super().__init__(f"{status} {code}: {message}" + (f" (request {request_id})" if request_id else ""))
        self.status, self.code, self.message = status, code, message
        self.retryable, self.retry_after_ms, self.request_id = retryable, retry_after_ms, request_id


class MissingApiKey(StackError):
    """The run needs FOLDBUN_API_KEY because the free tier cannot do it; `reason` says what the free tier lacks."""

    def __init__(self, reason):
        super().__init__(0, "missing_api_key", f"FOLDBUN_API_KEY is not set, and this needs a key: {reason}.")


class BudgetExceeded(StackError):
    def __init__(self, message):
        super().__init__(0, "budget_exceeded", message)


def image_base64(data: bytes) -> dict:
    """Inline image input: simplest for a single call."""
    return {"base64": base64.b64encode(data).decode("ascii")}


def image_handle(handle: str) -> dict:
    """Uploaded image input (needs a key): reuse one upload across calls and models; required in batches."""
    return {"id": handle}


class StackClient:
    def __init__(self, api_key=None, base_url=None, *, project=None, max_concurrency=4, max_credits=None):
        self.api_key = api_key or os.environ.get("FOLDBUN_API_KEY") or None  # optional: without it, the free tier
        self.project = project or os.environ.get("FOLDBUN_PROJECT_ID") or COOKBOOK_PROJECT
        self.free = not self.api_key
        base = urllib.parse.urlsplit(base_url or os.environ.get("FOLDBUN_API_BASE") or DEFAULT_BASE)
        self._scheme, self._host, self._prefix = base.scheme, base.netloc, base.path.rstrip("/")
        cap = max_credits if max_credits is not None else os.environ.get("FOLDBUN_MAX_CREDITS")
        self.max_credits = float(cap) if cap not in (None, "") else None
        self.ledger = []  # one row per successful inference call; see calls()
        self._sem = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()
        self._models, self._uploads, self._spent, self._reserved, self._gate = None, {}, 0.0, 0.0, {}

    # region http
    def _request(self, method, path, body=None, headers=None, timeout=T_CALL, stream=False):
        conn_cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(self._host, timeout=timeout[0])
        h = {"X-Foldbun-Project": self.project, "User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        h.update(headers or {})
        conn.connect()
        conn.sock.settimeout(timeout[1])
        conn.request(method, self._prefix + path, body=body, headers=h)
        resp = conn.getresponse()
        if stream and resp.status < 300:
            return resp, None
        raw = resp.read()
        conn.close()
        return resp, raw

    def _send(self, method, path, body=None, headers=None, timeout=T_CALL, retry_network=True, stream=False, gate=None):
        """Send with retries: `retryable` API errors (and 429s) always, network errors only when retry_network.

        `gate` names the rate-limit window of the call (the model): when a response said `X-RateLimit-Remaining: 0`,
        the next call of that window waits `X-RateLimit-Reset` seconds first."""
        for attempt in range(MAX_RETRIES + 1):
            self._wait_gate(gate)
            try:
                resp, raw = self._request(method, path, body, headers, timeout, stream)
            except (OSError, http.client.HTTPException) as exc:
                if not retry_network or attempt == MAX_RETRIES:
                    raise StackError(0, "network_error", str(exc), True) from exc
                time.sleep(min(2**attempt, MAX_WAIT_S))
                continue
            if resp.status < 300:
                self._note_limits(gate, resp)
                return resp, raw
            err = self._error(resp, raw)
            if err.code in FINAL_CODES or not (err.retryable or err.code in RETRY_CODES) or attempt == MAX_RETRIES:
                raise err
            header = resp.getheader("Retry-After")
            wait = max(err.retry_after_ms / 1000, float(header) if header and header.isdigit() else 0, 1.0)
            time.sleep(min(wait + random.uniform(0, min(wait * 0.25, 2.0)), MAX_WAIT_S))  # jitter on top, never shorter
        raise AssertionError("unreachable")

    def _wait_gate(self, gate):
        while gate is not None:
            with self._lock:
                wait = self._gate.get(gate, 0) - time.monotonic()
            if wait <= 0:
                return
            time.sleep(wait)

    def _note_limits(self, gate, resp):
        """X-RateLimit-Remaining 0: hold the next call of this window for X-RateLimit-Reset seconds."""
        remaining, reset = resp.getheader("X-RateLimit-Remaining"), resp.getheader("X-RateLimit-Reset")
        if gate is None or remaining is None or not remaining.strip().isdigit() or int(remaining) > 0:
            return
        try:
            seconds = min(max(float(reset), 0.0), MAX_WAIT_S) if reset else 1.0
        except ValueError:
            seconds = 1.0
        with self._lock:
            self._gate[gate] = max(self._gate.get(gate, 0), time.monotonic() + seconds)

    def _error(self, resp, raw):
        rid = resp.getheader("X-Request-Id")
        try:
            e = json.loads(raw)["error"]
            message = e["message"] + (FREE_HINT.get(e["code"], "") if self.free else "")
            return StackError(resp.status, e["code"], message, e.get("retryable", False), e.get("retryAfterMs", 0), rid)
        except (ValueError, KeyError, TypeError):
            return StackError(resp.status, "http_error", raw[:200].decode("utf-8", "replace"), resp.status >= 500, 0, rid)

    # endregion

    def get(self, path):
        _, raw = self._send("GET", path, timeout=T_MODELS)
        return json.loads(raw)

    def require_key(self, reason):
        """Refuse a feature the free tier does not have (uploads, handles, batches, big runs): raises MissingApiKey."""
        if self.free:
            raise MissingApiKey(reason)

    def whoami(self):
        """GET /v1/whoami: the project (free) or the account (key) behind the credentials."""
        return self.get("/v1/whoami")

    def models(self):
        """GET /v1/models: the model list with prices, once. Works with a project ID alone (each model then also carries its free-tier limits)."""
        if self._models is None:
            self._models = {m["id"]: m for m in self.get("/v1/models")["data"]}
        return self._models

    # region upload
    def upload(self, data: bytes, content_type="image/jpeg") -> dict:
        """POST /v1/images with the raw bytes. Needs a key; returns {id, width, height, bytes, contentType}."""
        self.require_key("image uploads are not part of the free tier, which takes inline base64 images")
        _, raw = self._send("POST", "/v1/images", data, {"Content-Type": content_type}, T_UPLOAD)
        info = json.loads(raw)
        with self._lock:
            self._uploads[info["id"]] = (data, content_type)
        return info

    # endregion

    # region post
    def post(self, path, body, idempotency_key=None, *, tag=None, timeout=T_CALL):
        """One inference call. The idempotency key is reused on every retry, so a retry never charges twice."""
        model = body.get("model") or DEFAULT_MODEL.get(path)
        if "id" in (body.get("image") or {}):
            self.require_key("image handles come from uploads, which are not part of the free tier")
        worst = 0 if self.free else estimate(self.models(), path, body, model)
        self._reserve(worst)
        headers = {"Content-Type": "application/json"}
        if not self.free:
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        data = json.dumps(body).encode()
        try:
            with self._sem:
                try:
                    resp, raw = self._send("POST", path, data, headers, timeout, gate=model)
                except StackError as err:
                    handle = (body.get("image") or {}).get("id")
                    if err.code != "image_not_found" or handle not in self._uploads:
                        raise
                    # Evicted: upload again (same handle) and repeat once. A 404 is stored under the idempotency
                    # key and was not charged, so the repeat needs a new key.
                    self.upload(*self._uploads[handle])
                    headers["Idempotency-Key"] = str(uuid.uuid4())
                    resp, raw = self._send("POST", path, data, headers, timeout, gate=model)
        finally:
            self._release(worst)
        result = json.loads(raw)
        self._record(path, result, resp.getheader("Idempotent-Replayed") == "true", tag)
        return result

    # endregion

    # region batch
    def batch(self, endpoint, body, idempotency_key=None, *, tag=None):
        """POST /v1/batch/{endpoint} as Server-Sent Events. Yields (event, data) for progress, item and summary. Needs a key."""
        self.require_key("batch requests are not part of the free tier")
        path = f"/v1/batch/{endpoint}"
        single = f"/v1/{endpoint}"
        model = body.get("model") or DEFAULT_MODEL.get(single)
        worst = sum(estimate(self.models(), single, it, model) for it in body["items"])
        self._reserve(worst)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        try:
            with self._sem:
                resp, _ = self._send("POST", path, json.dumps(body).encode(), headers, T_BATCH, stream=True, gate=model)
                deadline, event = time.monotonic() + BATCH_MAX_S, None
                try:
                    for line in resp:
                        if time.monotonic() > deadline:
                            raise StackError(0, "batch_timeout", f"batch exceeded {BATCH_MAX_S} s")
                        line = line.decode("utf-8").rstrip("\r\n")
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event:
                            data = json.loads(line[5:])
                            if event == "item" and data.get("status") == "ok":
                                self._record(path, data["result"], bool(data.get("replayed")), tag)
                            yield event, data
                            if event == "summary":
                                return
                except (OSError, http.client.HTTPException) as exc:
                    raise StackError(0, "network_error", f"batch stream: {exc}", True) from exc
                finally:
                    resp.close()
        finally:
            self._release(worst)

    # endregion

    # region ledger
    def _record(self, endpoint, result, replayed, tag):
        billing = result.get("billing") or {}
        row = {
            "endpoint": endpoint,
            "model": result.get("model"),
            "list": billing.get("total", billing.get("listPrice", result.get("cost", 0))),
            "charged": 0 if replayed or self.free else result.get("cost", 0),
            "cache": result.get("cache"),
            "queueMs": result.get("timing", {}).get("queueMs", 0),
            "processingMs": result.get("timing", {}).get("processingMs", 0),
            "replayed": replayed,
        }
        with self._lock:
            self.ledger.append((tag, row))
            if not replayed:
                self._spent += row["list"]

    def calls(self, tag=None):
        """Ledger rows (the `calls[]` of result.json), optionally only those made with `tag`."""
        with self._lock:
            return [dict(r) for t, r in self.ledger if tag is None or t == tag]

    def summary(self):
        rows = [r for _, r in self.ledger if not r["replayed"]]
        return {"calls": len(self.ledger), "listTotal": _round(sum(r["list"] for r in rows)), "chargedTotal": _round(sum(r["charged"] for r in rows))}

    # endregion

    def _reserve(self, worst):
        with self._lock:
            if self.max_credits is not None and self._spent + self._reserved + worst > self.max_credits + 1e-9:
                raise BudgetExceeded(f"call refused: up to {worst:g} credits would pass FOLDBUN_MAX_CREDITS={self.max_credits:g} (spent {self._spent:g})")
            self._reserved += worst

    def _release(self, worst):
        with self._lock:
            self._reserved -= worst


def _round(x):
    return math.floor(x * 1e6 + 0.5) / 1e6


def estimate(models, path, body, model):
    """Worst-case list price of one call (the encoding fee is always counted)."""
    price = {p["unit"]: p["price"] for p in (models.get(model) or {}).get("prices", [])}
    unit = lambda u, n=1: price.get(u, 0) * n  # noqa: E731
    if path == "/v1/segment":
        prompts = body.get("prompts", [])
        texts = sum(1 for p in prompts if "text" in p)
        boxes = sum(1 for p in prompts if "box" in p)
        points = sum(1 for p in prompts if "point" in p)
        if model == "sam-lite":
            return _round(unit("encode_image") + unit("box", boxes) + unit("point", points))
        return _round(unit("encode_image") + unit("prompt", texts or (1 if boxes else 0)) + unit("point", points))
    if path == "/v1/refine":
        return _round(unit("encode_image") + unit("refine"))
    if path == "/v1/embeddings":
        return _round(unit("text", len(body["texts"])) if "texts" in body else unit("image"))
    return _round(unit("page") if path == "/v1/documents/parse" else unit("image"))
