"""Parsing the strings that receipts print: amounts in both number formats, currency marks and dates."""

import re
from datetime import date
from decimal import Decimal

# region amount-regex
# One money-looking token: optional sign or "(" before it, optional currency mark, the number, optional trailing sign.
# It must not touch letters, "/" or ":" (so dates and times do not match) and must not be followed by "%".
# A stray "." or "," just before a grouped number (".20.000", a currency mark the OCR reduced to a dot) is part of no amount.
AMOUNT_RE = re.compile(
    r"(?<![\w/:.,])(?:[.,](?=\d{1,3}[.,]\d{3}(?![\d])))?(?P<pre>[-−(]?)\s*(?P<cur>Rp\.?|IDR|USD|EUR|GBP|AUD|CAD|[$€£¥])?\s*(?P<sign>[-−]?)"
    r"(?P<num>\d{1,3}(?:[\u00a0\u202f]\d{3})+(?:[.,]\d{1,2})?|\d[\d.,]*\d|\d)(?P<post>-|\)|\s?CR)?(?![\w%/:])",
    re.I,
)
# endregion
CURRENCY = {"rp": "IDR", "rp.": "IDR", "idr": "IDR", "usd": "USD", "eur": "EUR", "gbp": "GBP", "aud": "AUD", "cad": "CAD", "€": "EUR", "£": "GBP", "¥": "JPY"}


# region parse-amount
def parse_number(num: str) -> Decimal | None:
    """Digits with "." and "," in either convention: "1,234.50", "1.234,50", "6.000", "4,500", "12,5", "1 234,50".

    The rule: when both marks occur, the last one is the decimal mark. A mark that occurs several times is a thousands
    mark. A single mark followed by exactly three digits is a thousands mark ("6.000" is six thousand, as on the
    Indonesian receipts of the CORD set); followed by one or two digits it is a decimal mark. "0.500" is a decimal.
    """
    num = num.replace("\u00a0", "").replace("\u202f", "")
    marks = [c for c in num if c in ".,"]
    if not marks:
        return Decimal(num)
    last = max(num.rfind("."), num.rfind(","))
    if len(set(marks)) == 2:  # "1.234,50" or "1,234.50": the last mark is the decimal mark
        return Decimal(num[:last].replace(".", "").replace(",", "") + "." + num[last + 1 :])
    mark = marks[0]
    if len(marks) > 1:  # "1.250.000": thousands marks only
        groups = num.split(mark)
        return Decimal("".join(groups)) if all(len(g) == 3 for g in groups[1:]) else None
    head, tail = num.split(mark)
    if len(tail) == 3 and head != "0" and 1 <= len(head) <= 3:
        return Decimal(head + tail)
    return Decimal(f"{head}.{tail}") if head else None


def parse_amount(text: str) -> Decimal | None:
    """The amount in a cell such as "$1,234.50", "-$23.56", "(12.00)", "12.00-" or "Rp 45.000"; None when there is none."""
    found = amounts_in(text)
    return found[-1][0] if found else None


def amounts_in(text: str) -> list[tuple[Decimal, str | None]]:
    """Every money-looking token of the text as (signed value, currency mark), left to right."""
    out = []
    for m in AMOUNT_RE.finditer(text):
        value = parse_number(m["num"])
        if value is None:
            continue
        post = (m["post"] or "").strip().upper()
        # A minus (ASCII or U+2212) before the number or the mark, "(12.00)", "12.00-" and "12.00 CR" all mean negative.
        if m["pre"] in ("-", "−") or m["sign"] or (m["pre"] == "(" and post == ")") or post in ("-", "CR"):
            value = -value
        out.append((value, m["cur"]))
    return out


# endregion


def currency_of(text: str) -> tuple[str | None, str | None]:
    """(ISO code, printed mark) of the first currency mark in the text. "$" alone is ambiguous, so it gives no code."""
    m = AMOUNT_RE.search(text)
    for m in AMOUNT_RE.finditer(text):
        if m["cur"]:
            mark = m["cur"]
            return CURRENCY.get(mark.lower()), mark
    return None, None


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_PATTERNS = [
    (re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)"), "dmy"),
    (re.compile(r"(?<!\w)(\d{1,2})[ -]([A-Za-z]{3})[a-z]*\.?,?[ -](\d{4})(?!\d)"), "d-mon-y"),
    (re.compile(r"(?<!\w)([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})(?!\d)"), "mon-d-y"),
]


def parse_date(text: str, day_first: bool = True) -> date | None:
    """The first calendar date in the text as a `date`: ISO, 04/08/2026, 4.8.26, 4 Aug 2026 or Aug 4, 2026.

    Numeric day/month/year is read day first unless the second number cannot be a month; `day_first=False` flips the default.
    """
    for rx, kind in DATE_PATTERNS:
        for m in rx.finditer(text):
            a, b, c = m.groups()
            try:
                if kind == "ymd":
                    return date(int(a), int(b), int(c))
                if kind == "dmy":
                    first, second, year = int(a), int(b), int(c) + (2000 if len(c) == 2 else 0)
                    if first > 12 or (day_first and second <= 12):
                        return date(year, second, first)
                    return date(year, first, second)
                if kind == "d-mon-y":
                    return date(int(c), MONTHS[b.lower()], int(a))
                return date(int(c), MONTHS[a.lower()], int(b))
            except (ValueError, KeyError):
                continue
    return None
