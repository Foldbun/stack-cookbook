"""Use case 04: 30 synthetic document pages (15 clean + 15 photographed) from datasets/generators/documents.py.

  uv run --project datasets python -m datasets.build.documents

Smoke: d01-p2 (clean, with a table) and d01-photo-p3 (photographed, with a table). Both carry `document: "d01"` in the smoke
manifest, so they are read as pages 2 and 3 of one document and the repeated running header and footer can be removed.
The full manifest keeps the clean and the photographed copies as separate documents (`dNN` and `dNN-photo`).
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

from .common import USE_CASES, WORK, emit_set, sha256_file, write_attribution, write_json
from ..generators import documents

UC = "04-documents-to-rag-chunks"
SMOKE_IDS = {"d01-p2": "d01", "d01-photo-p3": "d01"}      # page id -> document id in the smoke manifest
TRUTH_DOC = {
    "source": "Synthetic pages made by datasets/generators/documents.py (CC0); body text excerpted from public-domain books",
    "blocks": "items[id].blocks: the page's layout blocks in reading order: {order, type (header, title, text, caption, table, formula, footer), "
              "text, bbox [x1, y1, x2, y2] in pixels of the stored image, level (titles), cells (tables: matrix of strings), latex (formulas)}. "
              "The boxes of a photographed page are the clean page's boxes carried through the same perspective and rotation.",
    "markdown": "items[id].markdown: the page as Markdown, blocks joined by a blank line; tables as HTML, formulas as $$ ... $$.",
    "twin": "items[id].twin: the id of the other variant of the same page (clean <-> photographed).",
}


def sync_use_case_smoke() -> None:
    """The smoke set the use case reads: assets/smoke/ is a copy of datasets/smoke/<uc>/, with the sha256 of every image in the
    manifest and `smoke/`-prefixed paths in the attribution rows (the file is relative to the use case's assets/)."""
    src, dst = Path(__file__).resolve().parent.parent / "smoke" / UC, USE_CASES / UC / "assets" / "smoke"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    man = json.loads((dst / "manifest.json").read_text())
    for it in man["items"]:
        it["sha256"] = sha256_file(dst / it["image"])
    write_json(dst / "manifest.json", man)
    with open(src / "ATTRIBUTION.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    write_attribution(dst / "ATTRIBUTION.csv", [{**r, "file": "smoke/" + r["file"]} for r in rows])


def build() -> None:
    st = WORK / UC
    if st.exists():
        shutil.rmtree(st)
    items, truth, attr = documents.generate(st)
    truth_doc = {**TRUTH_DOC, "items": truth}
    smoke_items = [{**it, "document": SMOKE_IDS[it["id"]]} for it in items if it["id"] in SMOKE_IDS]
    emit_set(UC, st, items=items, truth=truth_doc, attribution=attr, smoke_items=smoke_items,
             smoke_truth={**TRUTH_DOC, "items": {i: truth[i] for i in SMOKE_IDS}},
             smoke_files=[f"images/{i}.jpg" for i in SMOKE_IDS])
    sync_use_case_smoke()
    print(UC, "pages", len(items), "documents", len({i["document"] for i in items}))


def main() -> None:
    build()


if __name__ == "__main__":
    main()
