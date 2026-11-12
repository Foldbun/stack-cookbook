"""Search an image library: index a folder, then query it by image, by text, or for near-duplicates.

    uv run imgsearch.py index ./photos            embed new and changed files (resumable, incremental)
    uv run imgsearch.py image ./photos/a.jpg      the images that look most like this one
    uv run imgsearch.py text "a photo of a dog"   the images that match a sentence
    uv run imgsearch.py duplicates --threshold .9 groups of near-duplicate images
    uv run imgsearch.py status                    what the index holds

Image similarity uses dinov3 (Built with DINOv3); text search uses siglip2. FOLDBUN_API_KEY must be set.
Exit codes: 0 done, 1 failed, 2 no API key, 3 account tier cannot use a model, 4 credit cap refused a call,
5 stopped because the daily allowance is used up (run the same command again after 00:00 UTC).
"""

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import embedder
import search
import stack_client as sc
from index import LibraryIndex, file_sha256, scan_folder

EXIT_FAILED, EXIT_NO_KEY, EXIT_TIER, EXIT_BUDGET, EXIT_ALLOWANCE = 1, 2, 3, 4, 5


def next_reset() -> str:
    """When the included daily calls come back: 00:00 UTC."""
    now = datetime.now(UTC)
    wait = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC) - now
    return f"00:00 UTC, in {wait.seconds // 3600} h {wait.seconds % 3600 // 60} min"


def print_cost(client: sc.StackClient) -> None:
    s = client.summary()
    print(f"cost: {s['listTotal']:g} credits at list price, {s['chargedTotal']:g} charged, in {s['calls']} calls")


def cmd_index(args, client) -> int:
    root = args.folder.resolve()
    index = LibraryIndex.load(args.index)
    index.set_items(scan_folder(root, index.items))
    index.commit()  # drops the rows of files that were removed
    t0 = time.monotonic()
    report = embedder.index_library(
        client, index, root, batch_size=args.batch_size, free_only=args.free_only, keep_free=args.keep_free, workers=args.concurrency
    )
    print(f"embedded {report.embedded} vectors in {time.monotonic() - t0:.1f} s")
    print_cost(client)
    for sha, err in report.failed.items():
        print(f"  failed {sha[:12]}: {err['code']}: {err['message']}", file=sys.stderr)
    if report.stopped:
        left = sum(len(index.missing(m)) for m in ("dinov3", "siglip2"))
        print(f"stopped: {report.stopped['message']} ({report.stopped['code']}); {left} vectors are still missing.")
        print(f"The included calls come back at {next_reset()}. Run the same command then: only missing images are embedded.")
        return EXIT_ALLOWANCE if report.stopped["code"] == "daily_limit_reached" else EXIT_BUDGET
    return EXIT_FAILED if report.failed else 0


def show(items, ranked) -> None:
    for rank, (row, score) in enumerate(ranked, 1):
        print(f"{rank:>3}  {score:.4f}  {items[row]['id']}")


def cmd_image(args, client) -> int:
    index = LibraryIndex.load(args.index)
    items, matrix = index.matrix("dinov3")
    if not items:
        print("the index has no dinov3 vectors: run `index` first", file=sys.stderr)
        return EXIT_FAILED
    sha = file_sha256(args.image)
    vector = index.vector("dinov3", sha)  # an image of the library needs no API call
    if vector is None:
        vector, _ = embedder.embed_image(client, args.image, tag="query")
    own = next((i for i, it in enumerate(items) if it["sha256"] == sha), None)
    show(items, search.top_k(matrix, vector, args.k, exclude=own))
    print_cost(client)
    return 0


def cmd_text(args, client) -> int:
    index = LibraryIndex.load(args.index)
    items, matrix = index.matrix("siglip2")
    if not items:
        print("the index has no siglip2 vectors: run `index` first", file=sys.stderr)
        return EXIT_FAILED
    vector, _ = embedder.embed_text(client, args.text, tag="query")
    show(items, search.top_k(matrix, vector, args.k))
    print_cost(client)
    return 0


def cmd_duplicates(args, client) -> int:  # no API call: the vectors are on disk
    index = LibraryIndex.load(args.index)
    items, matrix = index.matrix("dinov3")
    groups = search.duplicate_groups(matrix, args.threshold)
    for n, group in enumerate(groups, 1):
        print(f"group {n}: " + ", ".join(items[i]["id"] for i in group))
    print(f"{len(groups)} groups of near-duplicates among {len(items)} images at similarity >= {args.threshold}")
    return 0


def cmd_status(args, client) -> int:
    index = LibraryIndex.load(args.index)
    print(f"{len(index.items)} images; vectors: " + ", ".join(f"{m} {len(i['sha256s'])} x {i['dimensions']}" for m, i in index.models.items()))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", type=Path, default=Path("library-index"), help="index directory (default: ./library-index)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index", help="embed the new and changed images of a folder")
    p.add_argument("folder", type=Path)
    p.add_argument("--batch-size", type=int, default=50, help="images per batch request; the index is saved after each (default 50, at most 1000)")
    p.add_argument("--concurrency", type=int, default=4, help="parallel uploads")
    p.add_argument("--free-only", action="store_true", help="stop when the included daily calls are used up instead of paying for more")
    p.add_argument("--keep-free", type=int, default=0, metavar="N", help="with the public pool or --free-only: leave N included calls for queries")
    p.set_defaults(run=cmd_index)
    p = sub.add_parser("image", help="the images that look most like a given image")
    p.add_argument("image", type=Path)
    p.add_argument("-k", type=int, default=10)
    p.set_defaults(run=cmd_image)
    p = sub.add_parser("text", help="the images that match a sentence")
    p.add_argument("text")
    p.add_argument("-k", type=int, default=10)
    p.set_defaults(run=cmd_text)
    p = sub.add_parser("duplicates", help="groups of near-duplicate images")
    p.add_argument("--threshold", type=float, default=search.DEFAULT_THRESHOLD)
    p.set_defaults(run=cmd_duplicates)
    p = sub.add_parser("status", help="what the index holds")
    p.set_defaults(run=cmd_status)
    args = ap.parse_args(argv)
    try:
        client = sc.StackClient(max_concurrency=4) if args.cmd in ("index", "image", "text") else None
        return args.run(args, client)
    except sc.MissingApiKey as err:
        print(err.message, file=sys.stderr)
        return EXIT_NO_KEY
    except sc.StackError as err:
        print(f"{err.status} {err.code}: {err.message}" + (f" (request {err.request_id})" if err.request_id else ""), file=sys.stderr)
        if err.code == "daily_limit_reached":
            print(f"The included calls come back at {next_reset()}.", file=sys.stderr)
            return EXIT_ALLOWANCE
        return {"model_locked": EXIT_TIER, "budget_exceeded": EXIT_BUDGET}.get(err.code, EXIT_FAILED)


if __name__ == "__main__":
    sys.exit(main())
