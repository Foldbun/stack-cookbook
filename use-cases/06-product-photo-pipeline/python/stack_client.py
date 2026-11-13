# Copied from clients/python/ in github.com/Foldbun/stack-cookbook. Edit the original there.
"""Minimal client for the Foldbun Stack API (Python standard library only).

Semantics shared with the TypeScript and Go client files: API key from FOLDBUN_API_KEY, base URL from
FOLDBUN_API_BASE, retries only for `retryable` errors, an idempotency key per call that is reused on retry,
a per-pool token bucket, a concurrency cap, a credit cap (FOLDBUN_MAX_CREDITS) and a ledger of every call.
"""

import base64
import http.client
import json
import math
import os
import threading
import time
import urllib.parse
import uuid

VERSION = "1.0.0"
DEFAULT_BASE = "https://api.stack.foldbun.com"
USER_AGENT = f"foldbun-stack-cookbook/{VERSION} (python; +https://github.com/Foldbun/stack-cookbook)"
MISSING_KEY = (
    'FOLDBUN_API_KEY is not set. See "API keys" in the README; every use case page shows the output and cost of a real run, so it can be read without a key.'
)
DEFAULT_MODEL = {"/v1/ocr": "ppocr-v6", "/v1/documents/parse": "paddleocr-vl", "/v1/remove-background": "birefnet", "/v1/refine": "sam3"}
# (connect, read) timeouts in seconds
T_MODELS, T_UPLOAD, T_CALL, T_BATCH = (10, 10), (10, 60), (10, 90), (10, 45)
BATCH_MAX_S, MAX_RETRIES, MAX_WAIT_S = 600, 5, 60


class StackError(Exception):
    """An API error (`status`, `code`, ...) or a local refusal (`status` 0)."""

    def __init__(self, status, code, message, retryable=False, retry_after_ms=0, request_id=None):
        super().__init__(f"{status} {code}: {message}" + (f" (request {request_id})" if request_id else ""))
        self.status, self.code, self.message = status, code, message
        self.retryable, self.retry_after_ms, self.request_id = retryable, retry_after_ms, request_id


class MissingApiKey(StackError):
    def __init__(self):
        super().__init__(0, "missing_api_key", MISSING_KEY)


class BudgetExceeded(StackError):
    def __init__(self, message):
        super().__init__(0, "budget_exceeded", message)


def image_base64(data: bytes) -> dict:
    """Inline image input: simplest for a single call."""
    return {"base64": base64.b64encode(data).decode("ascii")}


def image_handle(handle: str) -> dict:
    """Uploaded image input: reuse one upload across calls and models; required in batches."""
    return {"id": handle}


class _Bucket:
    def __init__(self, per_minute):
        self.rate, self.tokens, self.t = per_minute / 60.0, float(per_minute), time.monotonic()
        self.cap, self.lock = float(per_minute), threading.Lock()

    def take(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


class StackClient:
    def __init__(self, api_key=None, base_url=None, *, max_concurrency=4, max_credits=None, throttle=True):
        self.api_key = api_key or os.environ.get("FOLDBUN_API_KEY")
        if not self.api_key:
            raise MissingApiKey()
        base = urllib.parse.urlsplit(base_url or os.environ.get("FOLDBUN_API_BASE") or DEFAULT_BASE)
        self._scheme, self._host, self._prefix = base.scheme, base.netloc, base.path.rstrip("/")
        cap = max_credits if max_credits is not None else os.environ.get("FOLDBUN_MAX_CREDITS")
        self.max_credits = float(cap) if cap not in (None, "") else None
        self.throttle = throttle
        self.ledger = []  # one row per successful inference call; see calls()
        self._sem = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()
        self._models, self._buckets, self._uploads, self._spent, self._reserved = None, {}, {}, 0.0, 0.0

    # region http
    def _request(self, method, path, body=None, headers=None, timeout=T_CALL, stream=False):
        conn_cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(self._host, timeout=timeout[0])
        h = {"Authorization": f"Bearer {self.api_key}", "User-Agent": USER_AGENT, "Accept": "application/json"}
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

    def _send(self, method, path, body=None, headers=None, timeout=T_CALL, retry_network=True, stream=False):
        """Send with retries: `retryable` API errors always, network errors only when retry_network."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp, raw = self._request(method, path, body, headers, timeout, stream)
            except (OSError, http.client.HTTPException) as exc:
                if not retry_network or attempt == MAX_RETRIES:
                    raise StackError(0, "network_error", str(exc), True) from exc
                time.sleep(min(2**attempt, MAX_WAIT_S))
                continue
            if resp.status < 300:
                return resp, raw
            err = self._error(resp, raw)
            if not err.retryable or attempt == MAX_RETRIES:
                raise err
            header = resp.getheader("Retry-After")
            wait_ms = max(err.retry_after_ms, int(header) * 1000 if header and header.isdigit() else 0, 1000)
            time.sleep(min(wait_ms / 1000, MAX_WAIT_S))
        raise AssertionError("unreachable")

    @staticmethod
    def _error(resp, raw):
        rid = resp.getheader("X-Request-Id")
        try:
            e = json.loads(raw)["error"]
            return StackError(resp.status, e["code"], e["message"], e.get("retryable", False), e.get("retryAfterMs", 0), rid)
        except (ValueError, KeyError, TypeError):
            return StackError(resp.status, "http_error", raw[:200].decode("utf-8", "replace"), resp.status >= 500, 0, rid)

    # endregion

    def get(self, path):
        _, raw = self._send("GET", path, timeout=T_MODELS)
        return json.loads(raw)

    def models(self):
        if self._models is None:
            self._models = {m["id"]: m for m in self.get("/v1/models")["data"]}
        return self._models

    # region upload
    def upload(self, data: bytes, content_type="image/jpeg") -> dict:
        """POST /v1/images with the raw bytes. Free; returns {id, width, height, bytes, contentType}."""
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
        self._reserve(estimate(self.models(), path, body, model))
        headers = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        data = json.dumps(body).encode()
        try:
            with self._sem:
                self._take(model)
                try:
                    resp, raw = self._send("POST", path, data, headers, timeout)
                except StackError as err:
                    handle = (body.get("image") or {}).get("id")
                    if err.code != "image_not_found" or handle not in self._uploads:
                        raise
                    # Evicted: upload again (same handle) and repeat once. A 404 is stored under the idempotency
                    # key and was not charged, so the repeat needs a new key.
                    self.upload(*self._uploads[handle])
                    headers["Idempotency-Key"] = str(uuid.uuid4())
                    resp, raw = self._send("POST", path, data, headers, timeout)
        finally:
            self._release(estimate(self.models(), path, body, model))
        result = json.loads(raw)
        self._record(path, result, resp.getheader("Idempotent-Replayed") == "true", tag)
        return result

    # endregion

    # region batch
    def batch(self, endpoint, body, idempotency_key=None, *, tag=None):
        """POST /v1/batch/{endpoint} as Server-Sent Events. Yields (event, data) for progress, item and summary."""
        path = f"/v1/batch/{endpoint}"
        single = f"/v1/{endpoint}"
        model = body.get("model") or DEFAULT_MODEL.get(single)
        worst = sum(estimate(self.models(), single, it, model) for it in body["items"])
        self._reserve(worst)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        try:
            with self._sem:
                self._take(model)
                resp, _ = self._send("POST", path, json.dumps(body).encode(), headers, T_BATCH, stream=True)
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
            "list": billing.get("total", result.get("cost", 0)),
            "charged": 0 if replayed else result.get("cost", 0),
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

    def _take(self, model):
        info = self.models().get(model) or {}
        per_minute = info.get("perMinute")
        if not self.throttle or not per_minute:
            return
        with self._lock:
            bucket = self._buckets.setdefault(info.get("pool") or model, _Bucket(per_minute))
        bucket.take()


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
