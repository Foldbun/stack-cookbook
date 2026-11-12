"""Every endpoint of the fake API answers in the shape of harness/vendor/openapi.yaml (the published API description).

The `server` fixture runs the fake with validation on: each request, response and event is checked against the
spec, and the fixture fails the test on any violation. These tests drive every endpoint and the error paths."""

import base64
import json
import urllib.error
import urllib.request

import pytest
from conftest import fake_log, png

from harness import openapi
from harness.fakeapi import FakeServer


def call(srv, method, path, body=None, key="fbs_fake", headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(srv.url + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def upload(srv, salt=0, key="fbs_fake"):
    status, _, body = call(srv, "POST", "/v1/images", raw=png(salt), key=key, headers={"Content-Type": "image/png"})
    assert status in (200, 201)
    return json.loads(body)["id"]


def test_validator_catches_bad_shapes():
    assert openapi.errors(openapi.schema("OcrResponse"), {"lines": []})
    assert openapi.errors(openapi.schema("Error"), {"error": {"code": "x"}})
    assert openapi.request_errors("/v1/segment", {"model": "sam3", "image": {"id": "img_bad"}, "prompts": []})
    assert openapi.request_errors("/v1/batch/ocr", {"items": [{"image": {"id": "img_" + "0" * 64}, "model": "ppocr-v6"}]})


def test_models(server):
    status, headers, body = call(server, "GET", "/v1/models")
    assert status == 200 and len(headers["X-Request-Id"]) == 16
    assert {m["id"] for m in json.loads(body)["data"]} == {"sam3", "sam-lite", "ppocr-v6", "paddleocr-vl", "dinov3", "siglip2", "birefnet"}


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/v1/segment",
            {
                "model": "sam3",
                "prompts": [{"text": "cat", "group": "pets"}, {"box": [2, 2, 30, 30]}, {"box": [1, 1, 5, 5], "positive": False}, {"point": {"x": 10, "y": 10}}],
            },
        ),
        ("/v1/segment", {"model": "sam-lite", "prompts": [{"box": [2, 2, 30, 30]}, {"point": {"x": 10, "y": 10}}]}),
        ("/v1/refine", {"model": "sam-lite", "points": [{"x": 10, "y": 10}, {"x": 40, "y": 30, "positive": False}]}),
        ("/v1/refine", {"box": [3, 3, 40, 40]}),
        ("/v1/ocr", {"options": {"words": False, "minConfidence": 0.2}}),
        ("/v1/ocr", {}),
        ("/v1/documents/parse", {"options": {"blocks": True}}),
        ("/v1/documents/parse", {"options": {"blocks": False}}),
        ("/v1/embeddings", {"model": "dinov3"}),
        ("/v1/embeddings", {"model": "siglip2"}),
        ("/v1/remove-background", {"options": {"output": "matte", "size": 64}}),
    ],
)
def test_inference_endpoints(server, path, body):
    for image in ({"id": upload(server)}, {"base64": base64.b64encode(png()).decode()}):
        status, headers, raw = call(server, "POST", path, dict(body, image=image), headers={"Idempotency-Key": f"k-{path}-{len(image)}-{list(image)[0]}"})
        assert status == 200, raw
        out = json.loads(raw)
        assert out["cost"] == out["billing"]["total"] if out["billing"]["charged"] else out["cost"] == 0


def test_text_embeddings(server):
    status, _, raw = call(server, "POST", "/v1/embeddings", {"model": "siglip2", "texts": ["a red car", "a dog"]})
    out = json.loads(raw)
    assert status == 200 and [e["index"] for e in out["embeddings"]] == [0, 1] and "image" not in out


@pytest.mark.parametrize(
    "endpoint,item,model",
    [
        ("segment", {"prompts": [{"text": "cat"}]}, "sam3"),
        ("ocr", {}, None),
        ("documents/parse", {}, None),
        ("embeddings", {}, "dinov3"),
        ("remove-background", {"options": {"size": 64}}, None),
    ],
)
@pytest.mark.parametrize("accept", ["text/event-stream", "application/json"])
def test_batch_endpoints(server, endpoint, item, model, accept):
    body = {"items": [dict(item, image={"id": upload(server, i)}) for i in range(2)]}
    if model:
        body["model"] = model
    status, headers, raw = call(server, "POST", f"/v1/batch/{endpoint}", body, headers={"Accept": accept, "Idempotency-Key": "batch-1"})
    assert status == 200
    if accept == "application/json":
        assert json.loads(raw)["summary"]["counts"]["ok"] == 2
    else:
        assert headers["Content-Type"] == "text/event-stream" and raw.decode().count("event: item") == 2


def test_error_envelopes(server):
    h = upload(server)
    assert call(server, "GET", "/v1/models", key="nope")[0] == 401
    assert call(server, "GET", "/v1/nothing")[0] == 404
    assert call(server, "POST", "/v1/ocr", {"image": {"url": "https://example.com/a.png"}})[0] == 400
    assert call(server, "POST", "/v1/ocr", {"image": {"id": "img_" + "1" * 64}})[0] == 404
    assert call(server, "POST", "/v1/ocr", raw=b"{not json")[0] == 400
    assert call(server, "POST", "/v1/images", raw=b"not an image", headers={"Content-Type": "image/png"})[0] == 400
    status, _, raw = call(server, "POST", "/v1/segment", {"model": "sam3", "image": {"id": h}, "prompts": [{"text": "cat"}]}, key="fbs_fake_public")
    assert status == 403 and json.loads(raw)["error"]["code"] == "model_locked"
    server.fake.reset({"rules": [{"path": "/v1/ocr", "error": {"code": "model_busy", "retryAfterMs": 2500}}]})
    status, headers, raw = call(server, "POST", "/v1/ocr", {"image": {"base64": base64.b64encode(png()).decode()}})
    assert status == 503 and headers["Retry-After"] == "3" and json.loads(raw)["error"]["retryAfterMs"] == 2500
    server.fake.reset({"balance": 0.1})
    h = upload(server)
    status, _, raw = call(server, "POST", "/v1/segment", {"model": "sam3", "image": {"id": h}, "prompts": [{"text": "cat"}]})
    assert status == 402
    assert call(server, "POST", "/v1/ocr", {"image": {"id": h}}, headers={"Idempotency-Key": "bad key"})[0] == 400


def test_invalid_request_is_rejected_and_reported():
    with FakeServer(validate=True) as srv:
        status, _, raw = call(srv, "POST", "/v1/segment", {"model": "sam3", "image": {"id": "img_x"}, "prompts": []})
        assert status == 400 and json.loads(raw)["error"]["code"] == "invalid_request"
        assert fake_log(srv)["violations"]  # the harness sees which implementation sent what


def test_record_mode_produces_replayable_fixtures(tmp_path):
    with FakeServer() as upstream, FakeServer(upstream=upstream.url, record=str(tmp_path)) as rec:
        status, _, raw = call(rec, "POST", "/v1/images", raw=png(), headers={"Content-Type": "image/png"})
        h = json.loads(raw)["id"]
        status, _, raw = call(rec, "POST", "/v1/ocr", {"image": {"id": h}, "options": {"words": False}})
        assert status == 200
        recorded = json.loads((tmp_path / "recorded.json").read_text())
    assert recorded[0]["image"] == h[4:] and "balance" not in recorded[0]["response"] and "id" not in recorded[0]["response"]
    recorded[0]["response"]["lines"][0]["text"] = "Recorded text"
    (tmp_path / "recorded.json").write_text(json.dumps(recorded))
    with FakeServer(fixtures=[str(tmp_path)], validate=True) as srv:
        h = upload(srv)
        status, _, raw = call(srv, "POST", "/v1/ocr", {"image": {"id": h}, "options": {"words": False}})
        assert json.loads(raw)["lines"][0]["text"] == "Recorded text"
        assert fake_log(srv)["violations"] == []
