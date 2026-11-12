"""Write (or check) the committed full-set manifests, use-cases/<use case>/assets/full.json, from the built archives.

  uv run --project datasets python -m datasets.build.repo_manifests            # write all four
  uv run --project datasets python -m datasets.build.repo_manifests --check    # exit 1 when a committed file differs
  uv run --project datasets python -m datasets.build.repo_manifests --only 03 05

`datasets.build` writes the same files while it builds, through the same function (common.repo_manifest); this command
is for when the archives in datasets/dist/ already exist. Only the manifest.json inside each archive is read.
"""
from __future__ import annotations

import argparse
import json
import sys
import tarfile

from .common import DIST, USE_CASES, repo_manifest, write_repo_manifest

UCS = {"01": "01-dataset-prelabelling", "02": "02-click-to-segment", "03": "03-receipts-to-json", "05": "05-image-library-search"}


def archive_manifest(uc: str) -> dict:
    with tarfile.open(DIST / f"{uc}-full.tar.gz") as tar:
        return json.load(tar.extractfile("manifest.json"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--only", nargs="+", choices=list(UCS), default=list(UCS))
    a = ap.parse_args()
    bad = 0
    for k in a.only:
        uc = UCS[k]
        m = archive_manifest(uc)
        target = USE_CASES / uc / "assets" / "full.json"
        if a.check:
            same = target.exists() and json.loads(target.read_text()) == repo_manifest(uc, m)
            print(f"{uc}: {'ok' if same else 'DIFFERS'}")
            bad += not same
        else:
            print(f"{uc}: {len(m['items'])} items -> {write_repo_manifest(uc, m).relative_to(USE_CASES.parent)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
