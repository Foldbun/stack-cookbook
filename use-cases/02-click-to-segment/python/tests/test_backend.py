# ruff: noqa: E501
"""The backend against a stand-in for the Stack client (no network, no key).

Run from the python/ directory of this use case, inside the cookbook repository:

    uv run --with pytest pytest tests
"""

import hashlib
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import stack_client as sc  # noqa: E402
from backend import Backend, RateLimit, Server, from_stack  # noqa: E402


class FakeStack:
    def __init__(self):
        self.uploads = 0
        self.bodies = []
        self.fail = None
        self.gate = None  # when set, post() waits for it
        self.entered = threading.Event()
        self.upload_gate = None
        self.rows = []
        self._lock = threading.Lock()

    def upload(self, data, content_type="image/jpeg"):
        with self._lock:
            self.uploads += 1
        if self.upload_gate:
            self.upload_gate.wait(10)
        return {"id": "img_" + "a" * 64, "width": 100, "height": 80}

    def post(self, path, body, idempotency_key=None, *, tag=None):
        self.entered.set()
        if self.gate:
            self.gate.wait(10)
        if self.fail:
            raise self.fail
        with self._lock:
            self.bodies.append(body)
            cache = "miss" if len(self.bodies) == 1 else "hit"
            list_price = 0.21 if cache == "miss" else 0.01
            self.rows.append(
                (
                    tag,
                    {
                        "endpoint": path,
                        "model": body["model"],
                        "list": list_price,
                        "charged": 0,
                        "cache": cache,
                        "queueMs": 4,
                        "processingMs": 60,
                        "replayed": False,
                    },
                )
            )
        polygon = [{"outer": [10, 10, 50, 10, 50, 40, 10, 40]}]
        return {
            "model": body["model"],
            "cost": 0,
            "cache": cache,
            "billing": {"total": list_price},
            "instances": [{"score": 0.9, "bbox": [10, 10, 51, 41], "area": 1271, "polygon": polygon}],
        }

    def calls(self, tag=None):
        with self._lock:
            return [dict(r) for t, r in self.rows if tag is None or t == tag]


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def static_dir(tmp_path):
    d = tmp_path / "dist"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>page</title>")
    (d / "app.js").write_text("console.log(1)")
    (d / "style.css").write_text("body{}")
    (d / "logo.svg").write_text("<svg/>")
    (d / "pic.png").write_bytes(b"\x89PNG")
    (d / "pic.jpg").write_bytes(b"\xff\xd8\xff")
    (d / "pic.webp").write_bytes(b"RIFF")
    (d / "data.bin").write_bytes(b"x")
    (d / "m.mjs").write_text("export {}")
    (d / "d.json").write_text("{}")
    (d / "e.jpeg").write_bytes(b"\xff\xd8\xff")
    (d / "f.ico").write_bytes(b"ico")
    (d / ".secret").write_text("hidden")
    (d / "sub").mkdir()
    (d / "sub" / "inner.html").write_text("inner")
    (tmp_path / "outside.txt").write_text("outside")
    (d / "link.txt").symlink_to(tmp_path / "outside.txt")
    return d


class Api:
    def __init__(self, port):
        self.port = port

    def request(self, method, path, session=None, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = dict(headers or {})
        if session:
            h["X-Session-Id"] = session
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        return res.status, raw, res

    def json(self, method, path, session=None, body=None, headers=None):
        status, raw, res = self.request(method, path, session, body, headers)
        return status, json.loads(raw), res

    def upload(self, session, salt=1, data=None):
        return self.json("POST", "/api/images", session, data if data is not None else bytes([0xFF, 0xD8, 0xFF, salt]), {"Content-Type": "image/jpeg"})

    def refine(self, session, body):
        return self.json("POST", "/api/refine", session, json.dumps(body), {"Content-Type": "application/json"})


@pytest.fixture
def stack():
    return FakeStack()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def api(stack, clock, static_dir):
    backend = Backend(stack, now=clock, log=lambda line: None, static_dir=static_dir)
    server = Server("127.0.0.1", 0, backend)
    thread = threading.Thread(target=lambda: server.serve_forever(0.02), daemon=True)
    thread.start()
    yield Api(server.server_address[1])
    server.shutdown()
    server.server_close()


def test_health_needs_no_session(api):
    status, body, _ = api.json("GET", "/api/health")
    assert status == 200
    assert body == {"status": "ok", "language": "python", "client": f"stackClient {sc.VERSION}"}


def test_every_other_route_needs_a_well_formed_session_id(api):
    assert api.json("GET", "/api/usage")[1]["error"]["code"] == "invalid_session"
    assert api.json("GET", "/api/usage", "bad id!")[1]["error"]["code"] == "invalid_session"
    assert api.json("GET", "/api/usage", "x" * 65)[1]["error"]["code"] == "invalid_session"
    assert api.json("GET", "/api/usage", "x" * 64)[0] == 200
    assert api.json("GET", "/api/nothing", "s")[1]["error"]["code"] == "not_found"
    assert api.json("GET", "/api/refine", "s")[0] == 405
    assert api.json("POST", "/api/health")[0] == 405


def test_an_image_is_uploaded_to_stack_once_per_content_in_any_session(api, stack):
    s1, a, _ = api.upload("up-1")
    s2, b, _ = api.upload("up-2")
    assert (s1, s2) == (200, 200)
    assert a == b
    assert a["imageId"] == "im_" + hashlib.sha256(bytes([0xFF, 0xD8, 0xFF, 1])).hexdigest()[:32]
    assert (a["width"], a["height"]) == (100, 80)
    assert stack.uploads == 1
    status, err, _ = api.json("POST", "/api/images", "up-1", "x", {"Content-Type": "text/plain"})
    assert (status, err["error"]["code"]) == (415, "unsupported_media_type")
    assert api.json("POST", "/api/images", "up-1", b"", {"Content-Type": "image/png"})[1]["error"]["code"] == "invalid_request"
    assert api.json("GET", "/api/usage", "up-1")[1]["uploads"] == 1


def test_concurrent_uploads_of_the_same_bytes_make_one_stack_upload(api, stack):
    stack.upload_gate = threading.Event()
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(api.upload(f"conc-{i}", data=b"\xff\xd8\xff-same"))) for i in range(6)]
    for t in threads:
        t.start()
    stack.upload_gate.set()
    for t in threads:
        t.join(15)
    assert [r[0] for r in results] == [200] * 6
    assert len({r[1]["imageId"] for r in results}) == 1
    assert stack.uploads == 1


def test_a_failed_upload_is_not_cached(api, stack):
    def boom(data, content_type="image/jpeg"):
        raise sc.StackError(0, "network_error", "reset", True)

    good = stack.upload
    stack.upload = boom
    status, body, _ = api.upload("fail", 9)
    stack.upload = good
    assert (status, body["error"]["code"], body["error"]["upstreamCode"]) == (503, "upstream_busy", "network_error")
    assert api.refine("fail", {"imageId": "im_" + "0" * 32, "points": [{"x": 1, "y": 1}]})[0] == 404
    assert api.upload("fail", 9)[0] == 200


def test_the_image_cache_keeps_at_most_256_images():
    backend = Backend(FakeStack(), log=lambda line: None)
    import backend as module

    class Req:
        headers = {"Content-Type": "image/png"}

        def __init__(self, n):
            self.data = n.to_bytes(4, "big")

        def read_body(self, limit):
            return self.data

    s = backend._session("many")
    for i in range(module.MAX_IMAGES + 5):
        backend._upload_image(Req(i), s)
        s.upload.tokens = 5
    assert len(backend._images) == module.MAX_IMAGES


def test_refine_sends_all_points_with_explicit_positive_and_returns_polygon_cost_and_usage(api, stack):
    img = api.upload("click", 2)[1]
    points = [{"x": 20, "y": 20}, {"x": 70, "y": 60, "positive": False}]
    status, r, _ = api.refine("click", {"imageId": img["imageId"], "points": points})
    assert status == 200
    assert stack.bodies[0] == {
        "model": "sam-lite",
        "image": {"id": "img_" + "a" * 64},
        "points": [{"x": 20, "y": 20, "positive": True}, {"x": 70, "y": 60, "positive": False}],
    }
    assert r["polygon"] == [{"outer": [10, 10, 50, 10, 50, 40, 10, 40]}]
    assert (r["cache"], r["listCredits"], r["costCredits"], r["bbox"], r["area"], r["score"], r["model"]) == (
        "miss",
        0.21,
        0,
        [10, 10, 51, 41],
        1271,
        0.9,
        "sam-lite",
    )
    assert isinstance(r["latencyMs"], int)
    assert set(r) == {"polygon", "bbox", "area", "score", "model", "costCredits", "listCredits", "cache", "latencyMs"}
    api.refine("click", {"imageId": img["imageId"], "points": points, "box": [1, 2, 30, 40]})
    assert stack.bodies[1]["box"] == [1, 2, 30, 40]
    usage = api.json("GET", "/api/usage", "click")[1]
    assert (usage["clicks"], usage["uploads"], usage["listCredits"], usage["cacheHits"], usage["cacheMisses"]) == (2, 1, 0.22, 1, 1)
    assert len(usage["calls"]) == 2 and usage["sessionId"] == "click"
    fresh = api.json("GET", "/api/usage", "fresh")[1]
    assert fresh["calls"] == [] and fresh["clicks"] == 0 and fresh["costCredits"] == 0


def test_refine_checks_the_request_before_calling_stack(api, stack):
    img = api.upload("check", 3)[1]
    iid = img["imageId"]
    cases = [
        ({"imageId": "im_" + "0" * 32, "points": [{"x": 1, "y": 1}]}, "image_not_found"),
        ({"imageId": iid, "points": [{"x": 1, "y": 1, "positive": False}]}, "invalid_request"),
        ({"imageId": iid, "points": [{"x": 101, "y": 1}]}, "invalid_request"),
        ({"imageId": iid, "points": [{"x": -1, "y": 1}]}, "invalid_request"),
        ({"imageId": iid, "points": [{"x": True, "y": 1}]}, "invalid_request"),
        ({"imageId": iid, "points": [{"x": 1, "y": 1}] * 21}, "invalid_request"),
        ({"imageId": iid, "points": [], "box": [10, 10, 5, 20]}, "invalid_request"),
        ({"imageId": iid, "points": [], "box": [0, 0, 101, 20]}, "invalid_request"),
        ({"imageId": iid, "points": [], "box": [0, 0, 10]}, "invalid_request"),
        ({"imageId": iid, "points": [{"x": 1, "y": 1}], "model": "sam3"}, "model_not_allowed"),
        ({"imageId": iid, "points": [{"x": 1, "y": 1}], "extra": 1}, "invalid_request"),
        ({"imageId": "nope", "points": [{"x": 1, "y": 1}]}, "invalid_request"),
        ({"points": [{"x": 1, "y": 1}]}, "invalid_request"),
        ([], "invalid_request"),
    ]
    for i, (body, code) in enumerate(cases):  # a session of its own each: the burst limit counts invalid requests too
        status, r, _ = api.refine(f"check-{i}", body)
        assert r["error"]["code"] == code, body
        assert status == (404 if code == "image_not_found" else 400)
    assert api.json("POST", "/api/refine", "check", "not json", {"Content-Type": "application/json"})[1]["error"]["code"] == "invalid_request"
    assert api.json("POST", "/api/refine", "check", '{"imageId": NaN}', {"Content-Type": "application/json"})[1]["error"]["code"] == "invalid_request"
    assert stack.bodies == []
    # the box alone is enough, and the edge of the image is inside it
    assert api.refine("check", {"imageId": iid, "points": [], "box": [0, 0, 100, 80]})[0] == 200
    assert api.refine("check", {"imageId": iid, "points": [{"x": 100, "y": 80}]})[0] == 200


def test_a_request_body_over_the_limit_is_413(api, stack):
    status, body, _ = api.json("POST", "/api/refine", "big", b"x" * (64 * 1024 + 1), {"Content-Type": "application/json"})
    assert (status, body["error"]["code"]) == (413, "payload_too_large")
    big = b"\xff\xd8\xff" + b"0" * (20 * 1024 * 1024)
    status, body, _ = api.upload("big", data=big)
    assert (status, body["error"]["code"]) == (413, "payload_too_large")
    assert stack.uploads == 0


def test_a_rejected_request_consumes_a_rate_limit_token(api, stack):
    iid = api.upload("tokens-img", 6)[1]["imageId"]
    bad = lambda body: api.json("POST", "/api/refine", "tokens", body, {"Content-Type": "application/json"})
    statuses = [bad(b"not json")[0] for _ in range(8)]
    statuses += [bad(b"x" * (64 * 1024 + 1))[0], bad(json.dumps({"imageId": iid, "points": []}))[0]]
    assert statuses == [400] * 8 + [413, 400]
    assert bad(b"not json")[1]["error"]["code"] == "rate_limited"
    wrong = lambda ctype, body: api.json("POST", "/api/images", "tokens-up", body, {"Content-Type": ctype})
    ups = [
        wrong("text/plain", b"x")[0],
        wrong("image/png", b"")[0],
        wrong("image/png", b"\0" * (20 * 1024 * 1024 + 1))[0],
        wrong("text/plain", b"x")[0],
        wrong("text/plain", b"x")[0],
    ]
    assert ups == [415, 400, 413, 415, 415]
    assert wrong("text/plain", b"x")[1]["error"]["code"] == "rate_limited"


def test_an_empty_image_body_is_400_and_not_sent_to_stack(api, stack):
    status, body, _ = api.json("POST", "/api/images", "empty", b"", {"Content-Type": "image/jpeg"})
    assert (status, body["error"]["code"]) == (400, "invalid_request")
    assert stack.uploads == 0


def test_model_null_means_not_given(api, stack):
    iid = api.upload("null-model", 7)[1]["imageId"]
    status, _, _ = api.refine("null-model", {"imageId": iid, "points": [{"x": 5, "y": 5}], "model": None})
    assert status == 200 and stack.bodies[-1]["model"] == "sam-lite"


def test_the_allow_list_comes_from_the_models_option(stack):
    backend = Backend(stack, models=["sam3", "sam-lite"], log=lambda line: None)
    server = Server("127.0.0.1", 0, backend)
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    api = Api(server.server_address[1])
    iid = api.upload("m", 4)[1]["imageId"]
    assert api.refine("m", {"imageId": iid, "points": [{"x": 1, "y": 1}]})[0] == 200
    assert stack.bodies[0]["model"] == "sam3"  # the first is the default
    assert api.refine("m", {"imageId": iid, "points": [{"x": 1, "y": 1}], "model": "sam-lite"})[0] == 200
    assert api.refine("m", {"imageId": iid, "points": [{"x": 1, "y": 1}], "model": "other"})[1]["error"]["code"] == "model_not_allowed"
    server.shutdown()
    server.server_close()


def test_a_session_is_limited_to_a_burst_of_10_refines_with_retry_after(api, clock):
    iid = api.upload("burst", 5)[1]["imageId"]
    body = {"imageId": iid, "points": [{"x": 5, "y": 5}]}
    for _ in range(10):
        assert api.refine("burst", body)[0] == 200
    status, r, res = api.refine("burst", body)
    assert status == 429
    assert (r["error"]["code"], r["error"]["retryable"], r["error"]["retryAfterMs"]) == ("rate_limited", True, 1000)
    assert res.getheader("Retry-After") == "1"
    assert api.refine("other-session", {"imageId": iid, "points": [{"x": 5, "y": 5}]})[0] == 200  # per session
    clock.now += 1000
    assert api.refine("burst", body)[0] == 200


def test_uploads_are_limited_to_a_burst_of_5(api, clock):
    for i in range(5):
        assert api.upload("ups", i)[0] == 200
    status, r, res = api.upload("ups", 99)
    assert (status, r["error"]["code"], r["error"]["retryAfterMs"], res.getheader("Retry-After")) == (429, "rate_limited", 6000, "6")


def test_idle_sessions_are_dropped_after_30_minutes(api, clock):
    iid = api.upload("idle", 6)[1]["imageId"]
    api.refine("idle", {"imageId": iid, "points": [{"x": 5, "y": 5}]})
    clock.now += 30 * 60_000 + 1
    usage = api.json("GET", "/api/usage", "idle")[1]
    assert usage["clicks"] == 0 and usage["uploads"] == 0 and usage["calls"] == []


def test_there_are_at_most_1000_sessions_and_the_one_idle_longest_goes_first(stack, clock):
    backend = Backend(stack, now=clock, log=lambda line: None)
    for i in range(1001):
        clock.now += 1
        backend._session(f"s{i}")
    assert len(backend._sessions) == 1000
    assert "s0" not in backend._sessions and "s1" in backend._sessions


def test_stack_errors_map_to_upstream_busy_upstream_refused_and_credit_cap(api, stack):
    iid = api.upload("errors", 7)[1]["imageId"]
    stack.fail = sc.StackError(403, "model_locked", "model sam-lite is not available to this account", False, 0, "req_1")
    status, r, _ = api.refine("errors", {"imageId": iid, "points": [{"x": 5, "y": 5}]})
    assert status == 502
    assert r["error"] == {
        "code": "upstream_refused",
        "message": "Foldbun Stack refused the call",
        "retryable": False,
        "retryAfterMs": 0,
        "upstreamCode": "model_locked",
        "requestId": "req_1",
    }
    stack.fail = sc.StackError(503, "overloaded", "busy", True, 2500, "req_2")
    status, r, res = api.refine("errors", {"imageId": iid, "points": [{"x": 5, "y": 5}]})
    assert (status, r["error"]["code"], r["error"]["retryAfterMs"], res.getheader("Retry-After")) == (503, "upstream_busy", 2500, "3")
    stack.fail = sc.BudgetExceeded("cap")
    status, r, _ = api.refine("errors", {"imageId": iid, "points": [{"x": 5, "y": 5}]})
    assert (status, r["error"]["code"], r["error"]["retryable"]) == (503, "credit_cap", False)
    stack.fail = RuntimeError("bug")
    status, r, _ = api.refine("errors", {"imageId": iid, "points": [{"x": 5, "y": 5}]})
    assert (status, r["error"]["code"]) == (500, "internal") and "bug" not in json.dumps(r)
    stack.fail = None
    assert api.json("GET", "/api/usage", "errors")[1]["clicks"] == 0


def test_from_stack_mapping():
    busy = from_stack(sc.StackError(504, "upstream_timeout", "slow", True, 0))
    assert (busy.status, busy.code, busy.retryable, busy.retry_after_ms, busy.upstream_code) == (503, "upstream_busy", True, 1000, "upstream_timeout")
    assert from_stack(sc.StackError(0, "network_error", "reset", True)).upstream_code == "network_error"
    assert from_stack(sc.StackError(402, "insufficient_balance", "no", False)).status == 502
    cap = from_stack(sc.BudgetExceeded("cap"))
    assert (cap.status, cap.code, cap.retryable) == (503, "credit_cap", False)


def test_rate_limit_refills_at_per_minute():
    bucket = RateLimit(60, 2, 0)
    assert bucket.take(0) == 0
    assert bucket.take(0) == 0
    assert bucket.take(0) == 1000
    assert bucket.take(500) == 500
    assert bucket.take(1000) == 0


def test_a_slow_stack_call_does_not_block_health_or_other_sessions(api, stack):
    iid = api.upload("slow", 8)[1]["imageId"]
    stack.gate = threading.Event()
    result = []
    t = threading.Thread(target=lambda: result.append(api.refine("slow", {"imageId": iid, "points": [{"x": 5, "y": 5}]})))
    t.start()
    assert stack.entered.wait(10)
    assert api.json("GET", "/api/health")[0] == 200
    assert api.json("GET", "/api/usage", "someone")[0] == 200
    assert api.upload("other", 10)[0] == 200
    stack.gate.set()
    t.join(15)
    assert result[0][0] == 200


def test_concurrent_clicks_keep_every_session_consistent(api, stack):
    iid = api.upload("shared", 11)[1]["imageId"]
    results = {}

    def work(n):
        for _ in range(5):
            assert api.refine(f"w{n}", {"imageId": iid, "points": [{"x": 5, "y": 5}]})[0] == 200
        results[n] = api.json("GET", "/api/usage", f"w{n}")[1]

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(results) == 8
    assert all(u["clicks"] == 5 and len(u["calls"]) == 5 for u in results.values())
    assert len(stack.bodies) == 40


def test_static_files_are_served_with_type_and_no_cache(api):
    status, raw, res = api.request("GET", "/")
    assert (status, raw) == (200, b"<!doctype html><title>page</title>")
    assert res.getheader("Content-Type") == "text/html; charset=utf-8"
    assert res.getheader("Cache-Control") == "no-cache"
    assert res.getheader("Access-Control-Allow-Origin") is None
    expected = {
        "index.html": "text/html",
        "app.js": "text/javascript",
        "style.css": "text/css",
        "logo.svg": "image/svg+xml",
        "pic.png": "image/png",
        "pic.jpg": "image/jpeg",
        "pic.webp": "image/webp",
        "m.mjs": "text/javascript; charset=utf-8",
        "d.json": "application/json",
        "e.jpeg": "image/jpeg",
        "f.ico": "image/x-icon",
    }
    for name, content_type in expected.items():
        status, raw, res = api.request("GET", "/" + name + "?v=1")
        assert status == 200 and res.getheader("Content-Type").startswith(content_type), name
        assert res.getheader("Cache-Control") == "no-cache"
        assert res.getheader("X-Content-Type-Options") == "nosniff"
    assert api.request("GET", "/data.bin")[2].getheader("Content-Type") == "application/octet-stream"
    status, raw, res = api.request("HEAD", "/app.js")
    assert (status, raw, res.getheader("Content-Length")) == (200, b"", "14")
    assert api.request("GET", "/%61pp.js")[0] == 200  # a percent-encoded plain name is the same file


@pytest.mark.parametrize(
    "path",
    [
        "/missing.html",
        "/sub/inner.html",
        "/sub",
        "/sub/",
        "/../outside.txt",
        "/%2e%2e/outside.txt",
        "/..%2foutside.txt",
        "/%2e%2e%2foutside.txt",
        "/sub/%2e%2e/index.html",
        "/..%5coutside.txt",
        "/link.txt",
        "/.secret",
        "/%2esecret",
        "/index.html%00.png",
        "//etc/passwd",
        "/%ff",
    ],
)
def test_static_refuses_everything_outside_the_directory(api, path):
    status, raw, _ = api.request("GET", path)
    assert status == 404, path
    assert json.loads(raw)["error"]["code"] == "not_found"


def test_static_never_serves_the_api_and_only_accepts_get(api, static_dir):
    (static_dir / "api").mkdir()
    (static_dir / "api" / "usage").write_text("file")
    assert api.json("GET", "/api/usage", "s")[0] == 200
    assert api.json("GET", "/api/index.html", "s")[1]["error"]["code"] == "not_found"
    assert api.json("GET", "/api/", "s")[1]["error"]["code"] == "not_found"
    assert api.json("GET", "/api")[1]["error"]["code"] == "not_found"
    assert api.request("HEAD", "/missing.js")[0] == 404
    # any other method outside /api is 405 with Allow, whether or not the name exists
    for path in ("/", "/app.js", "/missing.js", "/sub", "/.secret"):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            status, raw, res = api.request(method, path, None, b"x" if method in ("POST", "PUT", "PATCH") else None)
            assert (status, res.getheader("Allow"), json.loads(raw)["error"]["code"]) == (405, "GET, HEAD", "method_not_allowed"), (method, path)


def test_a_missing_static_directory_is_not_found(stack, tmp_path):
    for static in (tmp_path / "nowhere", None):
        server = Server("127.0.0.1", 0, Backend(stack, static_dir=static, log=lambda line: None))
        threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
        api = Api(server.server_address[1])
        for path in ("/", "/index.html"):
            status, body, _ = api.json("GET", path)
            assert (status, body["error"]["code"]) == (404, "not_found")
        status, raw, res = api.request("POST", "/")
        assert (status, res.getheader("Allow"), json.loads(raw)["error"]["code"]) == (405, "GET, HEAD", "method_not_allowed")
        assert api.json("GET", "/api/health")[0] == 200
        server.shutdown()
        server.server_close()


def run_main(*args, env=None, **kw):
    full_env = {k: v for k, v in os.environ.items() if k not in ("FOLDBUN_API_KEY", "FOLDBUN_API_BASE")}
    full_env.update(env or {})
    return subprocess.Popen(
        [sys.executable, str(HERE / "main.py"), *args], env=full_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=HERE, **kw
    )


def test_main_without_a_key_exits_2_before_listening():
    p = run_main("--port", "0")
    out, err = p.communicate(timeout=20)
    assert p.returncode == 2
    assert "FOLDBUN_API_KEY" in err and "listening" not in out


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_main_listens_serves_health_and_stops_on_signal(sig):
    p = run_main("--port", "0", "--static", "no-such-dir", env={"FOLDBUN_API_KEY": "fbs_test", "SEGMENTER_MODELS": "sam-lite, sam3"})
    try:
        line = p.stdout.readline()
        assert line.startswith("listening on http://127.0.0.1:")
        port = int(line.strip().rsplit(":", 1)[1])
        assert Api(port).json("GET", "/api/health")[0] == 200
        assert Api(port).request("GET", "/")[0] == 404  # the directory does not exist
        p.send_signal(sig)
        assert p.wait(timeout=5) == 0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        p.stdout.close()
        p.stderr.close()
