"""Build every dataset and write the manifests.

  uv run --project datasets python -m datasets.build            # build 01 02 03 05, write datasets/manifest.json
  uv run --project datasets python -m datasets.build --verify   # rebuild from cache, compare against the committed manifest
  uv run --project datasets python -m datasets.build --only 01 05

--verify exits non-zero when any rebuilt tarball or smoke file differs from datasets/manifest.json, when a cached
download no longer matches datasets/sources.lock.json (checked on every cache hit), or when a smoke image has no
attribution row. Downloads are only made for files missing from datasets/.cache/.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys

from . import cord, openimages
from .common import ATTRIBUTION, DATASETS, DIST, SMOKE, files_listing, sha256_file, write_json

MANIFEST = DATASETS / "manifest.json"
UCS = {"01": "01-dataset-prelabelling", "02": "02-click-to-segment", "03": "03-receipts-to-json", "05": "05-image-library-search"}


def build(only: list[str]) -> None:
    for k in only:
        (cord.build if k == "03" else openimages.BUILDERS[k])()


def collect(only: list[str]) -> dict:
    sets = {}
    for k in only:
        uc = UCS[k]
        tar = DIST / f"{uc}-full.tar.gz"
        info = json.loads((DIST / f"{uc}-full.files.json").read_text())
        sets[uc] = {"file": tar.name, "bytes": tar.stat().st_size, "sha256": sha256_file(tar), "files": len(info["files"]),
                    "smoke": {f["path"]: f["sha256"] for f in files_listing(SMOKE / uc)}}
    return sets


def attribution_ok(uc: str) -> list[str]:
    rows = {r["file"] for r in csv.DictReader(open(SMOKE / uc / "ATTRIBUTION.csv", newline=""))}
    return [p["path"] for p in files_listing(SMOKE / uc)
            if p["path"].split("/")[0] in ("images", "masks") and p["path"] not in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--only", nargs="+", choices=list(UCS), default=list(UCS))
    a = ap.parse_args()
    committed = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    build(a.only)
    sets = collect(a.only)
    write_json(DIST / "manifest.json", {"release": "datasets-v1", "sets": {
        uc: {**{k: v for k, v in s.items() if k != "smoke"}, "contents": json.loads((DIST / f"{uc}-full.files.json").read_text())["files"]}
        for uc, s in sets.items()}})
    bad = []
    for uc in sets:
        miss = attribution_ok(uc)
        if miss:
            bad.append(f"{uc}: smoke files without attribution row: {miss[:3]}")
    if a.verify:
        for uc, s in sets.items():
            c = committed.get("sets", {}).get(uc)
            if not c:
                bad.append(f"{uc}: not in datasets/manifest.json")
            elif c["sha256"] != s["sha256"]:
                bad.append(f"{uc}: tarball sha256 {s['sha256'][:12]} != committed {c['sha256'][:12]}")
            elif c["smoke"] != s["smoke"]:
                bad.append(f"{uc}: smoke files differ from the committed manifest")
        print("verify:", "FAILED" if bad else "ok")
    else:
        write_json(MANIFEST, {"release": "datasets-v1", "sets": {**committed.get("sets", {}), **sets}})
    for b in bad:
        print(" ", b, file=sys.stderr)
    for uc, s in sets.items():
        print(f"{uc}: {s['file']} {s['bytes']} bytes sha256 {s['sha256'][:16]} ({s['files']} files)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
