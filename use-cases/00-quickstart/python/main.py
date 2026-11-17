"""First call to the Foldbun Stack API: read the text of an image and see what the call is worth. Needs no key."""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import stack_client as sc

EXIT_FAILED, EXIT_NO_KEY, EXIT_TIER, EXIT_BUDGET = 1, 2, 3, 4


def read_text(client, item, base):
    data = (base / item["image"]).read_bytes()
    content_type = "image/webp" if item["image"].endswith(".webp") else "image/png" if item["image"].endswith(".png") else "image/jpeg"
    # region image-input
    if client.free or item.get("imageInput") == "base64":
        image = sc.image_base64(data)  # inline: simplest for a single call, and the only form the free tier takes
    else:
        uploaded = client.upload(data, content_type)  # needs a key; the handle can be reused for 7 days at most
        image = sc.image_handle(uploaded["id"])
    # endregion
    # region first-call
    result = client.post("/v1/ocr", {"model": "ppocr-v6", "image": image}, tag=item["id"])
    for line in result["lines"]:
        print(f"  {line['confidence']:.2f}  {line['text']}")
    price = result["billing"]["total"]  # list price in credits
    print(f"  list price {price} credits (free tier, not charged)" if client.free else f"  list price {price} credits, charged {result['cost']}")
    # endregion
    lines = [{"text": ln["text"], "confidence": ln["confidence"], "bbox": ln["bbox"]} for ln in result["lines"]]
    return {"image": {"width": result["image"]["width"], "height": result["image"]["height"]}, "lines": lines}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=Path(__file__).parent.parent / "assets/smoke/manifest.json")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--resume", action="store_true", help="accepted for the common interface; every call here is cheap")
    args = ap.parse_args()
    manifest = json.loads(args.manifest.read_text())
    started, t0 = datetime.now(UTC), time.monotonic()
    # region client
    # No FOLDBUN_API_KEY: the free tier, with the Cookbook's shared project (FOLDBUN_PROJECT_ID to use your own).
    client = sc.StackClient(max_concurrency=args.concurrency)  # FOLDBUN_API_KEY, FOLDBUN_PROJECT_ID, FOLDBUN_API_BASE
    try:
        who = client.whoami()  # shows who is calling: a project (free tier) or an account (API key)
    except sc.StackError as err:
        print(f"{err.status} {err.code}: {err.message}", file=sys.stderr)
        return EXIT_NO_KEY if err.code == "missing_api_key" else EXIT_FAILED
    print(f"free tier, project {who['project']['id']}" if client.free else f"key accepted; balance {who['balance']} credits")
    # endregion
    items, code = [], 0
    for item in manifest["items"][: args.limit]:
        print(item["id"])
        try:
            output, status, error = read_text(client, item, args.manifest.parent), "ok", None
        except sc.StackError as err:
            print(f"  {err.status} {err.code}: {err.message}" + (f" (request {err.request_id})" if err.request_id else ""), file=sys.stderr)
            if err.code in ("model_locked", "budget_exceeded"):
                code = EXIT_TIER if err.code == "model_locked" else EXIT_BUDGET
                break
            output, status, error = None, "error", {"status": err.status, "code": err.code, "message": err.message}
        entry = {"id": item["id"], "status": status, "calls": client.calls(item["id"]), "output": output}
        if error:
            entry["error"] = error
        items.append(entry)
    result = {
        "usecase": manifest["usecase"],
        "language": "python",
        "client": f"stack_client {sc.VERSION}",
        "startedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsedMs": int((time.monotonic() - t0) * 1000),
        "items": items,
        "ledger": client.summary(),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
