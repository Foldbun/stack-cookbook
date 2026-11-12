"""Use case 03 dataset: CORD v2 receipts (CC BY 4.0) + synthetic invoices (CC0).

  uv run --project datasets python -m datasets.build.cord --help
  uv run --project datasets python -m datasets.build.cord            # 60 CORD receipts + 20 invoices

CORD v2 (NAVER Clova) is read from the Hugging Face dataset repository at a pinned commit (the licence is stated
in datasets/LICENSES.md). 60 receipts are drawn from its validation split with a fixed seed, kept at native
resolution (long side 1296 px, because receipt text is small) and re-encoded as JPEG q90. Ground truth is
normalised from CORD's `gt_parse` (amounts parsed to integers; CORD receipts are mostly in IDR) and the original
parse is kept under `raw`. CORD has no date field, so `date` is null for receipts.
"""
from __future__ import annotations

import argparse
import io
import json
import random
import re
import shutil

import pyarrow.parquet as pq
from PIL import Image

from ..generators import invoices
from .common import CACHE, WORK, emit_set, fetch

SEED = 20261112 + 3
UC = "03-receipts-to-json"
HF_COMMIT = "7f0115a4b758a71d6473b8d085751692da2fef98"
PARQUET = "data/validation-00000-of-00001-cc3c5779fe22e8ca.parquet"
URL = f"https://huggingface.co/datasets/naver-clova-ix/cord-v2/resolve/{HF_COMMIT}/{PARQUET}"
PAGE = "https://huggingface.co/datasets/naver-clova-ix/cord-v2"
CORD_REPO = "https://github.com/clovaai/cord"
N_CORD = 60
# Source rows removed after review because a hand or finger holding the receipt is visible (no people in the sets).
REJECT_INDEX = {36, 31, 83, 79, 56, 4, 8, 16, 97, 93, 78, 75, 87, 94, 46, 63, 10}
MAX_SIDE = 1300


def parse_amount(s) -> int | None:
    """'45,500' -> 45500, '1.250.000' -> 1250000, '-5,000' -> -5000; a trailing ,dd / .dd after another separator is dropped."""
    if s is None:
        return None
    t = str(s).strip()
    neg = t.startswith("-") or t.endswith("-") or "(" in t
    m = re.search(r"[.,]\d{2}$", t)
    if m and re.search(r"[.,]", t[: m.start()]):
        t = t[: m.start()]
    d = re.sub(r"\D", "", t)
    return (-int(d) if neg else int(d)) if d else None


def as_list(x):
    return x if isinstance(x, list) else ([x] if x else [])


def normalise(gt: dict) -> dict | None:
    p = gt["gt_parse"]
    menu = as_list(p.get("menu"))
    sub, tot = p.get("sub_total") or {}, p.get("total") or {}
    if not menu or "total_price" not in tot or parse_amount(tot["total_price"]) is None:
        return None
    lines = []
    for m in menu:
        if not isinstance(m, dict) or "nm" not in m or "price" not in m:
            return None
        lines.append({"description": m["nm"], "quantity": parse_amount(m.get("cnt")) or 1,
                      "amount": parse_amount(m["price"]), "amount_text": m["price"]})
    return {
        "kind": "cord", "vendor": None, "date": None, "currency": "IDR",
        "line_items": lines, "line_item_count": len(lines),
        "subtotal": parse_amount(sub.get("subtotal_price")), "discount": parse_amount(sub.get("discount_price")),
        "service": parse_amount(sub.get("service_price")), "tax": parse_amount(sub.get("tax_price")),
        "total": parse_amount(tot["total_price"]), "total_text": tot["total_price"],
        "raw": p,
    }


# Source rows removed after review (source index -> reason), applied after the seeded selection: the receipt number
# (cord-NNN, which the use case splits into odd / even halves) goes to the next eligible receipt, all others keep theirs.
EXCLUDE_INDEX = {
    6: "part of a hand in the frame",
    70: "part of a hand in the frame",
    24: "part of a hand in the frame",
    45: "part of a hand in the frame",
    20: "part of a hand in the frame",
    77: "part of a hand in the frame",
}
# A receipt is also excluded when its ground-truth text (CORD's own word annotation, not OCR) shows a payment brand
# or the word "card" followed by four digits (the digits may follow the amount on the same row), or a digit run of 10
# or more that is not a price (prices carry thousands separators, so they never form such a run).
PAYMENT = r"(?:VISA|MASTER(?:CARD)?|AMEX|JCB|DEBIT|CREDIT|CARD|BCA|BNI|BRI|MANDIRI|CIMB|OVO|GOPAY|DANA|FLAZZ|TCASH|LINKAJA|SHOPEEPAY|EDC)"
FOUR_DIGITS = re.compile(r"(?<![\d,.])\d{4}(?![\d,.])")
LONG_RUN = re.compile(r"\d(?:-?\d){9,}")


def exclusion_reason(gt: dict) -> str | None:
    """Why a receipt must not be published, judged from its annotation text alone; None when it may be."""
    words = [w["text"] for line in gt.get("valid_line", []) for w in line["words"]]
    for k, w in enumerate(words):
        m = re.fullmatch(PAYMENT + r"\W*(\d{4})?", w.strip(), re.I)
        if m and (m.group(1) or any(FOUR_DIGITS.fullmatch(v.strip()) for v in words[k + 1: k + 4])):
            return "payment card digits"
        if LONG_RUN.search(w):
            return "long identifier number"
    return None


def signature(t: dict) -> tuple:
    """Item lines and total of a receipt, parsed: two receipts with the same signature are the same receipt."""
    return (tuple((re.sub(r"\W+", " ", str(ln["description"])).strip().lower(), ln["quantity"], ln["amount"]) for ln in t["line_items"]),
            t["total"])


def candidates() -> list:
    """CORD validation receipts that pass the structural filters, in the seeded order: (row index, row, truth, gt)."""
    path = fetch(URL, CACHE / "cord" / PARQUET.rsplit("/", 1)[1])
    table = pq.read_table(path).to_pylist()
    rng = random.Random(SEED)
    order = list(range(len(table)))
    rng.shuffle(order)
    out = []
    for idx in order:
        if idx in REJECT_INDEX:
            continue
        row = table[idx]
        gt = json.loads(row["ground_truth"])
        t = normalise(gt)
        if t is None or not 1 <= t["line_item_count"] <= 14:
            continue
        w0, h0 = Image.open(io.BytesIO(row["image"]["bytes"])).size
        if min(w0, h0) < 500:
            continue
        out.append((idx, row, t, gt))
    return out


def select(cands: list) -> list:
    """The first N_CORD candidates keep their slots (receipt numbers); a slot whose receipt is excluded, or repeats the
    item lines and total of a receipt that already holds a slot, goes to the next acceptable candidate after them."""
    def reason(c, kept):
        if c[0] in EXCLUDE_INDEX:
            return EXCLUDE_INDEX[c[0]]
        return exclusion_reason(c[3]) or ("same item lines and total as another receipt" if signature(c[2]) in kept else None)
    slots, kept, spare = {}, set(), list(cands[N_CORD:])
    for n, c in enumerate(cands[:N_CORD]):
        if reason(c, kept) is None:
            slots[n] = c
            kept.add(signature(c[2]))
    for n in range(N_CORD):
        while n not in slots:
            c = spare.pop(0)                        # raises when the spare candidates run out
            if reason(c, kept) is None:
                slots[n] = c
                kept.add(signature(c[2]))
    if len({signature(c[2]) for c in slots.values()}) != N_CORD:
        raise RuntimeError("two selected receipts have the same item lines and total")
    return [slots[n] for n in range(N_CORD)]


def cord_part(st) -> tuple[list, dict, list]:
    items, truth, attr = [], {}, []
    for idx, row, t, _ in select(candidates()):
        iid = f"cord-{len(items) + 1:03d}"
        rel = f"images/{iid}.jpg"
        w, h = _write(row["image"]["bytes"], st / rel)
        t["width"], t["height"], t["source_index"] = w, h, idx
        items.append({"id": iid, "image": rel})
        truth[iid] = t
        attr.append({"file": rel, "id": iid, "title": f"CORD v2 validation receipt {idx}", "author": "NAVER Clova AI (CORD)",
                     "author_url": CORD_REPO, "source_page_url": PAGE, "licence": "CC BY 4.0",
                     "licence_url": "https://creativecommons.org/licenses/by/4.0/",
                     "original_url": URL, "modified": f"Re-encoded as JPEG (q90), long side at most {MAX_SIDE} px",
                     "notes": "CORD v2 receipt image; ground truth normalised from gt_parse"})
    return items, truth, attr


def _write(data: bytes, dst) -> tuple[int, int]:
    from .common import reencode
    return reencode(data, dst, MAX_SIDE)


def build(invoices_only: bool = False) -> None:
    st = WORK / UC
    if st.exists():
        shutil.rmtree(st)
    (st / "images").mkdir(parents=True)
    items, truth, attr = ([], {}, []) if invoices_only else cord_part(st)
    inv_truth = invoices.generate(st, seed=4242, count=20)
    for iid, t in inv_truth.items():
        rel = f"images/{iid}.jpg"
        t["width"], t["height"] = Image.open(st / rel).size
        items.append({"id": iid, "image": rel})
        truth[iid] = t
        attr.append({"file": rel, "id": iid, "title": f"Synthetic invoice {iid} ({t['kind']})", "author": "Foldbun Stack Cookbook contributors",
                     "author_url": "", "source_page_url": "https://github.com/Foldbun/stack-cookbook",
                     "licence": "CC0 1.0", "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                     "original_url": "", "modified": "", "notes": "generated by datasets/generators/invoices.py (seed 4242); fictional companies and amounts"})
    # smoke: 3 CORD receipts + 1 photographed invoice (expected to need the fallback)
    cord_ids = [i["id"] for i in items if i["id"].startswith("cord-")]
    smoke_ids = (cord_ids[:3] if cord_ids else ["inv-001", "inv-002", "inv-003"]) + ["inv-011"]
    truth_doc = {"source": "CORD v2 (NAVER Clova, CC BY 4.0) and synthetic invoices (CC0)",
                 "fields": "total, subtotal, discount, tax, service: integer (CORD, IDR) or number with 2 decimals (invoices); "
                           "line_items[].amount likewise; date: ISO 8601 (invoices) or null (CORD has no date field); "
                           "line_item_count: number of top-level line items. `raw` keeps the unmodified CORD gt_parse.",
                 "items": truth}
    emit_set(UC, st, items=items, truth=truth_doc, attribution=attr,
             smoke_items=[it for it in items if it["id"] in smoke_ids],
             smoke_truth={**{k: v for k, v in truth_doc.items() if k != "items"}, "items": {i: truth[i] for i in smoke_ids}},
             smoke_files=[f"images/{i}.jpg" for i in smoke_ids])
    print(UC, "cord", len(cord_ids), "invoices", len(inv_truth))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--invoices-only", action="store_true", help="skip CORD: build 03 from the synthetic invoices alone")
    build(ap.parse_args().invoices_only)


if __name__ == "__main__":
    main()
