// Unit tests of the extraction rules and the sum validation. Run with `node --test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { extractReceipt, makeRow, parseItemRow, rowsFromBlocks, rowsFromOcr, summaryKind } from "./extract.ts";
import type { Box } from "./extract.ts";
import { passed, validate } from "./validate.ts";

const TODAY = "2026-11-12";

/** Rows from [text, x1, y1, x2, y2] tuples, as the OCR lines of a small receipt. */
function ocrRows(...cells: Array<[string, number, number, number, number]>) {
  return rowsFromOcr(cells.map(([text, a, b, c, d]) => ({ text, confidence: 0.95, bbox: [a, b, c, d] as Box })));
}

const checksOf = (receipt: ReturnType<typeof extractReceipt>) => Object.fromEntries(validate(receipt, TODAY).map((c) => [c.name, c.passed]));

test("a two-line item and the summary rows", () => {
  const rows = ocrRows(
    ["Coffee beans", 10, 100, 200, 120],
    ["2", 10, 130, 20, 150],
    ["3,50", 100, 130, 150, 150],
    ["7,00", 300, 130, 350, 150],
    ["Subtotal", 10, 200, 100, 220],
    ["7,00", 300, 200, 350, 220],
    ["VAT 19%", 10, 230, 100, 250],
    ["1,33", 300, 230, 350, 250],
    ["TOTAL", 10, 260, 100, 280],
    ["8,33", 300, 260, 350, 280],
    ["Cash", 10, 290, 100, 310],
    ["10,00", 300, 290, 350, 310],
  );
  const receipt = extractReceipt(rows, "ppocr-v6", 400);
  assert.deepEqual(
    receipt.lineItems.map((i) => [i.description, i.quantity, i.unitPrice, i.amount]),
    [["Coffee beans", 2, 3.5, 7]],
  );
  assert.deepEqual([receipt.subtotal, receipt.tax, receipt.total], [7, 1.33, 8.33]); // cash is not the total
  assert.ok(passed(validate(receipt, TODAY)));
});

test("validation fails when the sums do not add up", () => {
  const rows = ocrRows(["Tea 4.00", 10, 100, 200, 120], ["Total 5.00", 10, 200, 200, 220]);
  assert.equal(checksOf(extractReceipt(rows, "ppocr-v6")).total_matches_parts, false);
});

test("the sums are checked exactly: 0.1 + 0.2 = 0.3 passes", () => {
  const rows = ocrRows(["Pen 0.10", 10, 100, 200, 120], ["Ink 0.20", 10, 150, 200, 170], ["Total 0.30", 10, 200, 200, 220]);
  assert.ok(passed(validate(extractReceipt(rows, "ppocr-v6"), TODAY)));
});

test("a future date and a low confidence fail", () => {
  const rows = ocrRows(["Tea 4.00", 10, 100, 200, 120], ["Total 4.00", 10, 200, 200, 220], ["Date 01/01/2030", 10, 300, 200, 320]);
  const receipt = extractReceipt(rows, "ppocr-v6");
  assert.equal(checksOf(receipt).date_not_in_future, false);
  receipt.sources.total.confidence = 0.5;
  assert.equal(checksOf(receipt).confidence_floor, false);
});

test("the rows of an HTML table", () => {
  const block = {
    type: "table",
    order: 0,
    bbox: [0, 0, 10, 10] as Box,
    text: "<table><tr><th>Description</th><th>Qty</th><th>Amount</th></tr><tr><td>Tea &amp; milk</td><td>2</td><td>$4.00</td></tr></table>",
  };
  const rows = rowsFromBlocks([block]);
  assert.deepEqual(
    rows.map((r) => r.cells.map((c) => c.text)),
    [
      ["Description", "Qty", "Amount"],
      ["Tea & milk", "2", "$4.00"],
    ],
  );
});

test("a table row with quantity, unit price and amount", () => {
  const block = {
    type: "table",
    order: 0,
    bbox: [0, 0, 10, 10] as Box,
    text: "<table><tr><td>Description</td><td>Qty</td><td>Unit price</td><td>Amount</td></tr><tr><td>Tea</td><td>2</td><td>$2.00</td><td>$4.00</td></tr><tr><td>Total</td><td>$4.00</td></tr></table>",
  };
  const receipt = extractReceipt(rowsFromBlocks([block]), "paddleocr-vl", 100);
  assert.deepEqual(
    receipt.lineItems.map((i) => [i.description, i.quantity, i.unitPrice, i.amount]),
    [["Tea", 2, 2, 4]],
  );
  assert.equal(receipt.total, 4);
  assert.equal(receipt.currencyMark, "$");
  assert.ok(passed(validate(receipt, TODAY)));
});

const oneRow = (text: string) => makeRow([{ text, bbox: null, index: 0, confidence: null, size: null }]);

test("a heading with the word tax does not end the items", () => {
  const rows = ocrRows(
    ["9 Rue des Tilleuls", 10, 20, 200, 40],
    ["Tax invoice", 300, 20, 400, 40],
    ["Description", 10, 100, 100, 120],
    ["Qty", 200, 100, 230, 120],
    ["Amount", 300, 100, 380, 120],
    ["Tea", 10, 130, 100, 150],
    ["2", 200, 130, 210, 150],
    ["2,00 \u20ac", 250, 130, 290, 150],
    ["4,00 \u20ac", 300, 130, 380, 150],
    ["Total", 10, 200, 100, 220],
    ["4,00 \u20ac", 300, 200, 380, 220],
  );
  const receipt = extractReceipt(rows, "ppocr-v6", 400);
  assert.equal(receipt.tax, null);
  assert.deepEqual(
    receipt.lineItems.map((i) => [i.description, i.quantity, i.amount]),
    [["Tea", 2, 4]],
  );
  assert.ok(passed(validate(receipt, TODAY)));
});

test("a summary row needs an amount after its label", () => {
  assert.equal(summaryKind(oneRow("Tax invoice")), null);
  assert.equal(summaryKind(oneRow("9 Rue des Tilleuls Tax invoice")), null);
  assert.equal(summaryKind(oneRow("VAT (20%) 336,55")), "tax");
  assert.equal(summaryKind(oneRow("Cash 50.000")), "skip");
});

test("a currency mark apart from its number", () => {
  const eq = (got: ReturnType<typeof parseItemRow>, want: [string, string, string, string]) =>
    assert.deepEqual([got[0]?.toString(), got[1], got[2]?.toString(), got[3]?.toString()], want);
  eq(parseItemRow(oneRow("Consulting 5 82,37 \u20ac 411,85 \u20ac")), ["5", "Consulting", "82.37", "411.85"]);
  eq(parseItemRow(oneRow("Tea 2 $ 3.00 $ 6.00")), ["2", "Tea", "3.00", "6.00"]);
});

test("a table row wrapped after the quantity", () => {
  const rows = ocrRows(
    ["Description", 10, 100, 100, 120],
    ["Qty Unit price Amount", 200, 100, 380, 120],
    ["Rental, per day 1", 10, 130, 150, 150],
    ["20,00 \u20ac 20,00 \u20ac", 200, 160, 380, 180],
    ["Brochure, box of 250", 10, 190, 150, 210],
    ["3 4,00 \u20ac 12,00 \u20ac", 200, 220, 380, 240],
    ["Total", 10, 300, 100, 320],
    ["32,00 \u20ac", 300, 300, 380, 320],
  );
  const receipt = extractReceipt(rows, "ppocr-v6", 400);
  assert.deepEqual(
    receipt.lineItems.map((i) => [i.description, i.quantity, i.unitPrice, i.amount]),
    [
      ["Rental, per day", 1, 20, 20],
      ["Brochure, box of", 3, 4, 12],
    ],
  );
  assert.ok(passed(validate(receipt, TODAY)));
});

const vocabulary: Array<[string, string | null]> = [
  ["Net Total: Rp194,726", "subtotal"],
  ["NET Sales 35.000", "subtotal"],
  ["ITEMS:13 561,800", "subtotal"], // a count of items followed by their sum
  ["Srv Ch Rp9,736", "service"],
  ["P.Rest 10% 5,592", "tax"],
  ["PResto 10/ 2,700", "tax"],
  ["Pb 1 18,900", "tax"],
  ["3 T O T A L 174.983", "total"],
  ["Total Qty 7", "total"], // first label wins at the call site; the kind itself is still total
  ["Prestige Coffee 4.000", null], // "P" and "rest" inside a longer word are not the restaurant tax
  ["3 Items,", null], // no amount after the label
];
for (const [text, kind] of vocabulary) {
  test(`summaryKind(${JSON.stringify(text)})`, () => assert.equal(summaryKind(oneRow(text)), kind));
}

test("a discount before the subtotal is an item", () => {
  const rows = ocrRows(
    ["1 Tea", 10, 100, 100, 120],
    ["Rp10.000", 300, 100, 380, 120],
    ["1 Cake", 10, 130, 100, 150],
    ["Rp20.000", 300, 130, 380, 150],
    ["1 Discount 10%", 10, 160, 150, 180],
    ["-Rp3.000", 300, 160, 380, 180],
    ["Net Total:", 10, 200, 100, 220],
    ["Rp27.000", 300, 200, 380, 220],
    ["Srv Ch", 10, 230, 100, 250],
    ["Rp1.350", 300, 230, 380, 250],
    ["TOTAL:", 10, 260, 100, 280],
    ["Rp28.350", 300, 260, 380, 280],
  );
  const receipt = extractReceipt(rows, "ppocr-v6", 400);
  assert.deepEqual(receipt.lineItems.map((i) => i.amount), [10000, 20000, -3000]);
  assert.deepEqual([receipt.subtotal, receipt.discount, receipt.service, receipt.total], [27000, null, 1350, 28350]);
  assert.ok(passed(validate(receipt, TODAY)));
});

test("a discount echoed among the items is not an item", () => {
  const rows = ocrRows(
    ["Cake", 10, 100, 100, 120],
    ["63,636", 300, 100, 380, 120],
    ["Club Card", 10, 130, 100, 150],
    ["-9,545", 300, 130, 380, 150],
    ["SubTotal:", 10, 200, 100, 220],
    ["63,636", 300, 200, 380, 220],
    ["Discount:", 10, 230, 100, 250],
    ["-9,545", 300, 230, 380, 250],
    ["Total:", 10, 260, 100, 280],
    ["54,091", 300, 260, 380, 280],
  );
  const receipt = extractReceipt(rows, "ppocr-v6", 400);
  assert.deepEqual(receipt.lineItems.map((i) => i.description), ["Cake"]);
  assert.equal(receipt.discount, 9545);
  assert.ok(passed(validate(receipt, TODAY)));
});

test("items may differ from the subtotal by the rounding of whole numbers", () => {
  const line = (amount: number) => ({ description: "A", quantity: null, unitPrice: null, amount, source: { model: "m", lines: [], bbox: null, confidence: null } });
  const base = { merchant: null, date: null, currency: null, currencyMark: null, sources: {}, discount: null, service: null };
  // whole rupiah amounts rounded one by one: 7,727 + 33,636 = 41,363 against a printed subtotal of 41,364
  const rounded = { ...base, total: 45500, subtotal: 41364, tax: 4136, lineItems: [line(7727), line(33636)] };
  assert.ok(passed(validate(rounded, TODAY)));
  // six units off: a missing or misread item, not rounding
  assert.equal(checksOf({ ...rounded, lineItems: [line(7727), line(33630)] }).items_sum_to_subtotal, false);
  // decimals and small amounts keep the one-cent tolerance
  assert.equal(checksOf({ ...rounded, subtotal: 10.05, total: 10.05, tax: null, lineItems: [line(10)] }).items_sum_to_subtotal, false);
});
