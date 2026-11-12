// Unit tests of the amount and date parsing. Run with `node --test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { parseAmount, parseDate } from "./amounts.ts";
import { Dec } from "./decimal.ts";

const cases: Array<[string, string | null]> = [
  ["6.000", "6000"], // one mark and three digits: thousands
  ["4,500", "4500"],
  ["1.250.000", "1250000"],
  ["$1,234.50", "1234.50"],
  ["1.234,50", "1234.50"],
  ["Rp 45.000", "45000"],
  ["12,5", "12.5"],
  ["0.500", "0.500"],
  ["-$23.56", "-23.56"],
  ["−$23.56", "-23.56"],
  ["(12.00)", "-12.00"],
  ["12.00-", "-12.00"],
  ["Tax (6%) $26.86", "26.86"], // the 6 of "6%" is not an amount
  ["4/12/2019 6:31", null], // dates and times are not amounts
];

for (const [text, value] of cases) {
  test(`parseAmount(${JSON.stringify(text)})`, () => {
    const got = parseAmount(text);
    if (value === null) assert.equal(got, null);
    else assert.ok(got !== null && got.eq(Dec.parse(value)), `${got} is not ${value}`);
  });
}

test("money is exact: 0.1 + 0.2 is 0.3", () => {
  const sum = parseAmount("0.10")!.plus(parseAmount("0.20")!);
  assert.equal(sum.toString(), "0.30");
  assert.ok(sum.eq(Dec.parse("0.3")));
  assert.notEqual(0.1 + 0.2, 0.3); // the float sum is not
});

test("the decimal keeps what the receipt printed", () => {
  assert.equal(parseAmount("$16.00")!.toString(), "16.00");
  assert.equal(parseAmount("6.000")!.toNumber(), 6000);
  assert.equal(parseAmount("-0.07")!.toString(), "-0.07");
  assert.equal(Dec.of(1e-7).toString(), "0.0000001");
});

test("an amount with a stray leading dot", () => {
  assert.ok(parseAmount("TOTAL .20.000")!.eq(Dec.parse("20000")));
  assert.ok(parseAmount("Tea, 3.50")!.eq(Dec.parse("3.50"))); // a comma before a decimal number changes nothing
});

test("parseDate", () => {
  assert.equal(parseDate("Date: 2026-08-04"), "2026-08-04");
  assert.equal(parseDate("04/08/2026"), "2026-08-04"); // day first
  assert.equal(parseDate("04/08/2026", false), "2026-04-08");
  assert.equal(parseDate("12/13/2026"), "2026-12-13"); // 13 cannot be a month
  assert.equal(parseDate("Aug 4, 2026"), "2026-08-04");
  assert.equal(parseDate("4 August 2026"), "2026-08-04");
  assert.equal(parseDate("31/02/2026"), null);
  assert.equal(parseDate("29/02/2028"), "2028-02-29");
  assert.equal(parseDate("29/02/2026"), null);
});
