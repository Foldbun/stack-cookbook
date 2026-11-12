# Receipts and invoices to validated JSON

English | [简体中文](README.zh-CN.md)

Read receipts and invoices with `ppocr-v6`, extract merchant, date, line items and totals by rules, check that the numbers add up, and read only the items that fail again with `paddleocr-vl`. Items that still fail go to a review file instead of into your database.

## Problem <!-- #problem -->

A receipt is a photo, and the accounting system wants JSON. OCR gives lines of text with boxes, and everything after that is yours: which line is the total, whether `6.000` is six or six thousand, and above all whether the result can be trusted without a person looking at it.

The constraints that shape the code:

- **A wrong number is worse than a missing one.** The pipeline never guesses. Every result passes checks (items add up to the subtotal, subtotal minus discount plus tax is the total, the date exists and is not in the future, the lines it was read from were read with confidence). What fails is read again, and what still fails is written to `needs_review.jsonl`.
- **The better model is 8 times the price.** `ppocr-v6` costs 0.25 credits per image and `paddleocr-vl` 2 credits per page. Using the second one for every document would make the expensive path the default, so it runs only for the items that fail.
- **Rate limits.** The public pool allows 10 calls per minute and 100 per UTC day; on a funded account the free pool serves 60 included calls per minute and 1,000 per day, and calls above that are billed. `paddleocr-vl` is in the paid pool only. The client paces calls by the `perMinute` of `GET /v1/models`, so included calls stay free.
- **Reruns must not cost twice.** A crashed or repeated run should not pay for documents it already read.
- **Amounts are printed in several ways**: `6.000` and `1,250.00` and `1.250,00`, with `Rp`, `$` or no mark, and negatives as `-23.56`, `(23.56)` or `23.56-`.

## Pipeline <!-- #pipeline -->

<!-- figure: assets/pipeline.svg -->
![Pipeline: ppocr-v6, rules, validation, fallback to paddleocr-vl, needs_review.jsonl](assets/pipeline.svg)

1. Upload the image once (`POST /v1/images`, free) and keep the handle.
2. `POST /v1/ocr` with `ppocr-v6`: lines with boxes and confidence.
3. Group the lines into rows by vertical position, then read the fields from the rows: merchant, date, currency, line items, subtotal, discount, service, tax, total. Each value keeps its provenance: the model, the line indices and the box it was read from.
4. Validate. All checks must pass.
5. On failure, `POST /v1/documents/parse` with `paddleocr-vl` on the **same handle**. Its layout blocks (tables as HTML) are turned into the same kind of rows, so the same rules and the same validation apply.
6. Validated receipts go to `receipts.jsonl`; the rest go to `needs_review.jsonl` with the reasons and the best attempt as a starting point.

The output of one item is described by `output.schema.json`. It keeps the receipt of every attempt, so the strategies "OCR only" and "hybrid" can be scored from one run.

## Run it <!-- #run -->

```bash
export FOLDBUN_API_KEY="fbs_..."
export FOLDBUN_MAX_CREDITS=5           # optional: the client refuses calls that could pass it
cd use-cases/03-receipts-to-json/python
uv run main.py --out out               # the 4 smoke documents: about 3 credits at list price
uv run main.py --out out --resume      # after an interruption: finished items are not read again
uv run main.py --out out --no-fallback # OCR only
# or: cd ../typescript && pnpm install && node main.ts --out out   # the same pipeline in TypeScript
# or: cd ../go && go run . --out out                               # the same pipeline in Go
```

Arguments: `--manifest`, `--out`, `--concurrency` (default 4), `--limit`, `--resume`, `--no-fallback`. Outputs: `result.json` (format of `contract/result.schema.json`), `receipts.jsonl`, `needs_review.jsonl`, and `items/` with one file per finished item (used by `--resume`).

The `paddleocr-vl` fallback needs a funded account. On a new account the fallback call answers `403 model_locked`; the item then stays with what `ppocr-v6` read and goes to review, and the run still completes.

To score a run: `python evaluate.py --result out/result.json --truth assets/smoke/truth.json`.

The full set (60 CORD receipts and 20 synthetic invoices, 11 MB) is a release asset of the repository (see `datasets/README.md`). Its manifest is committed as `assets/full.json`; the images and the ground truth are in the archive. Extract it into `assets/`, then run the reference implementation on it from the repository root. Reading all 80 documents is at most 180 credits at list price (every document read by `ppocr-v6` and again by `paddleocr-vl`), usually about 60, and the fallback needs a funded account:

```bash
tar xzf 03-receipts-to-json-full.tar.gz -C use-cases/03-receipts-to-json/assets
export FOLDBUN_API_KEY="fbs_..."
uv run python -m harness live --size full --uc 03 --max-credits 180 --dry-run   # the plan, nothing is sent
uv run python -m harness refresh --size full --uc 03 --max-credits 180          # runs, scores, writes expected/full.json and expected/full-summary.json, then `uv run python -m harness snippets`
```

`harness live` runs the same and only checks the result against the floors in `usecase.toml` `[quality]`; the metrics are those of `evaluate.py --result out/result.json --truth assets/truth.json`.

The documents are split by number: the odd-numbered ones (`cord-001`, `inv-003`, ...) are the development half, on which the extraction rules were written and tuned, and the even-numbered ones are the held-out half, which was only ever run, never read. `evaluate.py` prints the metrics for all documents and, with the prefix `heldout_`, for the held-out half alone (`heldout_validation_pass_rate`, ...); the `[quality]` floors apply to the held-out metrics, because numbers measured on the documents the rules were tuned on would overstate the result.

## Code <!-- #code -->

### Idempotency <!-- #code-idempotency -->

The key is derived from the sha256 of the file and the model, so the same document and model always send the same key. For 24 hours the API replays the stored answer for a repeated key without running or charging the call. The key also names the program that sends it: the API compares the bytes of the request, so two programs that write the same JSON with different spacing must not share a key (the second one gets `409 idempotency_conflict`).

<!-- code: idempotency -->
<details><summary>Python (python/main.py)</summary>

```python
def idempotency_key(image_sha256: str, model: str) -> str:
    """Same file and model give the same key (at most 128 printable ASCII characters). Within 24 hours the API
    replays the stored answer for a repeated key without running or charging the call, so a restarted run is free.
    The key names this program ("py"): the API compares the bytes of the request, and another program that writes
    the same JSON with different spacing would get `409 idempotency_conflict` under a shared key."""
    return f"r03-py-{image_sha256}-{model}"
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
/** Same file and model give the same key (at most 128 printable ASCII characters). Within 24 hours the API
 * replays the stored answer for a repeated key without running or charging the call, so a restarted run is free.
 * The key names this program ("ts"): the API compares the bytes of the request, and another program that writes
 * the same JSON with different spacing would get `409 idempotency_conflict` under a shared key. */
function idempotencyKey(imageSha256: string, model: string): string {
  return `r03-ts-${imageSha256}-${model}`;
}
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
// idempotencyKey gives the same key for the same file and model (at most 128 printable ASCII characters). Within 24 hours
// the API replays the stored answer for a repeated key without running or charging the call, so a restarted run is free.
// The key names this program ("go"): the API compares the bytes of the request, and another program that writes the
// same JSON with different spacing would get `409 idempotency_conflict` under a shared key.
func idempotencyKey(imageSHA256, model string) string {
	return "r03-go-" + imageSHA256 + "-" + model
}
```

</details>
<!-- /code -->

### Amounts <!-- #code-amounts -->

Amounts come in both number formats. The rule: when both marks occur the last one is the decimal mark, a repeated mark is a thousands mark, and a single mark followed by exactly three digits is a thousands mark. That makes `6.000` six thousand, which is right for the rupiah receipts in the set and wrong for a three-decimal currency. Money is never added as a float: Python uses `Decimal`, TypeScript a small exact decimal type over `BigInt` (`decimal.ts`), Go the same over `math/big` (`decimal.go`), so that `0.10 + 0.20` is `0.30` in the checks. Amounts become JSON numbers only when they are written to the output.

<!-- code: parse-amount -->
<details><summary>Python (python/amounts.py)</summary>

```python
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
```

</details>

<details><summary>TypeScript (typescript/amounts.ts)</summary>

```ts
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
```

</details>

<details><summary>Go (go/amounts.go)</summary>

```go
// parseNumber reads digits with "." and "," in either convention: "1,234.50", "1.234,50", "6.000", "4,500", "12,5", "1 234,50".
//
// The rule: when both marks occur, the last one is the decimal mark. A mark that occurs several times is a thousands
// mark. A single mark followed by exactly three digits is a thousands mark ("6.000" is six thousand, as on the
// Indonesian receipts of the CORD set); followed by one or two digits it is a decimal mark. "0.500" is a decimal.
func parseNumber(num string) (Dec, bool) {
	num = strings.NewReplacer("\u00a0", "", "\u202f", "").Replace(num)
	dots, commas := strings.Count(num, "."), strings.Count(num, ",")
	if dots+commas == 0 {
		return parseDec(num)
	}
	last := max(strings.LastIndex(num, "."), strings.LastIndex(num, ","))
	if dots > 0 && commas > 0 { // "1.234,50" or "1,234.50": the last mark is the decimal mark
		whole := strings.NewReplacer(".", "", ",", "").Replace(num[:last])
		return parseDec(whole + "." + num[last+1:])
	}
	mark := "."
	if commas > 0 {
		mark = ","
	}
	if dots+commas > 1 { // "1.250.000": thousands marks only
		groups := strings.Split(num, mark)
		for _, g := range groups[1:] {
			if len(g) != 3 {
				return Dec{}, false
			}
		}
		return parseDec(strings.Join(groups, ""))
	}
	head, tail, _ := strings.Cut(num, mark)
	if len(tail) == 3 && head != "0" && len(head) >= 1 && len(head) <= 3 {
		return parseDec(head + tail)
	}
	if head == "" {
		return Dec{}, false
	}
	return parseDec(head + "." + tail)
}

// amountToken is one money-looking token of a text: its signed value and the currency mark printed with it ("" when none).
type amountToken struct {
	Value Dec
	Mark  string
}

// amountsIn returns every money-looking token of the text as (signed value, currency mark), left to right.
func amountsIn(text string) []amountToken {
	var out []amountToken
	for _, m := range findAmountTokens(text) {
		value, ok := parseNumber(m.num)
		if !ok {
			continue
		}
		post := strings.ToUpper(strings.TrimSpace(m.post))
		// A minus (ASCII or U+2212) before the number or the mark, "(12.00)", "12.00-" and "12.00 CR" all mean negative.
		if m.pre == "-" || m.pre == "−" || m.sign != "" || (m.pre == "(" && post == ")") || post == "-" || post == "CR" {
			value = value.Neg()
		}
		out = append(out, amountToken{value, m.cur})
	}
	return out
}

// parseAmount returns the amount in a cell such as "$1,234.50", "-$23.56", "(12.00)", "12.00-" or "Rp 45.000"; false when there is none.
func parseAmount(text string) (Dec, bool) {
	found := amountsIn(text)
	if len(found) == 0 {
		return Dec{}, false
	}
	return found[len(found)-1].Value, true
}
```

</details>
<!-- /code -->

### Lines to rows <!-- #code-rows -->

OCR returns lines, not rows. Lines whose vertical centres are within 0.6 of a median line height share a row.

<!-- code: rows-from-ocr -->
<details><summary>Python (python/extract.py)</summary>

```python
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
```

</details>

<details><summary>TypeScript (typescript/extract.ts)</summary>

```ts
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
```

</details>

<details><summary>Go (go/extract.go)</summary>

```go
// rowsFromOCR groups OCR lines into rows. Lines whose vertical centres are within 0.6 of a median line height share a row.
func rowsFromOCR(lines []ocrLine) []Row {
	var cells []Cell
	var heights []float64
	for i, ln := range lines {
		if strings.TrimSpace(ln.Text) == "" {
			continue
		}
		h := ln.BBox[3] - ln.BBox[1]
		cells = append(cells, Cell{ln.Text, ln.BBox, true, i, ln.Confidence, true, h})
		heights = append(heights, h)
	}
	if len(cells) == 0 {
		return nil
	}
	sort.Float64s(heights)
	median := heights[len(heights)/2]
	if len(heights)%2 == 0 {
		median = (heights[len(heights)/2-1] + heights[len(heights)/2]) / 2
	}
	tolerance := 0.6 * median
	centre := func(c Cell) float64 { return (c.BBox[1] + c.BBox[3]) / 2 }
	sort.SliceStable(cells, func(a, b int) bool { return centre(cells[a]) < centre(cells[b]) })
	var groups [][]Cell
	var anchor float64
	for _, cell := range cells {
		if n := len(groups); n > 0 && abs(centre(cell)-anchor) <= tolerance {
			groups[n-1] = append(groups[n-1], cell)
		} else {
			groups = append(groups, []Cell{cell})
			anchor = centre(cell)
		}
	}
	rows := make([]Row, len(groups))
	for i, g := range groups {
		sort.SliceStable(g, func(a, b int) bool { return g[a].BBox[0] < g[b].BBox[0] })
		rows[i] = Row{g}
	}
	return rows
}
```

</details>
<!-- /code -->

### Fields <!-- #code-fields -->

The first rule that matches a row decides which field it is, so "subtotal" is tested before "total", and payment rows (cash, change, card) are recognised only to be skipped. A row is a summary row only when an amount follows its label, so a heading such as "Tax invoice" does not end the item rows. A discount printed among the items, above the subtotal row, is a negative item (the subtotal already includes it).

<!-- code: summary-rules -->
<details><summary>Python (python/extract.py)</summary>

```python
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
```

</details>

<details><summary>TypeScript (typescript/extract.ts)</summary>

```ts
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
```

</details>

<details><summary>Go (go/extract.go)</summary>

```go
// Which printed label is which field. The first rule that matches a row wins, so "subtotal" is tested before "total".
// Payment rows (cash, change, card) are recognised only to be skipped: they carry amounts that are not fields.
var summaryRules = []struct {
	kind string
	re   *regexp.Regexp
}{
	{"skip", regexp.MustCompile(`(?i)cash|change|kemba|uang|tender|paid|payment|visa|mastercard|debit|credit|go-?pay|ovo|qris|\bqr\b|card|tunai|round`)},
	// "Net total" / "net sales" print the sum of the items; "ITEMS: 13  561,800" is a count of items followed by that sum.
	{"subtotal", regexp.MustCompile(`(?i)sub\s?-?\s?total|zwischensumme|sous-total|\bnet\s?(total|sales)|\bitems?\s?[:.]?\s?\d+\b`)},
	{"discount", regexp.MustCompile(`(?i)discount|\bdisc\b|diskon|rabatt|remise|potongan|savings`)},
	{"service", regexp.MustCompile(`(?i)service|servis|\bsvc\b|\bsrv\b`)},
	// "P.Rest" / "PResto" / "Pb 1" are the Indonesian restaurant tax (pajak restoran, PB1) as receipts abbreviate it.
	{"tax", regexp.MustCompile(`(?i)\btax|\bvat\b|\bgst\b|pajak|\bppn\b|\bpb\s?1\b|\bp\.?\s?rest(o|aurant)?\b|mwst|\btva\b|\biva\b`)},
	// "T O T A L" (letters spaced out, as some thermal printers and their OCR give it) is a total too.
	{"total", regexp.MustCompile(`(?i)total|\bt\s?o\s?t\s?a\s?l\b|amount due|balance due|grand|jumlah|gesamt|summe|to pay`)},
}

var (
	strongTotal     = regexp.MustCompile(`(?i)grand|amount due|balance due|total bayar|to pay`)
	headerWords     = regexp.MustCompile(`(?i)description|item|qty|quantity|unit price|price|amount|harga|jumlah`)
	headerWordsFull = regexp.MustCompile(`(?i)^(?:description|item|qty|quantity|unit price|price|amount|harga|jumlah)$`)
	metaRow         = regexp.MustCompile(`(?i)\b(date|time|due|invoice|no|tel|phone|cashier|kasir|table|order|bill to|terms)\b`)
	notAName        = regexp.MustCompile(`(?i)invoice|receipt|\bbill\b|\btax\b|faktur|struk|nota|\bdate\b|total|\btel\b|phone`)
)

// summaryKind returns the field a row is the label of, "skip" for payment rows, "" for none. A summary row is a label
// followed by an amount: a row whose only amount comes before the label ("9 Rue des Tilleuls  Tax invoice", a street number
// next to a heading) or that has no amount at all ("Tax invoice") is not one, so it neither ends the item rows nor gives a value.
func summaryKind(row Row) string {
	text := row.Text()
	for _, rule := range summaryRules {
		if loc := rule.re.FindStringIndex(text); loc != nil && (rule.kind == "skip" || len(amountsIn(text[loc[1]:])) > 0) {
			return rule.kind
		}
	}
	return ""
}
```

</details>
<!-- /code -->

Line items come from the rows above the first summary row. A description-only row waits for the numeric row under it.

<!-- code: item-rows -->
<details><summary>Python (python/extract.py)</summary>

```python
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
```

</details>

<details><summary>TypeScript (typescript/extract.ts)</summary>

```ts
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
```

</details>

<details><summary>Go (go/extract.go)</summary>

```go
// parseItemRow returns (quantity, description, unit price, amount) of one row; any part may be nil or empty. "1 Nasi (MLY) 6.000"
// and the numeric row "1 4,500 4,500" of a two-line item are both read here.
func parseItemRow(row Row) (qty *Dec, description string, unit, amount *Dec) {
	// A currency mark printed apart from its number ("82,37 €", "$ 12.00") is dropped, so the number ends the row.
	var tokens []string
	for _, t := range strings.Fields(row.Text()) {
		if !currencyToken.MatchString(t) {
			tokens = append(tokens, t)
		}
	}
	if len(tokens) > 1 && qtyPlain.MatchString(tokens[0]) {
		d, _ := parseDec(tokens[0])
		qty, tokens = &d, tokens[1:]
	} else if len(tokens) > 0 {
		if m := qtyTimes.FindStringSubmatch(tokens[0]); m != nil {
			d, _ := parseDec(m[1] + m[2])
			qty, tokens = &d, tokens[1:]
		}
	}
	numeric := make([]bool, len(tokens))
	for i, t := range tokens {
		numeric[i] = hasDigit.MatchString(t) && len(amountsIn(t)) == 1 && !twoLetter.MatchString(currWord.ReplaceAllString(t, ""))
	}
	trailing := len(numeric)
	for trailing > 0 && numeric[trailing-1] {
		trailing--
	}
	description = strings.Trim(strings.Join(tokens[:trailing], " "), " -:")
	var prices []Dec
	for _, t := range tokens[trailing:] {
		prices = append(prices, amountsIn(t)[0].Value)
	}
	if n := len(prices); qty == nil && n >= 3 && prices[n-3].IsInteger() { // table row: quantity, unit price, amount
		qty = ptr(prices[n-3])
	}
	if n := len(prices); n > 1 {
		unit = ptr(prices[n-2])
	}
	if n := len(prices); n > 0 {
		amount = ptr(prices[n-1])
	}
	return qty, description, unit, amount
}

// quantityRow reports a description followed by one lone small whole number (the quantity of a table row whose prices wrapped
// to the next line).
func quantityRow(description string, qty, unit, amount *Dec) bool {
	return description != "" && qty == nil && unit == nil && amount != nil && amount.Cmp(intDec(1000)) < 0 && amount.IsInteger()
}

// extractItems returns the line items from the rows between the table header (when there is one) and the first summary row.
//
// A row with a description and an amount is an item. A row with a description only is held back for the next row
// when that row has an amount and no description (the two-line layout of many receipts). Without a table header, an
// amount must sit in the right 40 percent of the image: that keeps addresses and phone numbers out.
func extractItems(rows []Row, model string, width int, headerAt, stopAt int) []LineItem {
	items := []LineItem{}
	var pending *struct {
		description string
		row         Row
	}
	start := 0
	if headerAt >= 0 {
		start = headerAt + 1
	}
	body := append([]Row(nil), rows[min(start, stopAt):stopAt]...)
	for n, row := range body {
		// Wrapped table row: "Equipment rental, per day  1" and then "71,96 €  71,96 €" (a photographed table whose numbers
		// sit a little lower than the text). A description ending in a lone small integer, followed by a row that is only
		// two or more numbers, is one item: the integer is the quantity, not the amount.
		if headerAt >= 0 && n+1 < len(body) {
			hq, hd, hu, ha := parseItemRow(row)
			_, td, tu, _ := parseItemRow(body[n+1])
			if quantityRow(hd, hq, hu, ha) && td == "" && tu != nil {
				row = Row{append(append([]Cell(nil), row.Cells...), body[n+1].Cells...)}
				body[n+1] = Row{}
			}
		}
		if len(row.Cells) == 0 {
			continue
		}
		qty, description, unit, amount := parseItemRow(row)
		if headerAt < 0 && metaRow.MatchString(row.Text()) {
			continue
		}
		if amount == nil {
			pending = nil
			if description != "" && utf8.RuneCountInString(description) > 2 && !headerWordsFull.MatchString(description) {
				pending = &struct {
					description string
					row         Row
				}{description, row}
			}
			continue
		}
		if headerAt < 0 && width > 0 && description != "" {
			right, found := 0.0, false
			for _, c := range row.Cells {
				if c.HasBox && (!found || c.BBox[2] > right) {
					right, found = c.BBox[2], true
				}
			}
			if found && right < 0.6*float64(width) {
				continue
			}
		}
		cells := append([]Cell(nil), row.Cells...)
		if description == "" && pending != nil {
			description, cells = pending.description, append(append([]Cell(nil), pending.row.Cells...), cells...)
		}
		pending = nil
		items = append(items, LineItem{Description: description, Quantity: number(qty), UnitPrice: number(unit), Amount: number(amount), cells: cells})
	}
	return items
}

// number turns a parsed value into the form the JSON prints.
func number(d *Dec) *Dec {
	if d == nil {
		return nil
	}
	return ptr(d.Number())
}
```

</details>
<!-- /code -->

### Validation <!-- #code-validate -->

<!-- code: validate -->
<details><summary>Python (python/validate.py)</summary>

```python
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
```

</details>

<details><summary>TypeScript (typescript/validate.ts)</summary>

```ts
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
```

</details>

<details><summary>Go (go/validate.go)</summary>

```go
// itemsTolerance is how far the item amounts may be from the subtotal. Normally one cent. A receipt that prints whole numbers
// of a large currency (45,500 rupiah) rounds every item to the unit, so n items can add up to n/2 units away from the
// subtotal; that is the receipt's rounding, not an error. Receipts with decimals, and small amounts, keep the cent.
func itemsTolerance(items []LineItem, subtotal Dec) Dec {
	whole := subtotal.IsInteger()
	for _, i := range items {
		whole = whole && orZero(i.Amount).IsInteger()
	}
	if half := newDec(int64(len(items))*5, 1); whole && subtotal.Cmp(intDec(1000)) >= 0 && half.Cmp(tolerance) > 0 {
		return half
	}
	return tolerance
}

// validate runs every check. A receipt is accepted when all of them passed; today is the date the receipt must not be after.
func validate(receipt Receipt, today calendarDate) []Check {
	items := receipt.LineItems
	total, subtotal := receipt.Total, receipt.Subtotal
	itemsSum := intDec(0)
	for _, i := range items {
		itemsSum = itemsSum.Add(orZero(i.Amount))
	}
	var checks []Check
	check := func(name string, passed bool, detail string) { checks = append(checks, Check{name, passed, detail}) }

	check("total_present", total != nil, either(total != nil, "a total was found", "no total row was found"))
	check("line_items_present", len(items) > 0, fmt.Sprintf("%d line items", len(items)))
	var incomplete, bad []int
	for n, i := range items {
		if i.Description == "" || i.Amount == nil {
			incomplete = append(incomplete, n)
		}
		if i.Quantity != nil && i.UnitPrice != nil && i.Quantity.Mul(*i.UnitPrice).Sub(orZero(i.Amount)).Abs().Cmp(tolerance) > 0 {
			bad = append(bad, n)
		}
	}
	ints := func(n int) string { return fmt.Sprint(n) }
	check("line_items_complete", len(incomplete) == 0, either(len(incomplete) == 0,
		"every item has a description and an amount", fmt.Sprintf("items %s lack a description", listOf(incomplete, ints))))
	check("line_item_arithmetic", len(bad) == 0, either(len(bad) == 0,
		"quantity x unit price = amount", fmt.Sprintf("quantity x unit price differs from the amount in items %s", listOf(bad, ints))))
	if subtotal != nil {
		check("items_sum_to_subtotal", itemsSum.Sub(*subtotal).Abs().Cmp(itemsTolerance(items, *subtotal)) <= 0, fmt.Sprintf("items add up to %s, subtotal is %s", itemsSum, subtotal))
	}
	if total != nil {
		base := itemsSum
		if subtotal != nil {
			base = *subtotal
		}
		expected := base.Sub(orZero(receipt.Discount)).Add(orZero(receipt.Service)).Add(orZero(receipt.Tax))
		check("total_matches_parts", expected.Sub(*total).Abs().Cmp(tolerance) <= 0, fmt.Sprintf("subtotal - discount + service + tax is %s, total is %s", expected, total))
	}
	if receipt.Date != nil {
		check("date_not_in_future", *receipt.Date <= today.ISO(), "date "+*receipt.Date)
	}
	var low []string
	lowSource := func(name string, s Source) {
		if s.Confidence != nil && *s.Confidence < confidenceFloor {
			low = append(low, name)
		}
	}
	for _, name := range receipt.Sources.keys {
		lowSource(name, receipt.Sources.values[name])
	}
	for n, i := range items {
		lowSource(fmt.Sprintf("lineItems[%d]", n), i.Source)
	}
	check("confidence_floor", len(low) == 0, either(len(low) == 0,
		fmt.Sprintf("every field line has confidence of at least %g", confidenceFloor),
		fmt.Sprintf("low confidence for %s", listOf(low, func(s string) string { return "'" + s + "'" }))))
	return checks
}

func passed(checks []Check) bool {
	for _, c := range checks {
		if !c.Passed {
			return false
		}
	}
	return true
}
```

</details>
<!-- /code -->

### Calls and fallback <!-- #code-calls -->

<!-- code: ocr-call -->
<details><summary>Python (python/main.py)</summary>

```python
ocr = client.post(
    "/v1/ocr",
    {"model": OCR_MODEL, "image": sc.image_handle(handle), "options": {"words": False}},
    idempotency_key=idempotency_key(sha, OCR_MODEL),
    tag=item["id"],
)
attempts.append(attempt(OCR_MODEL, extract_receipt(rows_from_ocr(ocr["lines"]), OCR_MODEL, ocr["image"]["width"]), today))
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
const ocr = await client.post(
  "/v1/ocr",
  { model: OCR_MODEL, image: sc.imageHandle(handle), options: { words: false } },
  { idempotencyKey: idempotencyKey(sha, OCR_MODEL), tag: item.id },
);
attempts.push(attempt(OCR_MODEL, extractReceipt(rowsFromOcr(ocr.lines), OCR_MODEL, ocr.image.width), today));
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
raw, err := client.Post("/v1/ocr",
	map[string]any{"model": ocrModel, "image": stackclient.ImageHandle(handle), "options": map[string]any{"words": false}},
	stackclient.Opts{Key: idempotencyKey(sha, ocrModel), Tag: item.ID})
if err != nil {
	return Entry{}, "", err
}
var ocr struct {
	Lines []ocrLine `json:"lines"`
	Image struct {
		Width int `json:"width"`
	} `json:"image"`
}
if err := json.Unmarshal(raw, &ocr); err != nil {
	return Entry{}, "", err
}
attempts = append(attempts, attempt(ocrModel, extractReceipt(rowsFromOCR(ocr.Lines), ocrModel, ocr.Image.Width), today))
```

</details>
<!-- /code -->

The second call reads the same handle: no second upload and no second image on the wire.

<!-- code: fallback-call -->
<details><summary>Python (python/main.py)</summary>

```python
if use_fallback and not attempts[0]["passed"]:
    try:
        page = client.post(
            "/v1/documents/parse",
            {"model": FALLBACK_MODEL, "image": sc.image_handle(handle), "options": {"blocks": True}},
            idempotency_key=idempotency_key(sha, FALLBACK_MODEL),
            tag=item["id"],
        )
        attempts.append(attempt(FALLBACK_MODEL, extract_receipt(rows_from_blocks(page["blocks"]), FALLBACK_MODEL, page["image"]["width"]), today))
    except sc.StackError as err:
        if err.code == "budget_exceeded":
            raise
        # An account without the paid pool (model_locked) or a failed call: the item stays with what ppocr-v6 read.
        attempts.append({"model": FALLBACK_MODEL, "receipt": None, "passed": False, "checks": [], "note": err.code})
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
if (useFallback && !attempts[0].passed) {
  try {
    const page = await client.post(
      "/v1/documents/parse",
      { model: FALLBACK_MODEL, image: sc.imageHandle(handle), options: { blocks: true } },
      { idempotencyKey: idempotencyKey(sha, FALLBACK_MODEL), tag: item.id },
    );
    attempts.push(attempt(FALLBACK_MODEL, extractReceipt(rowsFromBlocks(page.blocks), FALLBACK_MODEL, page.image.width), today));
  } catch (err) {
    if (!(err instanceof sc.StackError) || err.code === "budget_exceeded") throw err;
    // An account without the paid pool (model_locked) or a failed call: the item stays with what ppocr-v6 read.
    attempts.push({ model: FALLBACK_MODEL, receipt: null, passed: false, checks: [], note: err.code });
  }
}
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
if useFallback && !attempts[0].Passed {
	raw, err := client.Post("/v1/documents/parse",
		map[string]any{"model": fallbackModel, "image": stackclient.ImageHandle(handle), "options": map[string]any{"blocks": true}},
		stackclient.Opts{Key: idempotencyKey(sha, fallbackModel), Tag: item.ID})
	var apiErr *stackclient.Error
	switch {
	case errors.As(err, &apiErr) && apiErr.Code != "budget_exceeded":
		// An account without the paid pool (model_locked) or a failed call: the item stays with what ppocr-v6 read.
		attempts = append(attempts, Attempt{Model: fallbackModel, Note: apiErr.Code})
	case err != nil:
		return Entry{}, "", err
	default:
		var page struct {
			Blocks []block `json:"blocks"`
			Image  struct {
				Width int `json:"width"`
			} `json:"image"`
		}
		if err := json.Unmarshal(raw, &page); err != nil {
			return Entry{}, "", err
		}
		attempts = append(attempts, attempt(fallbackModel, extractReceipt(rowsFromBlocks(page.Blocks), fallbackModel, page.Image.Width), today))
	}
}
```

</details>
<!-- /code -->

### Concurrency, resume and review <!-- #code-run -->

<!-- code: concurrency -->
<details><summary>Python (python/main.py)</summary>

```python
# The client allows `--concurrency` requests in flight and spaces calls by the per-minute limit of the model's pool,
# so the worker threads here only keep it busy. One item is one thread: its calls are sequential (OCR, then maybe parse).
code, lock = 0, threading.Lock()
with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
    futures = {pool.submit(process_item, client, item, base, use_fallback, today): item for item in todo}
    for future in as_completed(futures):
        item = futures[future]
        try:
            entry = future.result()
        except sc.StackError as err:
            print(f"{item['id']}: {err.status} {err.code}: {err.message}" + (f" (request {err.request_id})" if err.request_id else ""), file=sys.stderr)
            if err.code in ("model_locked", "budget_exceeded"):
                code = EXIT_TIER if err.code == "model_locked" else EXIT_BUDGET
                for f in futures:
                    f.cancel()
                continue
            entry = {
                "id": item["id"],
                "status": "error",
                "calls": client.calls(item["id"]),
                "output": None,
                "error": {"status": err.status, "code": err.code, "message": err.message},
            }
        entries[item["id"]] = entry
        if entry["status"] == "ok":
            save_done(args.out, entry, use_fallback)
            with lock:
                print(f"{item['id']}: {entry['output']['outcome']} ({entry['output']['model']})")
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
// The client allows `--concurrency` requests in flight and spaces calls by the per-minute limit of the model's pool,
// so the workers here only keep it busy. One item is one task: its calls are sequential (OCR, then maybe parse).
let code = 0;
let next = 0;
async function worker(): Promise<void> {
  while (code === 0 && next < todo.length) {
    const item = todo[next++];
    let entry: Entry;
    try {
      entry = await processItem(client, item, base, useFallback, today);
    } catch (err) {
      if (!(err instanceof sc.StackError)) throw err;
      console.error(`${item.id}: ${err.describe()}`);
      if (err.code === "model_locked" || err.code === "budget_exceeded") {
        code = err.code === "model_locked" ? EXIT_TIER : EXIT_BUDGET; // the others stop picking up new items
        continue;
      }
      entry = { id: item.id, status: "error", calls: client.calls(item.id), output: null, error: { status: err.status, code: err.code, message: err.message } };
    }
    entries.set(item.id, entry);
    if (entry.status === "ok") {
      await saveDone(args.out, entry, useFallback);
      console.log(`${item.id}: ${entry.output.outcome} (${entry.output.model})`);
    }
  }
}
await Promise.all(Array.from({ length: Math.max(1, concurrency) }, worker));
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
// The client allows `--concurrency` requests in flight and spaces calls by the per-minute limit of the model's pool,
// so the worker goroutines here only keep it busy. One item is one goroutine: its calls are sequential (OCR, then maybe parse).
var (
	mu     sync.Mutex
	wg     sync.WaitGroup
	halt   atomic.Bool
	code   int
	failed error
)
jobs := make(chan manifestItem)
for w := 0; w < max(*concurrency, 1); w++ {
	wg.Add(1)
	go func() {
		defer wg.Done()
		for item := range jobs {
			if halt.Load() {
				continue
			}
			entry, sha, err := processItem(client, item, base, useFallback, today)
			var apiErr *stackclient.Error
			mu.Lock()
			switch {
			case errors.As(err, &apiErr):
				fmt.Fprintf(os.Stderr, "%s: %s\n", item.ID, apiErr)
				if apiErr.Code == "model_locked" || apiErr.Code == "budget_exceeded" {
					code = exitTier
					if apiErr.Code == "budget_exceeded" {
						code = exitBudget
					}
					halt.Store(true)
					break
				}
				entries[item.ID] = Entry{ID: item.ID, Status: "error", Calls: client.Calls(item.ID), Output: json.RawMessage("null"),
					Error: &itemError{apiErr.Status, apiErr.Code, apiErr.Message}}
			case err != nil:
				fmt.Fprintf(os.Stderr, "%s: %v\n", item.ID, err)
				failed = err
				halt.Store(true)
			default:
				entries[item.ID] = entry
				if err := saveDone(*out, entry, sha, useFallback); err != nil {
					fmt.Fprintln(os.Stderr, err)
				}
				var o Output
				_ = json.Unmarshal(entry.Output, &o)
				fmt.Printf("%s: %s (%s)\n", item.ID, o.Outcome, o.Model)
			}
			mu.Unlock()
		}
	}()
}
for _, item := range todo {
	jobs <- item
}
close(jobs)
wg.Wait()
```

</details>
<!-- /code -->

<!-- code: resume -->
<details><summary>Python (python/main.py)</summary>

```python
def item_file(out: Path, item_id: str) -> Path:
    return out / "items" / f"{item_id}.json"


def load_done(out: Path, item: dict, base: Path, use_fallback: bool) -> dict | None:
    """The saved entry of an item finished by an earlier run of this directory, when the file, the rules and the options are unchanged."""
    path = item_file(out, item["id"])
    if not path.exists():
        return None
    saved = json.loads(path.read_text())
    key = [hashlib.sha256((base / item["image"]).read_bytes()).hexdigest(), PIPELINE, use_fallback]
    return saved["entry"] if saved["key"] == key and saved["entry"]["status"] == "ok" else None


def save_done(out: Path, entry: dict, use_fallback: bool):
    """One small file per finished item, written atomically, so a killed run loses at most the items in flight."""
    path = item_file(out, entry["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in entry.items() if k != "_sha256"}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"key": [entry["_sha256"], PIPELINE, use_fallback], "entry": clean}))
    os.replace(tmp, path)
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
const itemFile = (out: string, itemId: string): string => join(out, "items", `${itemId}.json`);

/** The saved entry of an item finished by an earlier run of this directory, when the file, the rules and the options are unchanged. */
async function loadDone(out: string, item: Item, base: string, useFallback: boolean): Promise<Entry | null> {
  const path = itemFile(out, item.id);
  if (!existsSync(path)) return null;
  const saved = JSON.parse(await readFile(path, "utf8"));
  const key = [sha256(await readFile(join(base, item.image))), PIPELINE, useFallback];
  return JSON.stringify(saved.key) === JSON.stringify(key) && saved.entry.status === "ok" ? saved.entry : null;
}

/** One small file per finished item, written atomically, so a killed run loses at most the items in flight. */
async function saveDone(out: string, entry: Entry, useFallback: boolean): Promise<void> {
  const path = itemFile(out, entry.id);
  await mkdir(dirname(path), { recursive: true });
  const { _sha256, ...clean } = entry;
  const tmp = `${path}.tmp`;
  await writeFile(tmp, JSON.stringify({ key: [_sha256, PIPELINE, useFallback], entry: clean }));
  await rename(tmp, path);
}
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
func itemFile(out, itemID string) string { return filepath.Join(out, "items", itemID+".json") }

type savedItem struct {
	Key   []any `json:"key"`
	Entry Entry `json:"entry"`
}

func resumeKey(sha string, useFallback bool) []any { return []any{sha, pipeline, useFallback} }

// loadDone returns the saved entry of an item finished by an earlier run of this directory, when the file, the rules and the options are unchanged.
func loadDone(out string, item manifestItem, base string, useFallback bool) *Entry {
	raw, err := os.ReadFile(itemFile(out, item.ID))
	if err != nil {
		return nil
	}
	var saved savedItem
	if json.Unmarshal(raw, &saved) != nil {
		return nil
	}
	data, err := os.ReadFile(filepath.Join(base, item.Image))
	if err != nil {
		return nil
	}
	sum := sha256.Sum256(data)
	want, _ := json.Marshal(resumeKey(hex.EncodeToString(sum[:]), useFallback))
	have, _ := json.Marshal(saved.Key)
	if string(want) != string(have) || saved.Entry.Status != "ok" {
		return nil
	}
	return &saved.Entry
}

// saveDone writes one small file per finished item, atomically, so a killed run loses at most the items in flight.
func saveDone(out string, entry Entry, sha string, useFallback bool) error {
	path := itemFile(out, entry.ID)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	raw, err := json.Marshal(savedItem{resumeKey(sha, useFallback), entry})
	if err != nil {
		return err
	}
	tmp := strings.TrimSuffix(path, ".json") + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
```

</details>
<!-- /code -->

<!-- code: needs-review -->
<details><summary>Python (python/main.py)</summary>

```python
# What no model could make pass goes to a person, with the best receipt as a starting point and the reasons.
with (args.out / "needs_review.jsonl").open("w") as review, (args.out / "receipts.jsonl").open("w") as good:
    for entry in done:
        out = entry["output"]
        if out is None:
            continue
        if out["outcome"] == "validated":
            good.write(json.dumps({"id": entry["id"], "model": out["model"], "receipt": out["receipt"]}, ensure_ascii=False) + "\n")
        else:
            reasons = [c["detail"] for c in out["checks"] if not c["passed"]]
            review.write(json.dumps({"id": entry["id"], "reasons": reasons, "model": out["model"], "receipt": out["receipt"]}, ensure_ascii=False) + "\n")
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
// What no model could make pass goes to a person, with the best receipt as a starting point and the reasons.
const receipts: string[] = [];
const review: string[] = [];
for (const entry of done) {
  const out = entry.output;
  if (out === null) continue;
  if (out.outcome === "validated") {
    receipts.push(JSON.stringify({ id: entry.id, model: out.model, receipt: out.receipt }));
  } else {
    const reasons = out.checks.filter((c: Check) => !c.passed).map((c: Check) => c.detail);
    review.push(JSON.stringify({ id: entry.id, reasons, model: out.model, receipt: out.receipt }));
  }
}
await writeFile(join(args.out, "receipts.jsonl"), receipts.map((l) => l + "\n").join(""));
await writeFile(join(args.out, "needs_review.jsonl"), review.map((l) => l + "\n").join(""));
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
// What no model could make pass goes to a person, with the best receipt as a starting point and the reasons.
review, err := os.Create(filepath.Join(out, "needs_review.jsonl"))
if err != nil {
	return err
}
defer review.Close()
good, err := os.Create(filepath.Join(out, "receipts.jsonl"))
if err != nil {
	return err
}
defer good.Close()
for _, entry := range done {
	var o struct {
		Outcome string          `json:"outcome"`
		Model   string          `json:"model"`
		Receipt json.RawMessage `json:"receipt"`
		Checks  []Check         `json:"checks"`
	}
	if len(entry.Output) == 0 || string(entry.Output) == "null" || json.Unmarshal(entry.Output, &o) != nil {
		continue
	}
	var line any
	if o.Outcome == "validated" {
		line = struct {
			ID      string          `json:"id"`
			Model   string          `json:"model"`
			Receipt json.RawMessage `json:"receipt"`
		}{entry.ID, o.Model, o.Receipt}
	} else {
		reasons := []string{}
		for _, c := range o.Checks {
			if !c.Passed {
				reasons = append(reasons, c.Detail)
			}
		}
		line = struct {
			ID      string          `json:"id"`
			Reasons []string        `json:"reasons"`
			Model   string          `json:"model"`
			Receipt json.RawMessage `json:"receipt"`
		}{entry.ID, reasons, o.Model, o.Receipt}
	}
	w := review
	if o.Outcome == "validated" {
		w = good
	}
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(line); err != nil {
		return err
	}
}
```

</details>
<!-- /code -->

## Results <!-- #results -->

The numbers come from `evaluate.py`. Until a live run is recorded they come from `expected/offline.json`, which was produced against the offline fake API with hand-written responses (`fixtures/synthetic.json`). They show how the pipeline behaves, not how the models perform, and the offline run says nothing about latency.

Smoke set (4 documents), offline fixtures:

<!-- metrics: expected/smoke-summary.json -->
| Metric | Value |
|---|---|
| `items` | 4 |
| `items_scored` | 4 |
| `total_accuracy` | 1 |
| `date_accuracy` | 1 |
| `line_item_count_accuracy` | 1 |
| `merchant_accuracy` | 1 |
| `accepted_total_accuracy` | 1 |
| `accepted_date_accuracy` | 1 |
| `accepted_line_item_count_accuracy` | 1 |
| `accepted_merchant_accuracy` | 1 |
| `ocr_only_total_accuracy` | 1 |
| `ocr_only_date_accuracy` | 1 |
| `ocr_only_line_item_count_accuracy` | 1 |
| `ocr_only_merchant_accuracy` | 1 |
| `validation_pass_rate` | 1 |
| `ocr_only_validation_pass_rate` | 0.75 |
| `fallback_rate` | 0.25 |
| `needs_review_rate` | 0 |
| `ocr_only_needs_review_rate` | 0.25 |
| `ocr_only_cost_per_1000_list` | 250 |
| `hybrid_cost_per_1000_list` | 750 |
| `hybrid_cost_per_1000_charged` | 500 |
<!-- /metrics -->

Full set (80 documents), live run; filled in from `expected/full-summary.json` once a maintainer has recorded one:

<!-- metrics: expected/full-summary.json -->
| Metric | Value |
|---|---|
| `items` | 80 |
| `items_scored` | 80 |
| `total_accuracy` | 0.9625 |
| `date_accuracy` | 1 |
| `line_item_count_accuracy` | 0.8375 |
| `merchant_accuracy` | 0.9 |
| `accepted_total_accuracy` | 0.9851 |
| `accepted_date_accuracy` | 1 |
| `accepted_line_item_count_accuracy` | 0.9104 |
| `accepted_merchant_accuracy` | 0.9444 |
| `ocr_only_total_accuracy` | 0.9 |
| `ocr_only_date_accuracy` | 0.95 |
| `ocr_only_line_item_count_accuracy` | 0.7625 |
| `ocr_only_merchant_accuracy` | 0.75 |
| `validation_pass_rate` | 0.8375 |
| `ocr_only_validation_pass_rate` | 0.7125 |
| `fallback_rate` | 0.2875 |
| `needs_review_rate` | 0.1625 |
| `ocr_only_needs_review_rate` | 0.2875 |
| `ocr_only_cost_per_1000_list` | 250 |
| `hybrid_cost_per_1000_list` | 825 |
| `hybrid_cost_per_1000_charged` | 825 |
| `heldout_total_accuracy` | 0.95 |
| `heldout_date_accuracy` | 1 |
| `heldout_line_item_count_accuracy` | 0.825 |
| `heldout_merchant_accuracy` | 0.8 |
| `heldout_accepted_total_accuracy` | 1 |
| `heldout_accepted_date_accuracy` | 1 |
| `heldout_accepted_line_item_count_accuracy` | 0.9032 |
| `heldout_accepted_merchant_accuracy` | 0.875 |
| `heldout_ocr_only_total_accuracy` | 0.875 |
| `heldout_ocr_only_date_accuracy` | 0.9 |
| `heldout_ocr_only_line_item_count_accuracy` | 0.775 |
| `heldout_ocr_only_merchant_accuracy` | 0.7 |
| `heldout_validation_pass_rate` | 0.775 |
| `heldout_ocr_only_validation_pass_rate` | 0.7 |
| `heldout_fallback_rate` | 0.3 |
| `heldout_needs_review_rate` | 0.225 |
| `heldout_ocr_only_needs_review_rate` | 0.3 |
| `heldout_ocr_only_cost_per_1000_list` | 250 |
| `heldout_hybrid_cost_per_1000_list` | 850 |
| `heldout_hybrid_cost_per_1000_charged` | 850 |
<!-- /metrics -->

Metric names: `*_accuracy` are the share of documents whose field matches the ground truth (total exact; date exact where the truth has one; line-item count exact); with no prefix, the receipts the pipeline delivered, with `accepted_` only those that passed validation, with `ocr_only_` what `ppocr-v6` alone produced. `validation_pass_rate` is the share accepted without review; `fallback_rate` the share read again; `needs_review_rate` the share that still failed. Costs are per 1,000 documents at list price (`charged` excludes what the daily allowance covered).

## Cost <!-- #cost -->

One `ppocr-v6` call is 0.25 credits and one `paddleocr-vl` call is 2 credits at list price. For N documents with a fallback rate f the list price is `0.25 N + 2 f N`: 250 credits per 1,000 documents for OCR only, and 250 + 2,000 f for the hybrid, so f = 0.1 gives 450. Inside the daily allowance the `ppocr-v6` calls are not charged; `paddleocr-vl` is always billed. Idempotent replays are not charged.

<!-- cost -->
| Run | Calls | List price (credits) | Charged (credits) |
|---|---|---|---|
| smoke | 5 | 3 | 2 |
| full | 103 | 66 | 66 |
<!-- /cost -->

## Limits and when not to use this <!-- #limits -->

- **Sums cannot see a description attached to the wrong amount.** On the photographed invoice the table is tilted, so the cells of one table row fall into different rows. The check that every item has a description catches that case, but a pairing error with complete descriptions would pass. Row geometry is the fragile part of the rules.
- **The rules are for receipts and simple invoices.** A new layout needs new rules; forms with many variable fields are better read with `paddleocr-vl` directly and located by layout block.
- **Not every field exists.** The CORD receipts in the set have no date, and their merchant is blurred, so the merchant and date are null there; the checks only apply to what is found.
- **Number formats.** `6.000` is read as six thousand. Currencies with three decimals, or a document that mixes formats, need a hint that this code does not take. `01/02/2026` is read day first.
- **OCR confusions** such as `O` for `0` are not repaired; they fail validation and go to the fallback.
- **Two readings of the same image can differ slightly.** Between two runs of the 80-document set, 2 documents came back with a different text in one place (a title block, one digit of an amount). Results are stable enough to compare runs, not to expect them identical; the idempotency key is what makes a repeated run return the stored answer.
- **Handwriting, faded thermal paper and creased photos** reduce the quality of both models; the validation then sends more items to review.
- **No tax or accounting judgement.** The checks are arithmetic, not compliance.
- The handle of an uploaded image is kept for 7 days at most; on `404 image_not_found` the client uploads again by itself.

See [Text recognition](https://stack.foldbun.com/docs/use-cases/text-recognition/), [Document parsing](https://stack.foldbun.com/docs/use-cases/document-parsing/), [Rate limits](https://stack.foldbun.com/docs/rate-limits/) and [Billing](https://stack.foldbun.com/docs/billing/) in the documentation.

## Data <!-- #data -->

The smoke set is 3 receipts from CORD v2 (NAVER Clova, CC BY 4.0, re-encoded as JPEG) and one synthetic invoice (CC0), generated for this repository, rendered with the perspective of a photograph. Attribution: `assets/smoke/ATTRIBUTION.csv`. The CORD ground truth has no merchant and no date.
