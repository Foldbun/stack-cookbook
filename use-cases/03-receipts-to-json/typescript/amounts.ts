// Parsing the strings that receipts print: amounts in both number formats, currency marks and dates.
import { Dec } from "./decimal.ts";

// #region amount-regex
// One money-looking token: optional sign or "(" before it, optional currency mark, the number, optional trailing sign.
// It must not touch letters, "/" or ":" (so dates and times do not match) and must not be followed by "%".
// A stray "." or "," just before a grouped number (".20.000", a currency mark the OCR reduced to a dot) is part of no amount.
// (JavaScript's \w is ASCII only, so "a letter or digit" is spelled out with Unicode classes.)
export const AMOUNT_RE =
  /(?<![\p{L}\p{N}_/:.,])(?:[.,](?=\d{1,3}[.,]\d{3}(?!\d)))?(?<pre>[-−(]?)\s*(?<cur>Rp\.?|IDR|USD|EUR|GBP|AUD|CAD|[$€£¥])?\s*(?<sign>[-−]?)(?<num>\d{1,3}(?:[  ]\d{3})+(?:[.,]\d{1,2})?|\d[\d.,]*\d|\d)(?<post>-|\)|\s?CR)?(?![\p{L}\p{N}_%/:])/giu;
// #endregion
const CURRENCY: Record<string, string> = { rp: "IDR", "rp.": "IDR", idr: "IDR", usd: "USD", eur: "EUR", gbp: "GBP", aud: "AUD", cad: "CAD", "€": "EUR", "£": "GBP", "¥": "JPY" };

// #region parse-amount
/** Digits with "." and "," in either convention: "1,234.50", "1.234,50", "6.000", "4,500", "12,5", "1 234,50".
 *
 * The rule: when both marks occur, the last one is the decimal mark. A mark that occurs several times is a thousands
 * mark. A single mark followed by exactly three digits is a thousands mark ("6.000" is six thousand, as on the
 * Indonesian receipts of the CORD set); followed by one or two digits it is a decimal mark. "0.500" is a decimal.
 */
export function parseNumber(text: string): Dec | null {
  const num = text.replaceAll(" ", "").replaceAll(" ", "");
  const marks = [...num].filter((c) => c === "." || c === ",");
  if (marks.length === 0) return Dec.parse(num);
  const last = Math.max(num.lastIndexOf("."), num.lastIndexOf(","));
  if (new Set(marks).size === 2) {
    // "1.234,50" or "1,234.50": the last mark is the decimal mark
    return Dec.parse(num.slice(0, last).replaceAll(".", "").replaceAll(",", "") + "." + num.slice(last + 1));
  }
  const mark = marks[0];
  if (marks.length > 1) {
    // "1.250.000": thousands marks only
    const groups = num.split(mark);
    return groups.slice(1).every((g) => g.length === 3) ? Dec.parse(groups.join("")) : null;
  }
  const [head, tail] = num.split(mark);
  if (tail.length === 3 && head !== "0" && head.length >= 1 && head.length <= 3) return Dec.parse(head + tail);
  return head ? Dec.parse(`${head}.${tail}`) : null;
}

/** The amount in a cell such as "$1,234.50", "-$23.56", "(12.00)", "12.00-" or "Rp 45.000"; null when there is none. */
export function parseAmount(text: string): Dec | null {
  const found = amountsIn(text);
  return found.length ? found[found.length - 1][0] : null;
}

/** Every money-looking token of the text as [signed value, currency mark], left to right. */
export function amountsIn(text: string): Array<[Dec, string | null]> {
  const out: Array<[Dec, string | null]> = [];
  for (const m of text.matchAll(AMOUNT_RE)) {
    const g = m.groups!;
    let value = parseNumber(g.num);
    if (value === null) continue;
    const post = (g.post ?? "").trim().toUpperCase();
    // A minus (ASCII or U+2212) before the number or the mark, "(12.00)", "12.00-" and "12.00 CR" all mean negative.
    if (g.pre === "-" || g.pre === "−" || g.sign || (g.pre === "(" && post === ")") || post === "-" || post === "CR") value = value.neg();
    out.push([value, g.cur ?? null]);
  }
  return out;
}
// #endregion

/** [ISO code, printed mark] of the first currency mark in the text. "$" alone is ambiguous, so it gives no code. */
export function currencyOf(text: string): [string | null, string | null] {
  for (const m of text.matchAll(AMOUNT_RE)) {
    const mark = m.groups!.cur;
    if (mark) return [CURRENCY[mark.toLowerCase()] ?? null, mark];
  }
  return [null, null];
}

const MONTHS: Record<string, number> = Object.fromEntries(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].map((m, i) => [m, i + 1]));
const DATE_PATTERNS: Array<[RegExp, string]> = [
  [/(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)/gu, "ymd"],
  [/(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)/gu, "dmy"],
  [/(?<![\p{L}\p{N}_])(\d{1,2})[ -]([A-Za-z]{3})[a-z]*\.?,?[ -](\d{4})(?!\d)/gu, "d-mon-y"],
  [/(?<![\p{L}\p{N}_])([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})(?!\d)/gu, "mon-d-y"],
];

/** "YYYY-MM-DD" of a real calendar date, null for 31/02 and the like. */
function isoDate(year: number, month: number, day: number): string | null {
  const d = new Date(Date.UTC(2000, month - 1, day));
  d.setUTCFullYear(year);
  if (year < 1 || year > 9999 || d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1 || d.getUTCDate() !== day) return null;
  return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

/** The first calendar date in the text as "YYYY-MM-DD": ISO, 04/08/2026, 4.8.26, 4 Aug 2026 or Aug 4, 2026.
 *
 * Numeric day/month/year is read day first unless the second number cannot be a month; `dayFirst = false` flips the default.
 */
export function parseDate(text: string, dayFirst = true): string | null {
  for (const [rx, kind] of DATE_PATTERNS) {
    for (const m of text.matchAll(rx)) {
      const [, a, b, c] = m;
      let found: string | null = null;
      if (kind === "ymd") {
        found = isoDate(Number(a), Number(b), Number(c));
      } else if (kind === "dmy") {
        const first = Number(a);
        const second = Number(b);
        const year = Number(c) + (c.length === 2 ? 2000 : 0);
        found = first > 12 || (dayFirst && second <= 12) ? isoDate(year, second, first) : isoDate(year, first, second);
      } else if (kind === "d-mon-y") {
        const month = MONTHS[b.toLowerCase()];
        found = month ? isoDate(Number(c), month, Number(a)) : null;
      } else {
        const month = MONTHS[a.toLowerCase()];
        found = month ? isoDate(Number(c), month, Number(b)) : null;
      }
      if (found) return found;
    }
  }
  return null;
}
