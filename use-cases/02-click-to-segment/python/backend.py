"""Key-holding backend of the click-to-segment component. The HTTP contract is contract/02-backend.yaml; the browser
talks only to this backend, and only this backend talks to the Foldbun Stack API.

Standard library only: `http.server` with one thread per connection. The StackClient is synchronous and thread-safe;
what the backend itself shares between requests (sessions, image cache) is guarded by one lock that is never held
during a Stack call, so a slow upstream call blocks only its own request.
"""

import hashlib
import http.client
import json
import math
import re
import socket
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote

import stack_client as sc

LANGUAGE = "python"
IMAGE_BYTES = 20 * 1024 * 1024  # the Stack upload limit
REFINE_BYTES = 64 * 1024
MAX_POINTS = 20  # the Stack limit of POST /v1/refine
MAX_IMAGES = 256
MAX_SESSIONS = 1000
IDLE_MS = 30 * 60_000
DRAIN_BYTES = 32 * 1024 * 1024  # an unread request body is read and dropped up to this size, so the answer can be sent
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
IMAGE_ID = re.compile(r"im_[0-9a-f]{32}")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}


class Stack(Protocol):
    """The part of StackClient the backend uses (the tests pass a stand-in)."""

    def upload(self, data: bytes, content_type: str = ...) -> dict: ...
    def post(self, path: str, body: dict, idempotency_key: str | None = ..., *, tag: str | None = ...) -> dict: ...
    def calls(self, tag: str | None = ...) -> list[dict]: ...


# region errors
class ApiError(Exception):
    """A backend error, sent as {"error": {code, message, retryable, retryAfterMs, upstreamCode?, requestId?}}."""

    def __init__(self, status, code, message, retryable=False, retry_after_ms=0, upstream_code=None, request_id=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retryable, self.retry_after_ms = retryable, retry_after_ms
        self.upstream_code, self.request_id = upstream_code, request_id
        self.allow: str | None = None  # Allow header of a 405

    def body(self) -> dict:
        err = {"code": self.code, "message": self.message, "retryable": self.retryable, "retryAfterMs": self.retry_after_ms}
        if self.upstream_code:
            err["upstreamCode"] = self.upstream_code
        if self.request_id:
            err["requestId"] = self.request_id
        return {"error": err}


def from_stack(err: BaseException) -> ApiError:
    """Map an error of the client file (after its own retries) to the backend's error. Stack's message is not forwarded."""
    if isinstance(err, ApiError):
        return err
    if isinstance(err, sc.BudgetExceeded):
        return ApiError(503, "credit_cap", "the backend's credit cap is reached")
    if isinstance(err, sc.StackError):
        if not err.retryable:
            return ApiError(502, "upstream_refused", "Foldbun Stack refused the call", False, 0, err.code, err.request_id)
        return ApiError(503, "upstream_busy", "Foldbun Stack is busy; try again", True, max(1000, err.retry_after_ms), err.code, err.request_id)
    if isinstance(err, OSError | http.client.HTTPException | ValueError):  # no usable answer (for example, not JSON)
        return ApiError(503, "upstream_busy", "Foldbun Stack is busy; try again", True, 1000, "network_error")
    return ApiError(500, "internal", "internal error")


# endregion


# region session-limit
class RateLimit:
    """Token bucket: `per_minute` sustained, `burst` at once. take() returns 0 when allowed, else the wait in ms."""

    def __init__(self, per_minute: float, burst: float, now: float):
        self.per_minute, self.burst, self.tokens, self.at = per_minute, burst, burst, now

    def take(self, now: float) -> int:
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.per_minute / 60_000)
        self.at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0
        return math.ceil((1 - self.tokens) * 60_000 / self.per_minute)


@dataclass
class Session:
    id: str
    tag: str  # ledger tag of this session's calls in the client file
    seen: float
    refine: RateLimit
    upload: RateLimit
    uploads: int = 0
    clicks: int = 0


# endregion


@dataclass
class Image:
    handle: str
    width: int
    height: int


@dataclass
class Request:
    method: str
    path: str  # without the query string
    headers: Any  # case-insensitive mapping
    read_body: Callable[[int], bytes]  # read_body(limit): the whole body, or ApiError 413 above `limit` bytes


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def json_response(status: int, doc: Any, headers: dict[str, str] | None = None) -> Response:
    return Response(status, json.dumps(doc).encode(), {"Content-Type": "application/json", **(headers or {})})


def invalid(message: str) -> ApiError:
    return ApiError(400, "invalid_request", message)


def finite(v: Any) -> bool:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


class Backend:
    def __init__(
        self,
        stack: Stack,
        *,
        models: list[str] | None = None,
        static_dir: str | Path | None = None,
        now: Callable[[], float] | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.stack = stack
        self.models = models or ["sam-lite"]
        self.static_dir = Path(static_dir) if static_dir else None
        self.now = now or (lambda: time.monotonic() * 1000)  # milliseconds
        self.log = log or (lambda line: print(line, flush=True))
        self._lock = threading.Lock()  # guards the two maps below and the sessions' counters and buckets
        self._sessions: OrderedDict[str, Session] = OrderedDict()  # least recently used first
        self._images: OrderedDict[str, Future] = OrderedDict()  # least recently used first; a Future[Image] while uploading

    def handle(self, req: Request) -> Response:
        """Answer one request. Never raises."""
        try:
            return self._route(req)
        except Exception as exc:
            err = from_stack(exc)
            if err.code == "internal":
                self.log(f"internal error: {type(exc).__name__}: {exc}")
            headers = {"Retry-After": str(math.ceil(err.retry_after_ms / 1000))} if err.retry_after_ms > 0 else {}
            if err.allow:
                headers["Allow"] = err.allow
            return json_response(err.status, err.body(), headers)

    def _route(self, req: Request) -> Response:
        path = req.path
        if path != "/api" and not path.startswith("/api/"):
            return self._static(req)
        if path == "/api/health":
            if req.method != "GET":
                raise ApiError(405, "method_not_allowed", "use GET")
            return json_response(200, {"status": "ok", "language": LANGUAGE, "client": f"stackClient {sc.VERSION}"})
        routes = {"/api/images": ("POST", self._upload_image), "/api/refine": ("POST", self._refine), "/api/usage": ("GET", self._usage)}
        route = routes.get(path)
        if not route:
            raise ApiError(404, "not_found", f"no route {path}")
        if req.method != route[0]:
            raise ApiError(405, "method_not_allowed", f"use {route[0]}")
        return json_response(200, route[1](req, self._session(req.headers.get("X-Session-Id"))))

    # region static
    def _static(self, req: Request) -> Response:
        """GET and HEAD: / is index.html, /<name> a regular file directly inside the static directory. Other methods are
        405 with Allow, whatever the name; names that are not served are 404."""
        if req.method not in ("GET", "HEAD"):
            err = ApiError(405, "method_not_allowed", "use GET or HEAD")
            err.allow = "GET, HEAD"
            raise err
        root = self.static_dir
        try:
            name = unquote(req.path[1:], errors="strict") or "index.html"
            ok = root is not None and name not in (".", "..") and not name.startswith(".") and not any(c in name for c in "/\\\0")
            file = root / name if ok and root else None
            # a symbolic link that leads out of the directory is not served either
            if file is None or not file.is_file() or file.resolve().parent != root.resolve():
                raise ApiError(404, "not_found", "no such file")
            data = file.read_bytes()
        except (OSError, UnicodeError, ValueError):
            raise ApiError(404, "not_found", "no such file") from None
        content_type = STATIC_TYPES.get(file.suffix.lower(), "application/octet-stream")
        return Response(200, data, {"Content-Type": content_type, "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    # endregion

    # region sessions
    def _session(self, header: str | None) -> Session:
        if header is None or not SESSION_ID.fullmatch(header):
            raise ApiError(400, "invalid_session", "send X-Session-Id: 1 to 64 characters from A-Z, a-z, 0-9, _ and -")
        now = self.now()
        with self._lock:
            while self._sessions and now - next(iter(self._sessions.values())).seen > IDLE_MS:
                self._sessions.popitem(last=False)  # idle for too long
            s = self._sessions.get(header)
            if s is None:
                s = Session(header, str(uuid.uuid4()), now, RateLimit(60, 10, now), RateLimit(10, 5, now))
                self._sessions[header] = s
            s.seen = now
            self._sessions.move_to_end(header)
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)  # too many: the one idle the longest goes first
            return s

    def _limit(self, bucket: RateLimit) -> None:
        with self._lock:
            wait = bucket.take(self.now())
        if wait:
            raise ApiError(429, "rate_limited", "too many requests in this session", True, wait)

    # endregion

    # region upload-once
    def _upload_image(self, req: Request, s: Session) -> dict:
        self._limit(s.upload)
        content_type = (req.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type not in IMAGE_TYPES:
            raise ApiError(415, "unsupported_media_type", "send the raw bytes as image/jpeg, image/png or image/webp")
        data = req.read_body(IMAGE_BYTES)
        if not data:
            raise invalid("the body is empty: send the raw bytes of the image")
        image_id = "im_" + hashlib.sha256(data).hexdigest()[:32]
        with self._lock:
            pending = self._images.get(image_id)
            owner = pending is None
            if pending is None:
                pending = self._images[image_id] = Future()
            self._images.move_to_end(image_id)  # most recently used goes last
            while len(self._images) > MAX_IMAGES:
                self._images.popitem(last=False)
        if owner:
            # one Stack upload per distinct content; the client file keeps the bytes and uploads again if Stack evicts them.
            # Concurrent uploads of the same bytes wait for this one instead of uploading again.
            try:
                u = self.stack.upload(data, content_type)
                pending.set_result(Image(u["id"], u["width"], u["height"]))
            except BaseException as exc:
                with self._lock:
                    if self._images.get(image_id) is pending:
                        del self._images[image_id]
                pending.set_exception(exc)
        try:
            img = pending.result()
        except Exception as exc:
            raise from_stack(exc) from exc
        with self._lock:
            s.uploads += 1
        return {"imageId": image_id, "width": img.width, "height": img.height}

    # endregion

    # region refine
    def _refine(self, req: Request, s: Session) -> dict:
        self._limit(s.refine)
        try:
            doc = json.loads(req.read_body(REFINE_BYTES).decode("utf-8"))
        except (ValueError, RecursionError):
            raise invalid("the body is not JSON") from None
        image_id, points, box, model = parse_refine(doc, self.models)
        with self._lock:
            pending = self._images.get(image_id)
            if pending is not None:
                self._images.move_to_end(image_id)
        not_found = ApiError(404, "image_not_found", "unknown imageId: upload the image again")
        if pending is None:
            raise not_found
        try:
            img = pending.result()
        except Exception:
            raise not_found from None
        check_bounds(points, box, img)
        # Stateless: every click sends all points of the object. The body is the same in every language.
        call: dict = {"model": model, "image": sc.image_handle(img.handle), "points": points}
        if box:
            call["box"] = box
        t = time.monotonic()
        try:
            result = self.stack.post("/v1/refine", call, tag=s.tag)
        except sc.StackError as exc:
            self.log(f"stack error: {exc}")
            raise from_stack(exc) from exc
        latency_ms = round((time.monotonic() - t) * 1000)
        with self._lock:
            s.clicks += 1
        # Stack returns the outline as a simplified polygon with holes by default: no mask decoding needed here.
        inst = (result.get("instances") or [{}])[0]
        return {
            "polygon": inst.get("polygon") or [],
            "bbox": inst.get("bbox") or [0, 0, 0, 0],
            "area": inst.get("area", 0),
            "score": inst.get("score", 0),
            "model": result.get("model") or model,
            "costCredits": result.get("cost", 0),
            "listCredits": (result.get("billing") or {}).get("total", 0),
            "cache": "hit" if result.get("cache") == "hit" else "miss",
            "latencyMs": latency_ms,
        }

    # endregion

    # region usage
    def _usage(self, req: Request, s: Session) -> dict:
        calls = self.stack.calls(s.tag)
        billed = [c for c in calls if not c["replayed"]]
        with self._lock:
            uploads, clicks = s.uploads, s.clicks
        return {
            "sessionId": s.id,
            "uploads": uploads,
            "clicks": clicks,
            "costCredits": round(sum(c["charged"] for c in billed), 6),
            "listCredits": round(sum(c["list"] for c in billed), 6),
            "cacheHits": sum(1 for c in calls if c["cache"] == "hit"),
            "cacheMisses": sum(1 for c in calls if c["cache"] == "miss"),
            "calls": calls,
        }

    # endregion


def parse_refine(doc: Any, models: list[str]) -> tuple[str, list[dict], list | None, str]:
    """Validate the body of POST /api/refine (shape only; bounds need the image size)."""
    if not isinstance(doc, dict):
        raise invalid("the body must be a JSON object")
    extra = [k for k in doc if k not in ("imageId", "points", "box", "model")]
    if extra:
        raise invalid(f"unknown field {extra[0]}")
    image_id = doc.get("imageId")
    if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
        raise invalid("imageId is missing or malformed")
    raw_points = doc.get("points")
    if not isinstance(raw_points, list) or len(raw_points) > MAX_POINTS:
        raise invalid(f"points must be a list of at most {MAX_POINTS}")
    points = []
    for p in raw_points:
        positive = p.get("positive") if isinstance(p, dict) else None
        if not isinstance(p, dict) or not finite(p.get("x")) or not finite(p.get("y")) or (positive is not None and not isinstance(positive, bool)):
            raise invalid("each point is {x, y, positive?} with numbers x and y")
        if any(k not in ("x", "y", "positive") for k in p):
            raise invalid("each point is {x, y, positive?}")
        points.append({"x": p["x"], "y": p["y"], "positive": True if positive is None else positive})
    box = doc.get("box")
    if box is not None and not (isinstance(box, list) and len(box) == 4 and all(finite(v) for v in box)):
        raise invalid("box is [x1, y1, x2, y2]")
    if not box and not any(p["positive"] for p in points):
        raise invalid("give at least one positive point or a box")
    model = doc.get("model")
    model = models[0] if model is None else model
    if not isinstance(model, str) or model not in models:
        raise ApiError(400, "model_not_allowed", f"this backend serves {', '.join(models)}")
    return image_id, points, box, model


def check_bounds(points: list[dict], box: list | None, img: Image) -> None:
    """Points in [0, width] x [0, height]; box with 0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height."""
    for p in points:
        if p["x"] < 0 or p["y"] < 0 or p["x"] > img.width or p["y"] > img.height:
            raise invalid(f"point ({p['x']}, {p['y']}) is outside the {img.width}x{img.height} image")
    if box and not (box[0] >= 0 and box[1] >= 0 and box[0] < box[2] and box[1] < box[3] and box[2] <= img.width and box[3] <= img.height):
        raise invalid(f"box is not inside the {img.width}x{img.height} image")


# The HTTP layer: a thin adapter from http.server to Backend.handle.
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "click-to-segment"
    timeout = 120  # seconds without data from the client: the connection is dropped
    backend: Backend

    def log_message(self, format, *args):  # the backend logs its own line per request
        pass

    def _too_large(self, limit: int) -> ApiError:
        return ApiError(413, "payload_too_large", f"at most {limit} bytes")

    def _read_body(self, limit: int) -> bytes:
        self._body_read = True
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return self._read_chunked(limit)
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        if not length.isdigit():
            self.close_connection = True
            raise invalid("Content-Length is malformed")
        n = int(length)
        if n > limit:
            self._drain(n)  # read on without keeping, so the answer can still be sent
            raise self._too_large(limit)
        return self.rfile.read(n) if n else b""

    def _read_chunked(self, limit: int) -> bytes:
        parts, total = [], 0
        try:
            while True:
                size = int(self.rfile.readline(1024).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(1024).strip():  # trailers
                        pass
                    break
                chunk = self.rfile.read(size)
                self.rfile.readline(8)
                total += len(chunk)
                if total <= limit:
                    parts.append(chunk)
                elif total > limit + DRAIN_BYTES:
                    self.close_connection = True
                    break
        except ValueError:
            self.close_connection = True
            raise invalid("the chunked body is malformed") from None
        if total > limit:
            raise self._too_large(limit)
        return b"".join(parts)

    def _drain(self, n: int) -> None:
        if n > DRAIN_BYTES:
            self.close_connection = True
            return
        while n > 0:
            got = self.rfile.read(min(n, 65536))
            if not got:
                break
            n -= len(got)

    def _serve(self) -> None:
        t = time.monotonic()
        self._body_read = False
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        res = self.backend.handle(Request(self.command, path, self.headers, self._read_body))
        try:
            self.send_response(res.status)
            for k, v in res.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(res.body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(res.body)
            self.wfile.flush()
            length = self.headers.get("Content-Length") or ""
            if not self._body_read and length.isdigit():  # an error before the body was read: read it on and drop it
                self._drain(int(length))
        except OSError:
            self.close_connection = True
        self.backend.log(f"{self.command} {self.path} {res.status} {round((time.monotonic() - t) * 1000)}ms")

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _serve


class Server(ThreadingHTTPServer):
    daemon_threads = True  # a stuck connection never keeps the process from stopping
    request_queue_size = 128

    def __init__(self, host: str, port: int, backend: Backend):
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), type("BoundHandler", (Handler,), {"backend": backend}))
