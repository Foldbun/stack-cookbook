"""Unit tests of the pure parts of 03-receipts-to-json/python: amounts, dates, extraction rules, validation, evaluation."""

import importlib.util
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

UC = Path(__file__).resolve().parent.parent / "use-cases" / "03-receipts-to-json"
sys.path.insert(0, str(UC / "python"))

import amounts  # noqa: E402
import extract  # noqa: E402
import validate  # noqa: E402


def load_evaluate():
    spec = importlib.util.spec_from_file_location("uc03_evaluate", UC / "evaluate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("6.000", 6000),  # one mark and three digits: thousands
        ("4,500", 4500),
        ("1.250.000", 1250000),
        ("$1,234.50", Decimal("1234.50")),
        ("1.234,50", Decimal("1234.50")),
        ("Rp 45.000", 45000),
        ("12,5", Decimal("12.5")),
        ("0.500", Decimal("0.500")),
        ("-$23.56", Decimal("-23.56")),
        ("−$23.56", Decimal("-23.56")),
        ("(12.00)", Decimal("-12.00")),
        ("12.00-", Decimal("-12.00")),
        ("Tax (6%) $26.86", Decimal("26.86")),  # the 6 of "6%" is not an amount
        ("4/12/2019 6:31", None),  # dates and times are not amounts
    ],
)
def test_parse_amount(text, value):
    assert amounts.parse_amount(text) == (None if value is None else Decimal(value))


def test_parse_date():
    assert amounts.parse_date("Date: 2026-08-04") == date(2026, 8, 4)
    assert amounts.parse_date("04/08/2026") == date(2026, 8, 4)  # day first
    assert amounts.parse_date("04/08/2026", day_first=False) == date(2026, 4, 8)
    assert amounts.parse_date("12/13/2026") == date(2026, 12, 13)  # 13 cannot be a month
    assert amounts.parse_date("Aug 4, 2026") == amounts.parse_date("4 August 2026") == date(2026, 8, 4)
    assert amounts.parse_date("31/02/2026") is None


def ocr_rows(*cells):
    """Rows from (text, x1, y1, x2, y2) tuples, as the OCR lines of a small receipt."""
    return extract.rows_from_ocr([{"text": t, "confidence": 0.95, "bbox": [a, b, c, d]} for t, a, b, c, d in cells])


def test_two_line_item_and_summary():
    rows = ocr_rows(
        ("Coffee beans", 10, 100, 200, 120),
        ("2", 10, 130, 20, 150),
        ("3,50", 100, 130, 150, 150),
        ("7,00", 300, 130, 350, 150),
        ("Subtotal", 10, 200, 100, 220),
        ("7,00", 300, 200, 350, 220),
        ("VAT 19%", 10, 230, 100, 250),
        ("1,33", 300, 230, 350, 250),
        ("TOTAL", 10, 260, 100, 280),
        ("8,33", 300, 260, 350, 280),
        ("Cash", 10, 290, 100, 310),
        ("10,00", 300, 290, 350, 310),
    )
    receipt = extract.extract_receipt(rows, "ppocr-v6", 400)
    assert [(i["description"], i["quantity"], i["unitPrice"], i["amount"]) for i in receipt["lineItems"]] == [("Coffee beans", 2, 3.5, 7.0)]
    assert (receipt["subtotal"], receipt["tax"], receipt["total"]) == (7.0, 1.33, 8.33)  # cash is not the total
    assert validate.passed(validate.validate(receipt, date(2026, 11, 12)))


def test_heading_with_the_word_tax_does_not_end_the_items():
    """ "Tax invoice" is a heading, and the street number before it is not an amount of the tax: neither row is a summary row."""
    rows = ocr_rows(
        ("9 Rue des Tilleuls", 10, 20, 200, 40),
        ("Tax invoice", 300, 20, 400, 40),
        ("Description", 10, 100, 100, 120),
        ("Qty", 200, 100, 230, 120),
        ("Amount", 300, 100, 380, 120),
        ("Tea", 10, 130, 100, 150),
        ("2", 200, 130, 210, 150),
        ("2,00 \u20ac", 250, 130, 290, 150),
        ("4,00 \u20ac", 300, 130, 380, 150),
        ("Total", 10, 200, 100, 220),
        ("4,00 \u20ac", 300, 200, 380, 220),
    )
    receipt = extract.extract_receipt(rows, "ppocr-v6", 400)
    assert receipt["tax"] is None
    assert [(i["description"], i["quantity"], i["amount"]) for i in receipt["lineItems"]] == [("Tea", 2, 4.0)]
    assert validate.passed(validate.validate(receipt, date(2026, 11, 12)))


def test_summary_kind_needs_an_amount_after_the_label():
    row = lambda text: extract.Row([extract.Cell(text, None, 0, None)])  # noqa: E731
    assert extract.summary_kind(row("Tax invoice")) is None
    assert extract.summary_kind(row("9 Rue des Tilleuls Tax invoice")) is None
    assert extract.summary_kind(row("VAT (20%) 336,55")) == "tax"
    assert extract.summary_kind(row("Cash 50.000")) == "skip"


def test_currency_mark_apart_from_its_number():
    """ "82,37 \u20ac" and "$ 12.00": the mark is a token of its own and must not stop the numbers at the end of the row."""
    cell = lambda text: extract.Row([extract.Cell(text, None, 0, None)])  # noqa: E731
    assert extract.parse_item_row(cell("Consulting 5 82,37 \u20ac 411,85 \u20ac")) == (5, "Consulting", Decimal("82.37"), Decimal("411.85"))
    assert extract.parse_item_row(cell("Tea 2 $ 3.00 $ 6.00")) == (2, "Tea", Decimal("3.00"), Decimal("6.00"))


def test_table_row_wrapped_after_the_quantity():
    """A photographed table: the description and its quantity are read as one row, the prices as the next one."""
    rows = ocr_rows(
        ("Description", 10, 100, 100, 120),
        ("Qty Unit price Amount", 200, 100, 380, 120),
        ("Rental, per day 1", 10, 130, 150, 150),
        ("20,00 \u20ac 20,00 \u20ac", 200, 160, 380, 180),
        ("Brochure, box of 250", 10, 190, 150, 210),
        ("3 4,00 \u20ac 12,00 \u20ac", 200, 220, 380, 240),
        ("Total", 10, 300, 100, 320),
        ("32,00 \u20ac", 300, 300, 380, 320),
    )
    receipt = extract.extract_receipt(rows, "ppocr-v6", 400)
    assert [(i["description"], i["quantity"], i["unitPrice"], i["amount"]) for i in receipt["lineItems"]] == [
        ("Rental, per day", 1, 20.0, 20.0),
        ("Brochure, box of", 3, 4.0, 12.0),
    ]
    assert validate.passed(validate.validate(receipt, date(2026, 11, 12)))


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Net Total: Rp194,726", "subtotal"),
        ("NET Sales 35.000", "subtotal"),
        ("ITEMS:13 561,800", "subtotal"),  # a count of items followed by their sum
        ("Srv Ch Rp9,736", "service"),
        ("P.Rest 10% 5,592", "tax"),
        ("PResto 10/ 2,700", "tax"),
        ("Pb 1 18,900", "tax"),
        ("3 T O T A L 174.983", "total"),
        ("Total Qty 7", "total"),  # first label wins at the call site; the kind itself is still total
        ("Prestige Coffee 4.000", None),  # "P" and "rest" inside a longer word are not the restaurant tax
        ("3 Items,", None),  # no amount after the label
    ],
)
def test_summary_vocabulary(text, kind):
    assert extract.summary_kind(extract.Row([extract.Cell(text, None, 0, None)])) == kind


def test_discount_before_the_subtotal_is_an_item():
    """ "Discount" among the items, above "Net Total": the subtotal already includes it, so it is a negative item."""
    rows = ocr_rows(
        ("1 Tea", 10, 100, 100, 120),
        ("Rp10.000", 300, 100, 380, 120),
        ("1 Cake", 10, 130, 100, 150),
        ("Rp20.000", 300, 130, 380, 150),
        ("1 Discount 10%", 10, 160, 150, 180),
        ("-Rp3.000", 300, 160, 380, 180),
        ("Net Total:", 10, 200, 100, 220),
        ("Rp27.000", 300, 200, 380, 220),
        ("Srv Ch", 10, 230, 100, 250),
        ("Rp1.350", 300, 230, 380, 250),
        ("TOTAL:", 10, 260, 100, 280),
        ("Rp28.350", 300, 260, 380, 280),
    )
    receipt = extract.extract_receipt(rows, "ppocr-v6", 400)
    assert [i["amount"] for i in receipt["lineItems"]] == [10000, 20000, -3000]
    assert (receipt["subtotal"], receipt["discount"], receipt["service"], receipt["total"]) == (27000, None, 1350, 28350)
    assert validate.passed(validate.validate(receipt, date(2026, 11, 12)))


def test_discount_echoed_among_the_items_is_not_an_item():
    rows = ocr_rows(
        ("Cake", 10, 100, 100, 120),
        ("63,636", 300, 100, 380, 120),
        ("Club Card", 10, 130, 100, 150),
        ("-9,545", 300, 130, 380, 150),
        ("SubTotal:", 10, 200, 100, 220),
        ("63,636", 300, 200, 380, 220),
        ("Discount:", 10, 230, 100, 250),
        ("-9,545", 300, 230, 380, 250),
        ("Total:", 10, 260, 100, 280),
        ("54,091", 300, 260, 380, 280),
    )
    receipt = extract.extract_receipt(rows, "ppocr-v6", 400)
    assert [i["description"] for i in receipt["lineItems"]] == ["Cake"]
    assert receipt["discount"] == 9545
    assert validate.passed(validate.validate(receipt, date(2026, 11, 12)))


def test_amount_with_a_stray_leading_dot():
    assert amounts.parse_amount("TOTAL .20.000") == 20000
    assert amounts.parse_amount("Tea, 3.50") == Decimal("3.50")  # a comma before a decimal number changes nothing


def test_items_may_differ_from_the_subtotal_by_the_rounding_of_whole_numbers():
    """Whole rupiah amounts rounded one by one: 7,727 + 33,636 = 41,363 against a printed subtotal of 41,364."""
    receipt = {
        "total": 45500, "subtotal": 41364, "discount": None, "service": None, "tax": 4136, "date": None, "sources": {},
        "lineItems": [
            {"description": "A", "quantity": None, "unitPrice": None, "amount": 7727, "source": {"confidence": None}},
            {"description": "B", "quantity": None, "unitPrice": None, "amount": 33636, "source": {"confidence": None}},
        ],
    }  # fmt: skip
    assert validate.passed(validate.validate(receipt, date(2026, 11, 12)))
    receipt["lineItems"][1]["amount"] = 33630  # six units off: a missing or misread item, not rounding
    assert not {c["name"]: c["passed"] for c in validate.validate(receipt, date(2026, 11, 12))}["items_sum_to_subtotal"]
    # decimals and small amounts keep the one-cent tolerance
    receipt = {**receipt, "subtotal": 10.05, "total": 10.05, "tax": None, "lineItems": [{**receipt["lineItems"][0], "amount": 10}]}
    assert not {c["name"]: c["passed"] for c in validate.validate(receipt, date(2026, 11, 12))}["items_sum_to_subtotal"]


def test_validation_fails_when_sums_do_not_add_up():
    rows = ocr_rows(("Tea 4.00", 10, 100, 200, 120), ("Total 5.00", 10, 200, 200, 220))
    checks = {c["name"]: c["passed"] for c in validate.validate(extract.extract_receipt(rows, "ppocr-v6"), date(2026, 11, 12))}
    assert checks["total_matches_parts"] is False


def test_future_date_and_low_confidence_fail():
    rows = ocr_rows(("Tea 4.00", 10, 100, 200, 120), ("Total 4.00", 10, 200, 200, 220), ("Date 01/01/2030", 10, 300, 200, 320))
    receipt = extract.extract_receipt(rows, "ppocr-v6")
    assert not {c["name"]: c["passed"] for c in validate.validate(receipt, date(2026, 11, 12))}["date_not_in_future"]
    receipt["sources"]["total"]["confidence"] = 0.5
    assert not {c["name"]: c["passed"] for c in validate.validate(receipt, date(2026, 11, 12))}["confidence_floor"]


def test_html_table_rows():
    block = {
        "type": "table",
        "order": 0,
        "bbox": [0, 0, 10, 10],
        "text": "<table><tr><th>Description</th><th>Qty</th><th>Amount</th></tr><tr><td>Tea</td><td>2</td><td>$4.00</td></tr></table>",
    }
    rows = extract.rows_from_blocks([block])
    assert [[c.text for c in r.cells] for r in rows] == [["Description", "Qty", "Amount"], ["Tea", "2", "$4.00"]]


def test_expected_summary_matches_expected_offline():
    """The numbers in the README come from expected/smoke-summary.json; it must be what evaluate.py says about expected/offline.json."""
    ev = load_evaluate()
    result = json.loads((UC / "expected" / "offline.json").read_text())
    truth = json.loads((UC / "assets" / "smoke" / "truth.json").read_text())
    assert ev.evaluate(result, truth, timing=False) == json.loads((UC / "expected" / "smoke-summary.json").read_text())
