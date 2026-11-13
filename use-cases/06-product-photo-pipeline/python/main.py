"""Turn marketplace photos into catalogue images: cut out the product, check the cut-out locally, compose the page.

    uv run python main.py --manifest <manifest.json> --out <dir> [--concurrency N] [--limit N] [--resume]

For every image the run calls `POST /v1/remove-background` (model `birefnet`, `cutout`, `webp`; 1 credit, no cache
discount), applies the four quality rules of pipeline.py and then either composes a 1200 x 1200 white-background WebP
or lists the photo in review.csv with the reason. Next to result.json it writes:

    catalogue/<id>.webp     the finished catalogue image (only for cut-outs that passed)
    review.csv              file, reasons and the measured numbers of every cut-out that did not pass
    alpha/<id>.png          the 8-bit alpha matte of every cut-out (what evaluate.py scores against the ground truth)
    cutouts/<id>.webp       the cut-outs as the API returned them, and state/<id>.json with the ledger rows: --resume reads these
"""

import argparse
import base64
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pipeline
import stack_client as sc

EXIT_FAILED, EXIT_NO_KEY, EXIT_TIER, EXIT_BUDGET = 1, 2, 3, 4
CREDITS_PER_IMAGE = 1  # list price of /v1/remove-background; it is not discounted when the cache is warm
REVIEW_COLUMNS = ["file", "id", "reasons", "coverage", "edge_touch", "semi_transparent", "pieces"]


# region fetch-cutout
def fetch_cutout(client: sc.StackClient, image_path: Path, item_id: str, out: Path, resume: bool) -> tuple[bytes, list[dict]]:
    """The WebP cut-out of one photo and the ledger rows of the call (none when it is read back from an earlier run)."""
    cached, state = out / "cutouts" / f"{item_id}.webp", out / "state" / f"{item_id}.json"
    if resume and cached.exists() and state.exists():
        return cached.read_bytes(), json.loads(state.read_text())["calls"]
    body = {"model": "birefnet", "image": sc.image_base64(image_path.read_bytes()), "options": {"output": "cutout", "format": "webp"}}
    result = client.post("/v1/remove-background", body, tag=item_id)  # retries, pacing and the credit cap live in the client
    data = base64.b64decode(result["data"])
    cached.parent.mkdir(parents=True, exist_ok=True)
    state.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    calls = client.calls(item_id)
    state.write_text(json.dumps({"calls": calls}))
    return data, calls


# endregion


# region process-item
def process(client: sc.StackClient, base: Path, item: dict, out: Path, resume: bool) -> dict:
    entry = {"id": item["id"], "calls": [], "output": None}
    try:
        data, entry["calls"] = fetch_cutout(client, base / item["image"], item["id"], out, resume)
        cutout = pipeline.decode_cutout(data)
        alpha = cutout.getchannel("A")
        (out / "alpha").mkdir(exist_ok=True)
        alpha.save(out / "alpha" / f"{item['id']}.png", optimize=True)
        quality = pipeline.assess(alpha)
        cut = {"format": "webp", "width": cutout.width, "height": cutout.height, "bytes": len(data)}
        output = {"cutout": cut, "quality": quality.to_json(), "catalogue": None}
        if quality.passed:
            webp = pipeline.encode_webp(pipeline.compose(cutout))
            (out / "catalogue").mkdir(exist_ok=True)
            (out / "catalogue" / f"{item['id']}.webp").write_bytes(webp)
            output["catalogue"] = {"file": f"catalogue/{item['id']}.webp", "width": pipeline.CANVAS, "height": pipeline.CANVAS, "bytes": len(webp)}
        entry["status"], entry["output"] = "ok", output
    except sc.StackError as err:
        entry["status"] = "error"
        entry["error"] = {"status": err.status, "code": err.code, "message": err.message}
    return entry


# endregion


def write_review(out: Path, entries: list[dict]) -> int:
    rows = [e for e in entries if e["status"] == "ok" and not e["output"]["quality"]["passed"]]
    with open(out / "review.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(REVIEW_COLUMNS)
        for e in rows:
            q = e["output"]["quality"]
            w.writerow([f"{e['id']}.jpg", e["id"], ";".join(q["reasons"]), q["coverage"], q["edge_touch"], q["semi_transparent"], q["pieces"]])
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=Path(__file__).parent.parent / "assets/smoke/manifest.json")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--resume", action="store_true", help="reuse the cut-outs already in <out>/cutouts instead of paying for them again")
    args = ap.parse_args()
    manifest = json.loads(args.manifest.read_text())
    base, items = args.manifest.parent, manifest["items"][: args.limit]
    started, t0 = datetime.now(UTC), time.monotonic()
    try:
        client = sc.StackClient(max_concurrency=args.concurrency)
        client.models()  # free: checks the key
    except sc.MissingApiKey as err:
        print(err.message, file=sys.stderr)
        return EXIT_NO_KEY
    args.out.mkdir(parents=True, exist_ok=True)

    # region run
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        entries = list(pool.map(lambda it: process(client, base, it, args.out, args.resume), items))
    # endregion

    codes = {e["error"]["code"] for e in entries if e["status"] == "error"}
    for e in entries:
        if e["status"] == "error":
            print(f"  {e['id']}: {e['error']['status']} {e['error']['code']}: {e['error']['message']}", file=sys.stderr)
    flagged = write_review(args.out, entries)
    result = {
        "usecase": manifest["usecase"],
        "language": "python",
        "client": f"stack_client {sc.VERSION}",
        "startedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsedMs": int((time.monotonic() - t0) * 1000),
        "items": entries,
        "ledger": client.summary(),
    }
    (args.out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    failed = sum(1 for e in entries if e["status"] == "error")
    print(f"{len(entries) - flagged - failed} catalogue images, {flagged} in review.csv, {failed} failed", file=sys.stderr)
    if "model_locked" in codes:
        return EXIT_TIER
    if "budget_exceeded" in codes:
        return EXIT_BUDGET
    return 0


if __name__ == "__main__":
    sys.exit(main())
