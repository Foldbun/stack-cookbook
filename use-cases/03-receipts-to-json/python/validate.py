"""Validation of an extracted receipt: the checks that decide whether a result can be trusted without a person."""

from datetime import date
from decimal import Decimal

TOLERANCE = Decimal("0.01")  # sums must agree to one cent (to one unit for currencies without decimals)
CONFIDENCE_FLOOR = 0.80  # minimum OCR confidence of every line a field was read from


def _d(x):
    return Decimal(str(x))


# region validate
def items_tolerance(amounts: list, subtotal) -> Decimal:
    """How far the item amounts may be from the subtotal. Normally one cent. A receipt that prints whole numbers of a large
    currency (45,500 rupiah) rounds every item to the unit, so n items can add up to n/2 units away from the subtotal; that
    is the receipt's rounding, not an error. Receipts with decimals, and small amounts, keep the cent."""
    whole = all(a == a.to_integral_value() for a in amounts) and _d(subtotal) == _d(subtotal).to_integral_value()
    return max(TOLERANCE, Decimal(len(amounts)) / 2) if whole and _d(subtotal) >= 1000 else TOLERANCE


def validate(receipt: dict, today: date) -> list[dict]:
    """Run every check. Returns [{name, passed, detail}]; a receipt is accepted when all of them passed."""
    items = receipt["lineItems"]
    total, subtotal = receipt["total"], receipt["subtotal"]
    amounts = [_d(i["amount"]) for i in items]
    items_sum = sum(amounts, Decimal(0))
    checks = []

    def check(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    check("total_present", total is not None, "a total was found" if total is not None else "no total row was found")
    check("line_items_present", items, f"{len(items)} line items")
    incomplete = [n for n, i in enumerate(items) if not i["description"] or i["amount"] is None]
    check("line_items_complete", not incomplete, "every item has a description and an amount" if not incomplete else f"items {incomplete} lack a description")
    bad = [
        n
        for n, i in enumerate(items)
        if i["quantity"] is not None and i["unitPrice"] is not None and abs(_d(i["quantity"]) * _d(i["unitPrice"]) - _d(i["amount"])) > TOLERANCE
    ]
    check("line_item_arithmetic", not bad, "quantity x unit price = amount" if not bad else f"quantity x unit price differs from the amount in items {bad}")
    if subtotal is not None:
        check(
            "items_sum_to_subtotal", abs(items_sum - _d(subtotal)) <= items_tolerance(amounts, subtotal), f"items add up to {items_sum}, subtotal is {subtotal}"
        )
    if total is not None:
        base = _d(subtotal) if subtotal is not None else items_sum
        expected = base - _d(receipt["discount"] or 0) + _d(receipt["service"] or 0) + _d(receipt["tax"] or 0)
        check("total_matches_parts", abs(expected - _d(total)) <= TOLERANCE, f"subtotal - discount + service + tax is {expected}, total is {total}")
    if receipt["date"]:
        check("date_not_in_future", date.fromisoformat(receipt["date"]) <= today, f"date {receipt['date']}")
    sources = list(receipt["sources"].items()) + [(f"lineItems[{n}]", i["source"]) for n, i in enumerate(items)]
    low = [(n, c) for n, s in sources if (c := s["confidence"]) is not None and c < CONFIDENCE_FLOOR]
    check(
        "confidence_floor",
        not low,
        f"every field line has confidence of at least {CONFIDENCE_FLOOR}" if not low else f"low confidence for {[n for n, _ in low]}",
    )
    return checks


def passed(checks: list[dict]) -> bool:
    return all(c["passed"] for c in checks)


# endregion
