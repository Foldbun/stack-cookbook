"""Validation against the public API description (harness/vendor/openapi.yaml)."""

import functools
from pathlib import Path

import jsonschema
import yaml

SPEC_PATH = Path(__file__).parent / "vendor" / "openapi.yaml"

REQUEST_SCHEMAS = {
    "/v1/segment": "SegmentRequest",
    "/v1/refine": "RefineRequest",
    "/v1/ocr": "OcrRequest",
    "/v1/documents/parse": "ParseRequest",
    "/v1/embeddings": "EmbedRequest",
    "/v1/remove-background": "RemoveBackgroundRequest",
}
RESPONSE_SCHEMAS = {
    "/v1/images": "ImageInfo",
    "/v1/segment": "SegmentResponse",
    "/v1/refine": "SegmentResponse",
    "/v1/ocr": "OcrResponse",
    "/v1/documents/parse": "ParseResponse",
    "/v1/embeddings": "EmbedResponse",
    "/v1/remove-background": "RemoveBackgroundResponse",
}
BATCH_REQUEST = {"segment": "SegmentBatchRequest"}  # the others are plain BatchRequest
EVENT_SCHEMAS = {"progress": "BatchProgress", "item": "BatchItem", "summary": "BatchSummary"}


@functools.cache
def spec():
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


@functools.cache
def _validator(pointer):
    root = dict(spec())
    root["$ref"] = pointer
    return jsonschema.Draft202012Validator(root)


def errors(pointer, instance):
    """Validation errors of `instance` against the schema at JSON pointer `pointer` in the spec."""
    return [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message[:300]}" for e in _validator(pointer).iter_errors(instance)]


def schema(name):
    return f"#/components/schemas/{name}"


MODELS_RESPONSE = "#/paths/~1v1~1models/get/responses/200/content/application~1json/schema"
WHOAMI_RESPONSE = "#/paths/~1v1~1whoami/get/responses/200/content/application~1json/schema"


def request_errors(path, body):
    if path.startswith("/v1/batch/"):
        endpoint = path[len("/v1/batch/") :]
        out = errors(schema(BATCH_REQUEST.get(endpoint, "BatchRequest")), body)
        single = REQUEST_SCHEMAS.get("/v1/" + endpoint)
        if not out and single and endpoint != "segment":
            for i, item in enumerate(body.get("items", [])):
                full = dict(item)
                if "model" in body:
                    full["model"] = body["model"]
                out += [f"items[{i}] {e}" for e in errors(schema(single), full)]
                if "model" in item:
                    out.append(f"items[{i}]: an item must not contain model")
        return out
    name = REQUEST_SCHEMAS.get(path)
    return errors(schema(name), body) if name else []


# Known inconsistencies of the published description, tolerated so the fake can answer like the real API.
KNOWN_SPEC_ISSUES = [
    # CallMeta lists `image` as required, but its description says it is absent for text-only embeddings.
    ("/v1/embeddings", "(root): 'image' is a required property"),
]


def response_errors(path, status, body):
    out = _response_errors(path, status, body)
    return [e for e in out if (path, e) not in KNOWN_SPEC_ISSUES and ("/v1/" + path[len("/v1/batch/") :], e.removeprefix("result ")) not in KNOWN_SPEC_ISSUES]


def _response_errors(path, status, body):
    if status >= 400:
        return errors(schema("Error"), body)
    if path == "/v1/models":
        return errors(MODELS_RESPONSE, body)
    if path == "/v1/whoami":
        return errors(WHOAMI_RESPONSE, body)
    name = RESPONSE_SCHEMAS.get(path)
    if path.startswith("/v1/batch/"):
        return errors(schema("BatchResponse"), body)
    return errors(schema(name), body) if name else [f"no response schema for {path}"]


def event_errors(path, event, data):
    name = EVENT_SCHEMAS.get(event)
    if not name:
        return [f"unknown event {event!r}"]
    out = errors(schema(name), data)
    if event == "item" and data.get("status") == "ok":
        single = "/v1/" + path[len("/v1/batch/") :]
        out += [f"result {e}" for e in errors(schema(RESPONSE_SCHEMAS[single]), data.get("result")) if (single, e) not in KNOWN_SPEC_ISSUES]
    return out
