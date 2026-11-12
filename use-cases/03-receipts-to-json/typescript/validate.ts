// Validation of an extracted receipt: the checks that decide whether a result can be trusted without a person.
import { Dec, ZERO } from "./decimal.ts";
import type { Receipt } from "./extract.ts";

const TOLERANCE = Dec.parse("0.01"); // sums must agree to one cent (to one unit for currencies without decimals)
const CONFIDENCE_FLOOR = 0.8; // minimum OCR confidence of every line a field was read from

export interface Check {
  name: string;
  passed: boolean;
  detail: string;
}

const d = (x: number | null | undefined): Dec => Dec.of(x ?? 0);

// #region validate
/** How far the item amounts may be from the subtotal. Normally one cent. A receipt that prints whole numbers of a large
 * currency (45,500 rupiah) rounds every item to the unit, so n items can add up to n/2 units away from the subtotal; that
 * is the receipt's rounding, not an error. Receipts with decimals, and small amounts, keep the cent. */
export function itemsTolerance(amounts: Dec[], subtotal: number): Dec {
  const sub = d(subtotal);
  const whole = amounts.every((a) => a.isInteger()) && sub.isInteger();
  if (!whole || sub.cmp(Dec.of(1000)) < 0) return TOLERANCE;
  const half = Dec.of(amounts.length).times(Dec.parse("0.5"));
  return half.cmp(TOLERANCE) > 0 ? half : TOLERANCE;
}

/** Run every check. Returns [{name, passed, detail}]; a receipt is accepted when all of them passed. `today` is "YYYY-MM-DD". */
export function validate(receipt: Receipt, today: string): Check[] {
  const items = receipt.lineItems;
  const { total, subtotal } = receipt;
  const amounts = items.map((i) => d(i.amount));
  const itemsSum = amounts.reduce((sum, a) => sum.plus(a), ZERO);
  const checks: Check[] = [];
  const check = (name: string, passed: boolean, detail: string) => checks.push({ name, passed, detail });

  check("total_present", total !== null, total !== null ? "a total was found" : "no total row was found");
  check("line_items_present", items.length > 0, `${items.length} line items`);
  const incomplete = items.flatMap((i, n) => (!i.description || i.amount === null ? [n] : []));
  check("line_items_complete", !incomplete.length, !incomplete.length ? "every item has a description and an amount" : `items [${incomplete.join(", ")}] lack a description`);
  const bad = items.flatMap((i, n) => (i.quantity !== null && i.unitPrice !== null && d(i.quantity).times(d(i.unitPrice)).minus(d(i.amount)).abs().cmp(TOLERANCE) > 0 ? [n] : []));
  check("line_item_arithmetic", !bad.length, !bad.length ? "quantity x unit price = amount" : `quantity x unit price differs from the amount in items [${bad.join(", ")}]`);
  if (subtotal !== null) {
    check("items_sum_to_subtotal", itemsSum.minus(d(subtotal)).abs().cmp(itemsTolerance(amounts, subtotal)) <= 0, `items add up to ${itemsSum}, subtotal is ${subtotal}`);
  }
  if (total !== null) {
    const base = subtotal !== null ? d(subtotal) : itemsSum;
    const expected = base.minus(d(receipt.discount)).plus(d(receipt.service)).plus(d(receipt.tax));
    check("total_matches_parts", expected.minus(d(total)).abs().cmp(TOLERANCE) <= 0, `subtotal - discount + service + tax is ${expected}, total is ${total}`);
  }
  if (receipt.date) check("date_not_in_future", receipt.date <= today, `date ${receipt.date}`);
  const sources = [...Object.entries(receipt.sources), ...items.map((i, n) => [`lineItems[${n}]`, i.source] as const)];
  const low = sources.flatMap(([name, s]) => (s.confidence !== null && s.confidence < CONFIDENCE_FLOOR ? [name] : []));
  check("confidence_floor", !low.length, !low.length ? `every field line has confidence of at least ${CONFIDENCE_FLOOR}` : `low confidence for [${low.map((n) => `'${n}'`).join(", ")}]`);
  return checks;
}

export function passed(checks: Check[]): boolean {
  return checks.every((c) => c.passed);
}
// #endregion
