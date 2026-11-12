"""Rule-based field extraction from recognised text.

Both models are turned into the same thing first: rows of cells. `ppocr-v6` gives lines with boxes, which are grouped
into rows by their vertical position. `paddleocr-vl` gives layout blocks: a table block is already rows and cells, any
other block is one row per text line. One set of rules then reads the fields from the rows, whichever model made them.
"""

import re
import statistics
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser

from amounts import amounts_in, currency_of, parse_date


@dataclass
class Cell:
    text: str
    bbox: list | None  # [x1, y1, x2, y2] in pixels of the image
    index: int  # index of the OCR line, or of the layout block
    confidence: float | None  # None when the model does not report one
    size: float | None = None  # text height in pixels of an OCR line; None for layout blocks


@dataclass
class Row:
    cells: list

    @property
    def text(self):
        return " ".join(c.text.strip() for c in self.cells)


# region rows-from-ocr
def rows_from_ocr(lines: list) -> list[Row]:
    """Group OCR lines into rows. Lines whose vertical centres are within 0.6 of a median line height share a row."""
    cells = [Cell(ln["text"], ln["bbox"], i, ln["confidence"], ln["bbox"][3] - ln["bbox"][1]) for i, ln in enumerate(lines) if ln["text"].strip()]
    if not cells:
        return []
    tolerance = 0.6 * statistics.median(c.bbox[3] - c.bbox[1] for c in cells)
    centre = lambda c: (c.bbox[1] + c.bbox[3]) / 2  # noqa: E731
    rows, anchor = [], None
    for cell in sorted(cells, key=centre):
        if rows and abs(centre(cell) - anchor) <= tolerance:
            rows[-1].append(cell)
        else:
            rows.append([cell])
            anchor = centre(cell)
    return [Row(sorted(r, key=lambda c: c.bbox[0])) for r in rows]


# endregion


class _Table(HTMLParser):
    """The rows of the first-level cells of an HTML table, as lists of strings."""

    def __init__(self):
        super().__init__()
        self.rows, self._cell = [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self.rows:
            self.rows[-1].append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


# region rows-from-blocks
def rows_from_blocks(blocks: list) -> list[Row]:
    """Rows of a `paddleocr-vl` page: one row per table row (cells = table cells), one row per text line elsewhere."""
    rows = []
    for block in sorted(blocks, key=lambda b: b["order"]):
        text, box = block.get("text") or "", block["bbox"]
        if block["type"] == "table":
            parser = _Table()
            parser.feed(text)
            rows += [Row([Cell(t, box, block["order"], None) for t in cells if t]) for cells in parser.rows if any(cells)]
        else:
            rows += [Row([Cell(line.strip(), box, block["order"], None)]) for line in text.splitlines() if line.strip()]
    return rows


# endregion

# region summary-rules
# Which printed label is which field. The first rule that matches a row wins, so "subtotal" is tested before "total".
# Payment rows (cash, change, card) are recognised only to be skipped: they carry amounts that are not fields.
SUMMARY_RULES = [
    ("skip", re.compile(r"cash|change|kemba|uang|tender|paid|payment|visa|mastercard|debit|credit|go-?pay|ovo|qris|\bqr\b|card|tunai|round", re.I)),
    # "Net total" / "net sales" print the sum of the items; "ITEMS: 13  561,800" is a count of items followed by that sum.
    ("subtotal", re.compile(r"sub\s?-?\s?total|zwischensumme|sous-total|\bnet\s?(total|sales)|\bitems?\s?[:.]?\s?\d+\b", re.I)),
    ("discount", re.compile(r"discount|\bdisc\b|diskon|rabatt|remise|potongan|savings", re.I)),
    ("service", re.compile(r"service|servis|\bsvc\b|\bsrv\b", re.I)),
    # "P.Rest" / "PResto" / "Pb 1" are the Indonesian restaurant tax (pajak restoran, PB1) as receipts abbreviate it.
    ("tax", re.compile(r"\btax|\bvat\b|\bgst\b|pajak|\bppn\b|\bpb\s?1\b|\bp\.?\s?rest(o|aurant)?\b|mwst|\btva\b|\biva\b", re.I)),
    # "T O T A L" (letters spaced out, as some thermal printers and their OCR give it) is a total too.
    ("total", re.compile(r"total|\bt\s?o\s?t\s?a\s?l\b|amount due|balance due|grand|jumlah|gesamt|summe|to pay", re.I)),
]
STRONG_TOTAL = re.compile(r"grand|amount due|balance due|total bayar|to pay", re.I)
HEADER_WORDS = re.compile(r"description|item|qty|quantity|unit price|price|amount|harga|jumlah", re.I)
META_ROW = re.compile(r"\b(date|time|due|invoice|no|tel|phone|cashier|kasir|table|order|bill to|terms)\b", re.I)
NOT_A_NAME = re.compile(r"invoice|receipt|\bbill\b|\btax\b|faktur|struk|nota|\bdate\b|total|\btel\b|phone", re.I)


CURRENCY_TOKEN = re.compile(r"[$€£¥]|Rp\.?|IDR|USD|EUR|GBP|AUD|CAD", re.I)


def summary_kind(row: Row) -> str | None:
    """Which summary field the row is, or None. A summary row is a label followed by an amount: a row whose only amount comes
    before the label ("9 Rue des Tilleuls  Tax invoice", a street number next to a heading) or that has no amount at all
    ("Tax invoice") is not one, so it neither ends the item rows nor gives a value."""
    text = row.text
    for kind, rx in SUMMARY_RULES:
        m = rx.search(text)
        if m and (kind == "skip" or amounts_in(text[m.end() :])):
            return kind
    return None


# endregion


def _source(model, cells):
    boxes = [c.bbox for c in cells if c.bbox]
    confidences = [c.confidence for c in cells if c.confidence is not None]
    return {
        "model": model,
        "lines": sorted({c.index for c in cells}),
        "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else None,
        "confidence": min(confidences) if confidences else None,
    }


def _number(value: Decimal):
    """JSON number in the form the receipt printed it: 6000 for "6.000", 16.0 for "$16.00"."""
    return int(value) if value.as_tuple().exponent >= 0 else float(value)


# region item-rows
def parse_item_row(row: Row):
    """(quantity, description, unit price, amount) of one row; any part may be None. "1 Nasi (MLY) 6.000" and the numeric row
    "1 4,500 4,500" of a two-line item are both read here."""
    # A currency mark printed apart from its number ("82,37 €", "$ 12.00") is dropped, so the number ends the row.
    tokens = [t for t in row.text.split() if not CURRENCY_TOKEN.fullmatch(t)]
    qty = None
    if len(tokens) > 1 and re.fullmatch(r"\d{1,3}", tokens[0]):
        qty, tokens = Decimal(tokens[0]), tokens[1:]
    elif tokens and (m := re.fullmatch(r"(\d{1,3})\s?x|x\s?(\d{1,3})", tokens[0], re.I)):
        qty, tokens = Decimal(m[1] or m[2]), tokens[1:]
    numeric = [bool(re.search(r"\d", t)) and len(amounts_in(t)) == 1 and not re.search(r"[A-Za-z]{2}", re.sub(r"Rp|IDR", "", t)) for t in tokens]
    trailing = len(numeric)
    while trailing and numeric[trailing - 1]:
        trailing -= 1
    description = " ".join(tokens[:trailing]).strip(" -:")
    prices = [amounts_in(t)[0][0] for t in tokens[trailing:]]
    if qty is None and len(prices) >= 3 and prices[-3] == prices[-3].to_integral_value():  # table row: quantity, unit price, amount
        qty = prices[-3]
    return qty, description or None, (prices[-2] if len(prices) > 1 else None), (prices[-1] if prices else None)


def _quantity_row(parsed) -> bool:
    """A description followed by one lone small whole number (the quantity of a table row whose prices wrapped to the next line)."""
    qty, description, unit, amount = parsed
    return bool(description) and qty is None and unit is None and amount is not None and amount < 1000 and amount == amount.to_integral_value()


def extract_items(rows: list[Row], model: str, width: int | None, header_at: int | None, stop_at: int) -> list:
    """Line items from the rows between the table header (when there is one) and the first summary row.

    A row with a description and an amount is an item. A row with a description only is held back for the next row
    when that row has an amount and no description (the two-line layout of many receipts). Without a table header, an
    amount must sit in the right 40 percent of the image: that keeps addresses and phone numbers out.
    """
    items, pending = [], None
    body = rows[(header_at + 1 if header_at is not None else 0) : stop_at]
    for n, row in enumerate(body):
        # Wrapped table row: "Equipment rental, per day  1" and then "71,96 €  71,96 €" (a photographed table whose numbers
        # sit a little lower than the text). A description ending in a lone small integer, followed by a row that is only
        # two or more numbers, is one item: the integer is the quantity, not the amount.
        if header_at is not None and n + 1 < len(body):
            head, tail = parse_item_row(row), parse_item_row(body[n + 1])
            if _quantity_row(head) and not tail[1] and tail[2] is not None:
                row = Row(row.cells + body[n + 1].cells)
                body[n + 1] = Row([])
        if not row.cells:
            continue
        qty, description, unit, amount = parse_item_row(row)
        if header_at is None and META_ROW.search(row.text):
            continue
        if amount is None:
            pending = (description, row) if description and len(description) > 2 and not HEADER_WORDS.fullmatch(description) else None
            continue
        if header_at is None and width and description and max(c.bbox[2] for c in row.cells if c.bbox) < 0.6 * width:
            continue
        cells = list(row.cells)
        if not description and pending:
            description, cells = pending[0], pending[1].cells + cells
        pending = None
        items.append({"description": description or "", "quantity": qty, "unitPrice": unit, "amount": amount, "_cells": cells})
    return items


# endregion


# region fields
def extract_receipt(rows: list[Row], model: str, width: int | None = None) -> dict:
    """Merchant, date, currency, line items and the summary amounts, each with the cells they were read from."""
    kinds = [summary_kind(r) for r in rows]
    # A discount printed among the items, before the subtotal row, is an item with a negative amount: the subtotal already
    # includes it. Only a discount printed after the subtotal is subtracted from it.
    kinds = [None if k == "discount" and "subtotal" in kinds[n + 1 :] else k for n, k in enumerate(kinds)]
    stop_at = next((i for i, k in enumerate(kinds) if k not in (None, "skip")), len(rows))
    header_at = next(
        (i for i, r in enumerate(rows[:stop_at]) if len(set(m.lower() for m in HEADER_WORDS.findall(r.text))) >= 2 and not amounts_in(r.text)), None
    )
    receipt = {"merchant": None, "date": None, "currency": None, "currencyMark": None, "lineItems": [], "sources": {}}
    for name in ("subtotal", "discount", "service", "tax", "total"):
        receipt[name] = None
    # Summary rows: the last amount of a row is its value. Of several rows with the same label the first wins, unless a later
    # total says "grand" or "amount due": noisy OCR can turn "Total Kembalian" (change) into something that is not skipped.
    for row, kind in zip(rows, kinds):
        found = amounts_in(row.text)
        if kind not in (None, "skip") and found and (receipt[kind] is None or (kind == "total" and STRONG_TOTAL.search(row.text))):
            value = abs(found[-1][0]) if kind == "discount" else found[-1][0]
            receipt[kind], receipt["sources"][kind] = _number(value), _source(model, row.cells)
    used = []
    items = extract_items(rows, model, width, header_at, stop_at)
    # A member or card discount is printed among the items and again as the "Discount" summary row: the same amount, negative
    # among the items. It is the discount, not an item.
    items = [
        i for i in items if not (receipt["discount"] and i["amount"] is not None and i["amount"] < 0 and -i["amount"] == Decimal(str(receipt["discount"])))
    ]
    for item in items:
        used += item["_cells"]
        source = _source(model, item.pop("_cells"))
        receipt["lineItems"].append({**{k: (_number(v) if isinstance(v, Decimal) else v) for k, v in item.items()}, "source": source})
    # Date: the first date in a row that is not the due date.
    for row in rows:
        if "due" not in row.text.lower():
            for cell in row.cells:
                if (day := parse_date(cell.text)) is not None:
                    receipt["date"], receipt["sources"]["date"] = day.isoformat(), _source(model, [cell])
                    break
        if receipt["date"]:
            break
    # Merchant: the largest text near the top that looks like a name rather than a heading or an address.
    names = [
        c for r in rows[: min(6, stop_at)] for c in r.cells if c not in used and len(re.findall(r"[^\W\d_]", c.text)) >= 3 and not NOT_A_NAME.search(c.text)
    ]
    names = [c for c in names if len(re.findall(r"[^\W\d_]", c.text)) >= 0.6 * len(c.text.replace(" ", ""))]
    if names:
        best = max(names, key=lambda c: (c.size or 0, -c.index))  # blocks have no size: the first candidate wins
        receipt["merchant"], receipt["sources"]["merchant"] = best.text.strip(), _source(model, [best])
    for row, kind in zip(rows, kinds):
        if kind not in (None, "skip") and amounts_in(row.text):
            receipt["currency"], receipt["currencyMark"] = currency_of(row.text)
            if receipt["currencyMark"]:
                break
    return receipt


# endregion
