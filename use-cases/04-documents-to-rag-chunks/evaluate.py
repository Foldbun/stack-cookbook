"""Quality, chunk statistics and cost of a run of 04-documents-to-rag-chunks, from result.json and the ground truth.

    python evaluate.py --result out/result.json --truth assets/smoke/truth.json [--out expected/smoke-summary.json]

Prints one flat JSON object of metrics on the last line of stdout (the harness reads that line). Standard library only.
It scores the 30-page full set as well as the smoke set: pass assets/truth.json of the extracted archive.

Every page of the set has a clean and a photographed variant (`truth.items[id].variant`), so each quality metric is also
reported per variant, with the gap between them. No latency or throughput figure is computed: timing measured through a
client's network says little about the service and is not published.

Metrics (pages the run could not read are left out of the quality metrics and counted in `pages_failed`):
  text_ned         normalised edit distance of the text of all blocks except tables, formulas and figures, in reading order, after
                   whitespace, quote and dash normalisation; 0 is perfect. Mean over pages.
  table_cell_f1    the HTML tables are parsed into cell grids (colspan and rowspan expanded); a cell counts when its row, column and
                   normalised text all match. Tables are paired in reading order; a truth table with no partner scores zero.
  reading_order_tau  Kendall tau between the model's block order and the true order, over the blocks matched by box overlap (IoU >= 0.3).
  formula_recall   share of the true formulas that a `formula` block overlaps (IoU >= 0.5).
  chunks, chunk_chars_*, tables_split  from the chunks of the run: a table is split when it is in no chunk, in several, or its HTML is not whole in the chunk.
  cost_per_1000_pages_list  list price of the run's calls per 1,000 pages, every page counted at list price (replayed calls included).
"""

import argparse
import json
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

ROUND = 4
TEXT_SKIP = ("table", "formula", "figure")


def norm(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-"}))
    return " ".join(text.split())


def edit_distance(a, b):
    """Levenshtein distance by the bit-parallel algorithm of Myers and Hyyro (Python integers as the bit vectors)."""
    if len(a) < len(b):
        a, b = b, a
    m = len(b)
    if m == 0:
        return len(a)
    peq = {}
    for i, c in enumerate(b):
        peq[c] = peq.get(c, 0) | (1 << i)
    mask, pv, mv, score, top = (1 << m) - 1, (1 << m) - 1, 0, m, 1 << (m - 1)
    for c in a:
        eq = peq.get(c, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | ~(xh | pv)
        mh = pv & xh
        if ph & top:
            score += 1
        elif mh & top:
            score -= 1
        ph = ((ph << 1) | 1) & mask
        mh = (mh << 1) & mask
        pv = (mh | ~(xv | ph)) & mask
        mv = ph & xv
    return score


def ned(pred, truth):
    pred, truth = norm(pred), norm(truth)
    return edit_distance(pred, truth) / max(len(pred), len(truth), 1)


def page_text(blocks):
    return " ".join(b.get("text", "") for b in sorted(blocks, key=lambda b: b["order"]) if b["type"] not in TEXT_SKIP)


class TableParser(HTMLParser):
    """HTML table to a grid of strings; colspan and rowspan are expanded. Only the first table of the input is read."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.span, self.depth = [], None, None, (1, 1), 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self.depth += 1
        elif tag == "tr" and self.depth == 1:
            self.row = []
            self.rows.append(self.row)
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
            num = lambda v: int(v) if str(v).isdigit() else 1  # noqa: E731
            self.span = (num(a.get("rowspan", 1)), num(a.get("colspan", 1)))

    def handle_endtag(self, tag):
        if tag == "table":
            self.depth -= 1
        elif tag in ("td", "th") and self.cell is not None:
            self.row.append(("".join(self.cell), *self.span))
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def grid(html):
    """{(row, col): normalised text} of the first table in `html`."""
    p = TableParser()
    p.feed(html or "")
    cells, used = {}, set()
    for r, row in enumerate(p.rows):
        c = 0
        for text, rs, cs in row:
            while (r, c) in used:
                c += 1
            for dr in range(rs):
                for dc in range(cs):
                    used.add((r + dr, c + dc))
                    cells[(r + dr, c + dc)] = norm(text)
            c += cs
    return cells


def grid_of_matrix(matrix):
    return {(r, c): norm(t) for r, row in enumerate(matrix) for c, t in enumerate(row)}


def cell_counts(pred, truth):
    """(true positives, predicted cells, true cells): a cell matches when position and text are the same."""
    tp = sum(1 for k, v in pred.items() if truth.get(k) == v)
    return tp, len(pred), len(truth)


def iou(a, b):
    ix, iy = max(0, min(a[2], b[2]) - max(a[0], b[0])), max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def match_blocks(pred, truth, thr):
    """Greedy one-to-one matching by IoU, best pairs first: {pred index: truth index}."""
    pairs = sorted(((iou(p["bbox"], t["bbox"]), i, j) for i, p in enumerate(pred) for j, t in enumerate(truth)), reverse=True)
    got, used_p, used_t = {}, set(), set()
    for score, i, j in pairs:
        if score < thr:
            break
        if i not in used_p and j not in used_t:
            got[i] = j
            used_p.add(i)
            used_t.add(j)
    return got


def kendall_tau(seq):
    n = len(seq)
    if n < 2:
        return None
    conc = sum(1 for i in range(n) for j in range(i + 1, n) if seq[i] < seq[j])
    return (2 * conc - n * (n - 1) / 2) / (n * (n - 1) / 2)


def reading_order(pred_blocks, truth_blocks):
    pred = sorted(pred_blocks, key=lambda b: b["order"])
    got = match_blocks(pred, truth_blocks, 0.3)
    return kendall_tau([truth_blocks[got[i]]["order"] for i in sorted(got)])


def formula_recall(pred_blocks, truth_blocks):
    want = [t for t in truth_blocks if t["type"] == "formula"]
    if not want:
        return None
    preds = [p for p in pred_blocks if p["type"] == "formula"]
    hit = sum(1 for t in want if any(iou(p["bbox"], t["bbox"]) >= 0.5 for p in preds))
    return hit / len(want)


def mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), ROUND) if values else None


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, max(0, round(q * (len(values) - 1))))] if values else None


def split_tables(entries):
    """Number of table blocks that are not whole in exactly one chunk (dropped blocks are not chunked and not counted)."""
    bad = total = 0
    for e in entries:
        page = e["output"]["page"]
        chunks = e["output"]["chunks"]  # a chunk never crosses a page, and page numbers repeat across documents
        for b in e["output"]["blocks"]:
            if b["type"] != "table" or b.get("dropped"):
                continue
            total += 1
            html = (b.get("markdown") or b.get("text") or "").strip()
            holders = [c for c in chunks if [page, b["order"]] in c["blocks"]]
            if len(holders) != 1 or html not in holders[0]["text"]:
                bad += 1
    return bad, total


def evaluate(result, truth):
    entries = [i for i in result["items"] if i["status"] == "ok" and i["id"] in truth["items"]]
    metrics = {"pages": len(result["items"]), "pages_scored": len(entries), "pages_failed": sum(1 for i in result["items"] if i["status"] != "ok")}
    per = {}
    for e in entries:
        t = truth["items"][e["id"]]
        blocks = e["output"]["blocks"]
        row = {"variant": t["variant"], "ned": ned(page_text(blocks), page_text(t["blocks"])), "tau": reading_order(blocks, t["blocks"]), "formula": formula_recall(blocks, t["blocks"])}
        pt = [b for b in sorted(blocks, key=lambda b: b["order"]) if b["type"] == "table"]
        tt = [b for b in t["blocks"] if b["type"] == "table"]
        counts = [0, 0, 0]
        for k, tb in enumerate(tt):
            pg = grid(pt[k].get("text") or pt[k].get("markdown")) if k < len(pt) else {}
            for n, v in enumerate(cell_counts(pg, grid_of_matrix(tb["cells"]))):
                counts[n] += v
        counts[1] += sum(len(grid(p.get("text") or "")) for p in pt[len(tt) :])  # tables the model invented count against precision
        row["table"] = counts if tt else None
        row["tables_found"] = (len(pt) == len(tt)) if tt else None
        per[e["id"]] = row

    def f1(rows):
        tp, p, r = (sum(x["table"][n] for x in rows if x["table"]) for n in range(3))
        return round(2 * tp / (p + r), ROUND) if p + r else None

    groups = {"": list(per.values()), "_clean": [r for r in per.values() if r["variant"] == "clean"], "_photographed": [r for r in per.values() if r["variant"] == "photographed"]}
    for suffix, rows in groups.items():
        metrics[f"text_ned{suffix}"] = mean(r["ned"] for r in rows)
        metrics[f"table_cell_f1{suffix}"] = f1(rows)
        metrics[f"reading_order_tau{suffix}"] = mean(r["tau"] for r in rows)
    metrics["formula_recall"] = mean(r["formula"] for r in per.values())
    metrics["tables_found_rate"] = mean(r["tables_found"] for r in per.values())
    gap = lambda a, b: None if a is None or b is None else round(a - b, ROUND)  # noqa: E731
    metrics["text_ned_gap"] = gap(metrics["text_ned_photographed"], metrics["text_ned_clean"])  # photographed minus clean: positive is worse
    metrics["table_cell_f1_gap"] = gap(metrics["table_cell_f1_clean"], metrics["table_cell_f1_photographed"])  # clean minus photographed: positive is worse

    chunks = [c for e in entries for c in e["output"]["chunks"]]
    sizes = [c["chars"] for c in chunks]
    bad, total = split_tables(entries)
    metrics.update(
        chunks=len(chunks),
        chunks_per_page=round(len(chunks) / max(len(entries), 1), 2),
        chunk_chars_p10=percentile(sizes, 0.1),
        chunk_chars_p50=percentile(sizes, 0.5),
        chunk_chars_p90=percentile(sizes, 0.9),
        chunk_chars_max=max(sizes, default=None),
        tables_in_chunks=total,
        tables_split=bad,
        blocks_dropped=sum(1 for e in entries for b in e["output"]["blocks"] if b.get("dropped")),
    )
    calls = [c for i in result["items"] for c in i["calls"]]
    metrics["cost_per_1000_pages_list"] = round(sum(c["list"] for c in calls) / max(len(result["items"]), 1) * 1000, 2)
    return metrics


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result", type=Path, required=True)
    ap.add_argument("--truth", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="also write the metrics to this file")
    args = ap.parse_args()
    metrics = evaluate(json.loads(args.result.read_text()), json.loads(args.truth.read_text()))
    if args.out:
        args.out.write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics))


if __name__ == "__main__":
    main()
