// Rule-based field extraction from recognised text.
//
// Both models are turned into the same thing first: rows of cells. `ppocr-v6` gives lines with boxes, which are grouped
// into rows by their vertical position. `paddleocr-vl` gives layout blocks: a table block is already rows and cells, any
// other block is one row per text line. One set of rules then reads the fields from the rows, whichever model made them.
import { amountsIn, currencyOf, parseDate } from "./amounts.ts";
import { Dec, ZERO } from "./decimal.ts";

export type Box = [number, number, number, number];

export interface Cell {
  text: string;
  bbox: Box | null; // [x1, y1, x2, y2] in pixels of the image
  index: number; // index of the OCR line, or of the layout block
  confidence: number | null; // null when the model does not report one
  size: number | null; // text height in pixels of an OCR line; null for layout blocks
}

export interface Row {
  cells: Cell[];
  text: string; // the cell texts joined by a space
}

export function makeRow(cells: Cell[]): Row {
  return { cells, text: cells.map((c) => c.text.trim()).join(" ") };
}

function sameCell(a: Cell, b: Cell): boolean {
  return a.text === b.text && a.index === b.index && a.confidence === b.confidence && a.size === b.size && JSON.stringify(a.bbox) === JSON.stringify(b.bbox);
}

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

const letters = (s: string): number => (s.match(/\p{L}/gu) ?? []).length;
const length = (s: string): number => [...s].length;

// #region rows-from-ocr
/** Group OCR lines into rows. Lines whose vertical centres are within 0.6 of a median line height share a row. */
export function rowsFromOcr(lines: Array<{ text: string; bbox: Box; confidence: number | null }>): Row[] {
  const cells: Cell[] = [];
  lines.forEach((ln, i) => {
    if (ln.text.trim()) cells.push({ text: ln.text, bbox: ln.bbox, index: i, confidence: ln.confidence, size: ln.bbox[3] - ln.bbox[1] });
  });
  if (!cells.length) return [];
  const tolerance = 0.6 * median(cells.map((c) => c.bbox![3] - c.bbox![1]));
  const centre = (c: Cell) => (c.bbox![1] + c.bbox![3]) / 2;
  const rows: Cell[][] = [];
  let anchor = 0;
  for (const cell of [...cells].sort((a, b) => centre(a) - centre(b))) {
    if (rows.length && Math.abs(centre(cell) - anchor) <= tolerance) {
      rows[rows.length - 1].push(cell);
    } else {
      rows.push([cell]);
      anchor = centre(cell);
    }
  }
  return rows.map((r) => makeRow(r.sort((a, b) => a.bbox![0] - b.bbox![0])));
}
// #endregion

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };

function unescapeHtml(text: string): string {
  return text.replace(/&(?:#x([0-9a-f]+)|#(\d+)|([a-z]+));/gi, (whole, hex, dec, name) => {
    if (name) return ENTITIES[name.toLowerCase()] ?? whole;
    const code = hex ? parseInt(hex, 16) : Number(dec);
    return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : whole;
  });
}

/** The rows of the first-level cells of an HTML table, as lists of strings. */
function tableRows(html: string): string[][] {
  const rows: string[][] = [];
  let cell: string[] | null = null;
  const endCell = () => {
    if (cell !== null && rows.length) {
      rows[rows.length - 1].push(cell.join("").split(/\s+/).filter(Boolean).join(" "));
      cell = null;
    }
  };
  for (const m of html.matchAll(/<!--[\s\S]*?-->|<(\/?)([a-zA-Z][^\s/>]*)[^>]*>|[^<]+|</g)) {
    const [token, closing, tag] = m;
    if (!tag) {
      if (cell !== null && !token.startsWith("<")) cell.push(unescapeHtml(token));
      continue;
    }
    const name = tag.toLowerCase();
    if (closing) {
      if (name === "td" || name === "th") endCell();
    } else if (name === "tr") {
      rows.push([]);
    } else if (name === "td" || name === "th") {
      cell = [];
      if (token.endsWith("/>")) endCell();
    }
  }
  return rows;
}

// Where Python's str.splitlines() cuts: \r\n and the single line-break characters.
const LINE_BREAK = new RegExp("\\r\\n|[\\n\\r\\v\\f\\x1c-\\x1e\\x85\\u2028\\u2029]");

// #region rows-from-blocks
/** Rows of a `paddleocr-vl` page: one row per table row (cells = table cells), one row per text line elsewhere. */
export function rowsFromBlocks(blocks: Array<{ type: string; order: number; bbox: Box; text?: string | null }>): Row[] {
  const rows: Row[] = [];
  for (const block of [...blocks].sort((a, b) => a.order - b.order)) {
    const text = block.text || "";
    const box = block.bbox;
    if (block.type === "table") {
      for (const cells of tableRows(text)) {
        if (cells.some(Boolean)) rows.push(makeRow(cells.filter(Boolean).map((t) => ({ text: t, bbox: box, index: block.order, confidence: null, size: null }))));
      }
    } else {
      for (const line of text.split(LINE_BREAK)) {
        if (line.trim()) rows.push(makeRow([{ text: line.trim(), bbox: box, index: block.order, confidence: null, size: null }]));
      }
    }
  }
  return rows;
}
// #endregion

// #region summary-rules
// Which printed label is which field. The first rule that matches a row wins, so "subtotal" is tested before "total".
// Payment rows (cash, change, card) are recognised only to be skipped: they carry amounts that are not fields.
const SUMMARY_RULES: Array<[string, RegExp]> = [
  ["skip", /cash|change|kemba|uang|tender|paid|payment|visa|mastercard|debit|credit|go-?pay|ovo|qris|\bqr\b|card|tunai|round/i],
  // "Net total" / "net sales" print the sum of the items; "ITEMS: 13  561,800" is a count of items followed by that sum.
  ["subtotal", /sub\s?-?\s?total|zwischensumme|sous-total|\bnet\s?(total|sales)|\bitems?\s?[:.]?\s?\d+\b/i],
  ["discount", /discount|\bdisc\b|diskon|rabatt|remise|potongan|savings/i],
  ["service", /service|servis|\bsvc\b|\bsrv\b/i],
  // "P.Rest" / "PResto" / "Pb 1" are the Indonesian restaurant tax (pajak restoran, PB1) as receipts abbreviate it.
  ["tax", /\btax|\bvat\b|\bgst\b|pajak|\bppn\b|\bpb\s?1\b|\bp\.?\s?rest(o|aurant)?\b|mwst|\btva\b|\biva\b/i],
  // "T O T A L" (letters spaced out, as some thermal printers and their OCR give it) is a total too.
  ["total", /total|\bt\s?o\s?t\s?a\s?l\b|amount due|balance due|grand|jumlah|gesamt|summe|to pay/i],
];
const STRONG_TOTAL = /grand|amount due|balance due|total bayar|to pay/i;
const HEADER_WORDS_SOURCE = "description|item|qty|quantity|unit price|price|amount|harga|jumlah";
const HEADER_WORDS = new RegExp(HEADER_WORDS_SOURCE, "gi");
const HEADER_WORDS_ONLY = new RegExp(`^(?:${HEADER_WORDS_SOURCE})$`, "i");
const META_ROW = /\b(date|time|due|invoice|no|tel|phone|cashier|kasir|table|order|bill to|terms)\b/i;
const CURRENCY_TOKEN = /^(?:[$€£¥]|Rp\.?|IDR|USD|EUR|GBP|AUD|CAD)$/i;
const NOT_A_NAME = /invoice|receipt|\bbill\b|\btax\b|faktur|struk|nota|\bdate\b|total|\btel\b|phone/i;

/** Which summary field the row is, or null. A summary row is a label followed by an amount: a row whose only amount comes
 * before the label ("9 Rue des Tilleuls  Tax invoice", a street number next to a heading) or that has no amount at all
 * ("Tax invoice") is not one, so it neither ends the item rows nor gives a value. */
export function summaryKind(row: Row): string | null {
  const text = row.text;
  for (const [kind, rx] of SUMMARY_RULES) {
    const m = rx.exec(text);
    if (m && (kind === "skip" || amountsIn(text.slice(m.index + m[0].length)).length)) return kind;
  }
  return null;
}
// #endregion

export interface Source {
  model: string;
  lines: number[];
  bbox: Box | null;
  confidence: number | null;
}

export interface LineItem {
  description: string;
  quantity: number | null;
  unitPrice: number | null;
  amount: number | null;
  source: Source;
}

export interface Receipt {
  merchant: string | null;
  date: string | null;
  currency: string | null;
  currencyMark: string | null;
  lineItems: LineItem[];
  sources: Record<string, Source>;
  subtotal: number | null;
  discount: number | null;
  service: number | null;
  tax: number | null;
  total: number | null;
}

function source(model: string, cells: Cell[]): Source {
  const boxes = cells.flatMap((c) => (c.bbox ? [c.bbox] : []));
  const confidences = cells.flatMap((c) => (c.confidence !== null ? [c.confidence] : []));
  return {
    model,
    lines: [...new Set(cells.map((c) => c.index))].sort((a, b) => a - b),
    bbox: boxes.length
      ? [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))]
      : null,
    confidence: confidences.length ? Math.min(...confidences) : null,
  };
}

/** The tokens of a text, split on whitespace like Python's `str.split()`. */
const words = (text: string): string[] => text.split(/\s+/).filter(Boolean);

/** `strip(" -:")` */
const stripMarks = (text: string): string => text.replace(/^[ :-]+|[ :-]+$/g, "");

// #region item-rows
/** [quantity, description, unit price, amount] of one row; any part may be null. "1 Nasi (MLY) 6.000" and the numeric row
 * "1 4,500 4,500" of a two-line item are both read here. */
export function parseItemRow(row: Row): [Dec | null, string | null, Dec | null, Dec | null] {
  // A currency mark printed apart from its number ("82,37 €", "$ 12.00") is dropped, so the number ends the row.
  let tokens = words(row.text).filter((t) => !CURRENCY_TOKEN.test(t));
  let qty: Dec | null = null;
  let m: RegExpExecArray | null;
  if (tokens.length > 1 && /^\d{1,3}$/.test(tokens[0])) {
    qty = Dec.parse(tokens[0]);
    tokens = tokens.slice(1);
  } else if (tokens.length && (m = /^(?:(\d{1,3})\s?x|x\s?(\d{1,3}))$/i.exec(tokens[0]))) {
    qty = Dec.parse(m[1] ?? m[2]);
    tokens = tokens.slice(1);
  }
  const numeric = tokens.map((t) => /\d/.test(t) && amountsIn(t).length === 1 && !/[A-Za-z]{2}/.test(t.replace("Rp", "").replace("IDR", "")));
  let trailing = numeric.length;
  while (trailing && numeric[trailing - 1]) trailing--;
  const description = stripMarks(tokens.slice(0, trailing).join(" ").trim());
  const prices = tokens.slice(trailing).map((t) => amountsIn(t)[0][0]);
  if (qty === null && prices.length >= 3 && prices[prices.length - 3].isInteger()) qty = prices[prices.length - 3]; // table row: quantity, unit price, amount
  return [qty, description || null, prices.length > 1 ? prices[prices.length - 2] : null, prices.length ? prices[prices.length - 1] : null];
}

/** A description followed by one lone small whole number (the quantity of a table row whose prices wrapped to the next line). */
function quantityRow([qty, description, unit, amount]: ReturnType<typeof parseItemRow>): boolean {
  return Boolean(description) && qty === null && unit === null && amount !== null && amount.cmp(Dec.of(1000)) < 0 && amount.isInteger();
}

interface RawItem {
  description: string;
  quantity: Dec | null;
  unitPrice: Dec | null;
  amount: Dec;
  cells: Cell[];
}

/** Line items from the rows between the table header (when there is one) and the first summary row.
 *
 * A row with a description and an amount is an item. A row with a description only is held back for the next row
 * when that row has an amount and no description (the two-line layout of many receipts). Without a table header, an
 * amount must sit in the right 40 percent of the image: that keeps addresses and phone numbers out.
 */
function extractItems(rows: Row[], width: number | null, headerAt: number | null, stopAt: number): RawItem[] {
  const items: RawItem[] = [];
  let pending: [string, Row] | null = null;
  const body = rows.slice(headerAt !== null ? headerAt + 1 : 0, stopAt);
  for (let n = 0; n < body.length; n++) {
    let row = body[n];
    // Wrapped table row: "Equipment rental, per day  1" and then "71,96 €  71,96 €" (a photographed table whose numbers
    // sit a little lower than the text). A description ending in a lone small integer, followed by a row that is only
    // two or more numbers, is one item: the integer is the quantity, not the amount.
    if (headerAt !== null && n + 1 < body.length) {
      const tail = parseItemRow(body[n + 1]);
      if (quantityRow(parseItemRow(row)) && !tail[1] && tail[2] !== null) {
        row = makeRow([...row.cells, ...body[n + 1].cells]);
        body[n + 1] = makeRow([]);
      }
    }
    if (!row.cells.length) continue;
    const [qty, parsed, unit, amount] = parseItemRow(row);
    let description = parsed;
    if (headerAt === null && META_ROW.test(row.text)) continue;
    if (amount === null) {
      pending = description && length(description) > 2 && !HEADER_WORDS_ONLY.test(description) ? [description, row] : null;
      continue;
    }
    if (headerAt === null && width && description && Math.max(...row.cells.flatMap((c) => (c.bbox ? [c.bbox[2]] : []))) < 0.6 * width) continue;
    let cells = [...row.cells];
    if (!description && pending) {
      description = pending[0];
      cells = [...pending[1].cells, ...cells];
    }
    pending = null;
    items.push({ description: description ?? "", quantity: qty, unitPrice: unit, amount, cells });
  }
  return items;
}
// #endregion

// #region fields
/** Merchant, date, currency, line items and the summary amounts, each with the cells they were read from. */
export function extractReceipt(rows: Row[], model: string, width: number | null = null): Receipt {
  const raw = rows.map(summaryKind);
  // A discount printed among the items, before the subtotal row, is an item with a negative amount: the subtotal already
  // includes it. Only a discount printed after the subtotal is subtracted from it.
  const kinds = raw.map((k, n) => (k === "discount" && raw.slice(n + 1).includes("subtotal") ? null : k));
  const firstSummary = kinds.findIndex((k) => k !== null && k !== "skip");
  const stopAt = firstSummary < 0 ? rows.length : firstSummary;
  const headerIndex = rows.slice(0, stopAt).findIndex((r) => new Set((r.text.match(HEADER_WORDS) ?? []).map((w) => w.toLowerCase())).size >= 2 && !amountsIn(r.text).length);
  const headerAt = headerIndex < 0 ? null : headerIndex;
  const receipt: Receipt = { merchant: null, date: null, currency: null, currencyMark: null, lineItems: [], sources: {}, subtotal: null, discount: null, service: null, tax: null, total: null };
  const amounts = receipt as unknown as Record<string, number | null>; // the five summary fields, by label
  // Summary rows: the last amount of a row is its value. Of several rows with the same label the first wins, unless a later
  // total says "grand" or "amount due": noisy OCR can turn "Total Kembalian" (change) into something that is not skipped.
  rows.forEach((row, n) => {
    const kind = kinds[n];
    const found = amountsIn(row.text);
    if (kind !== null && kind !== "skip" && found.length && (amounts[kind] === null || (kind === "total" && STRONG_TOTAL.test(row.text)))) {
      const last = found[found.length - 1][0];
      amounts[kind] = (kind === "discount" ? last.abs() : last).toNumber();
      receipt.sources[kind] = source(model, row.cells);
    }
  });
  const used: Cell[] = [];
  // A member or card discount is printed among the items and again as the "Discount" summary row: the same amount, negative
  // among the items. It is the discount, not an item.
  const discount = receipt.discount;
  const items = extractItems(rows, width, headerAt, stopAt).filter((i) => !(discount && i.amount.cmp(ZERO) < 0 && i.amount.neg().eq(Dec.of(discount))));
  for (const item of items) {
    used.push(...item.cells);
    receipt.lineItems.push({
      description: item.description,
      quantity: item.quantity?.toNumber() ?? null,
      unitPrice: item.unitPrice?.toNumber() ?? null,
      amount: item.amount.toNumber(),
      source: source(model, item.cells),
    });
  }
  // Date: the first date in a row that is not the due date.
  for (const row of rows) {
    if (!row.text.toLowerCase().includes("due")) {
      for (const cell of row.cells) {
        const day = parseDate(cell.text);
        if (day !== null) {
          receipt.date = day;
          receipt.sources.date = source(model, [cell]);
          break;
        }
      }
    }
    if (receipt.date) break;
  }
  // Merchant: the largest text near the top that looks like a name rather than a heading or an address.
  let names = rows
    .slice(0, Math.min(6, stopAt))
    .flatMap((r) => r.cells)
    .filter((c) => !used.some((u) => sameCell(u, c)) && letters(c.text) >= 3 && !NOT_A_NAME.test(c.text));
  names = names.filter((c) => letters(c.text) >= 0.6 * length(c.text.replaceAll(" ", "")));
  if (names.length) {
    let best = names[0];
    for (const c of names) if ((c.size ?? 0) > (best.size ?? 0) || ((c.size ?? 0) === (best.size ?? 0) && c.index < best.index)) best = c; // blocks have no size: the first candidate wins
    receipt.merchant = best.text.trim();
    receipt.sources.merchant = source(model, [best]);
  }
  rows.forEach((row, n) => {
    if (receipt.currencyMark || kinds[n] === null || kinds[n] === "skip" || !amountsIn(row.text).length) return;
    [receipt.currency, receipt.currencyMark] = currencyOf(row.text);
  });
  return receipt;
}
// #endregion
