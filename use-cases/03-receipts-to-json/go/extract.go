// Rule-based field extraction from recognised text.
//
// Both models are turned into the same thing first: rows of cells. `ppocr-v6` gives lines with boxes, which are grouped
// into rows by their vertical position. `paddleocr-vl` gives layout blocks: a table block is already rows and cells, any
// other block is one row per text line. One set of rules then reads the fields from the rows, whichever model made them.
package main

import (
	"encoding/json"
	"html"
	"regexp"
	"slices"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Cell is one piece of recognised text. It is comparable, so two cells with the same content are equal.
type Cell struct {
	Text    string
	BBox    [4]float64 // x1, y1, x2, y2 in pixels of the image
	HasBox  bool
	Index   int // index of the OCR line, or of the layout block
	Conf    float64
	HasConf bool    // false when the model does not report a confidence
	Size    float64 // text height in pixels of an OCR line; 0 for layout blocks
}

type Row struct{ Cells []Cell }

func (r Row) Text() string {
	parts := make([]string, len(r.Cells))
	for i, c := range r.Cells {
		parts[i] = strings.TrimSpace(c.Text)
	}
	return strings.Join(parts, " ")
}

// ocrLine and block are the parts of the two API answers that the rules read.
type ocrLine struct {
	Text       string     `json:"text"`
	Confidence float64    `json:"confidence"`
	BBox       [4]float64 `json:"bbox"`
}

type block struct {
	Type  string     `json:"type"`
	Order int        `json:"order"`
	BBox  [4]float64 `json:"bbox"`
	Text  *string    `json:"text"`
}

// region rows-from-ocr

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

// endregion

func abs(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}

// tableRows returns the rows of the first-level cells of an HTML table, as lists of strings. It follows the tags the
// way a streaming parser does: "tr" opens a row, "td" and "th" open a cell, their end tags close it.
func tableRows(markup string) [][]string {
	var rows [][]string
	var cell []string
	open := false
	for i := 0; i < len(markup); {
		if markup[i] != '<' {
			j := strings.IndexByte(markup[i:], '<')
			if j < 0 {
				j = len(markup) - i
			}
			if open {
				cell = append(cell, html.UnescapeString(markup[i:i+j]))
			}
			i += j
			continue
		}
		j := strings.IndexByte(markup[i:], '>')
		if j < 0 {
			break
		}
		tag := markup[i+1 : i+j]
		i += j + 1
		if tag == "" || tag[0] == '!' || tag[0] == '?' {
			continue
		}
		closing := tag[0] == '/'
		name := strings.ToLower(strings.TrimLeft(tag, "/"))
		if k := strings.IndexFunc(name, func(r rune) bool { return unicode.IsSpace(r) || r == '/' }); k >= 0 {
			name = name[:k]
		}
		switch {
		case !closing && name == "tr":
			rows = append(rows, []string{})
		case !closing && (name == "td" || name == "th"):
			cell, open = nil, true
		case closing && (name == "td" || name == "th") && open && len(rows) > 0:
			rows[len(rows)-1] = append(rows[len(rows)-1], strings.Join(strings.Fields(strings.Join(cell, "")), " "))
			open = false
		}
	}
	return rows
}

// region rows-from-blocks

// rowsFromBlocks returns the rows of a `paddleocr-vl` page: one row per table row (cells = table cells), one row per text line elsewhere.
func rowsFromBlocks(blocks []block) []Row {
	sorted := append([]block(nil), blocks...)
	sort.SliceStable(sorted, func(a, b int) bool { return sorted[a].Order < sorted[b].Order })
	var rows []Row
	for _, b := range sorted {
		text := ""
		if b.Text != nil {
			text = *b.Text
		}
		if b.Type == "table" {
			for _, cells := range tableRows(text) {
				var row Row
				for _, t := range cells {
					if t != "" {
						row.Cells = append(row.Cells, Cell{Text: t, BBox: b.BBox, HasBox: true, Index: b.Order})
					}
				}
				if len(row.Cells) > 0 {
					rows = append(rows, row)
				}
			}
			continue
		}
		for _, line := range strings.FieldsFunc(text, func(r rune) bool { return strings.ContainsRune("\n\r\v\f\x1c\x1d\x1e\u0085  ", r) }) {
			if line = strings.TrimSpace(line); line != "" {
				rows = append(rows, Row{[]Cell{{Text: line, BBox: b.BBox, HasBox: true, Index: b.Order}}})
			}
		}
	}
	return rows
}

// endregion

// region summary-rules

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

// endregion

// Source says where a value was read: the model, the OCR lines or blocks, their joint box and the lowest confidence.
type Source struct {
	Model      string      `json:"model"`
	Lines      []int       `json:"lines"`
	BBox       *[4]float64 `json:"bbox"`
	Confidence *float64    `json:"confidence"`
}

func sourceOf(model string, cells []Cell) Source {
	seen := map[int]bool{}
	lines := []int{}
	var box *[4]float64
	var conf *float64
	for _, c := range cells {
		if !seen[c.Index] {
			seen[c.Index] = true
			lines = append(lines, c.Index)
		}
		if c.HasBox {
			if box == nil {
				b := c.BBox
				box = &b
			} else {
				box[0], box[1], box[2], box[3] = min(box[0], c.BBox[0]), min(box[1], c.BBox[1]), max(box[2], c.BBox[2]), max(box[3], c.BBox[3])
			}
		}
		if c.HasConf && (conf == nil || c.Conf < *conf) {
			v := c.Conf
			conf = &v
		}
	}
	sort.Ints(lines)
	return Source{model, lines, box, conf}
}

// Sources keeps its keys in the order they were first set, so the JSON reads in the order the fields were found.
type Sources struct {
	keys   []string
	values map[string]Source
}

func (s *Sources) Set(key string, v Source) {
	if s.values == nil {
		s.values = map[string]Source{}
	}
	if _, ok := s.values[key]; !ok {
		s.keys = append(s.keys, key)
	}
	s.values[key] = v
}

func (s Sources) MarshalJSON() ([]byte, error) {
	out := []byte{'{'}
	for i, k := range s.keys {
		if i > 0 {
			out = append(out, ',')
		}
		key, _ := json.Marshal(k)
		val, err := json.Marshal(s.values[k])
		if err != nil {
			return nil, err
		}
		out = append(append(append(out, key...), ':'), val...)
	}
	return append(out, '}'), nil
}

type LineItem struct {
	Description string `json:"description"`
	Quantity    *Dec   `json:"quantity"`
	UnitPrice   *Dec   `json:"unitPrice"`
	Amount      *Dec   `json:"amount"`
	Source      Source `json:"source"`
	cells       []Cell // the cells the item was read from; not part of the output
}

type Receipt struct {
	Merchant     *string    `json:"merchant"`
	Date         *string    `json:"date"`
	Currency     *string    `json:"currency"`
	CurrencyMark *string    `json:"currencyMark"`
	LineItems    []LineItem `json:"lineItems"`
	Sources      Sources    `json:"sources"`
	Subtotal     *Dec       `json:"subtotal"`
	Discount     *Dec       `json:"discount"`
	Service      *Dec       `json:"service"`
	Tax          *Dec       `json:"tax"`
	Total        *Dec       `json:"total"`
}

// field returns the summary field of a kind ("subtotal", "discount", "service", "tax", "total").
func (r *Receipt) field(kind string) **Dec {
	switch kind {
	case "subtotal":
		return &r.Subtotal
	case "discount":
		return &r.Discount
	case "service":
		return &r.Service
	case "tax":
		return &r.Tax
	}
	return &r.Total
}

var (
	qtyPlain  = regexp.MustCompile(`^\d{1,3}$`)
	qtyTimes  = regexp.MustCompile(`(?i)^(?:(\d{1,3})\s?x|x\s?(\d{1,3}))$`)
	hasDigit  = regexp.MustCompile(`\d`)
	twoLetter = regexp.MustCompile(`[A-Za-z]{2}`)
	currWord  = regexp.MustCompile(`Rp|IDR`)
	// a currency mark that stands alone as a token
	currencyToken = regexp.MustCompile(`(?i)^(?:[$€£¥]|Rp\.?|IDR|USD|EUR|GBP|AUD|CAD)$`)
)

func ptr[T any](v T) *T { return &v }

// region item-rows

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

// endregion

func countLetters(s string) int {
	n := 0
	for _, r := range s {
		if unicode.IsLetter(r) {
			n++
		}
	}
	return n
}

// region fields

// extractReceipt reads the merchant, date, currency, line items and the summary amounts, each with the cells they were read from.
// width is the width of the image in pixels, or 0 when unknown.
func extractReceipt(rows []Row, model string, width int) Receipt {
	kinds := make([]string, len(rows))
	for i, r := range rows {
		kinds[i] = summaryKind(r)
	}
	// A discount printed among the items, before the subtotal row, is an item with a negative amount: the subtotal already
	// includes it. Only a discount printed after the subtotal is subtracted from it.
	for i := range kinds {
		if kinds[i] == "discount" && slices.Contains(kinds[i+1:], "subtotal") {
			kinds[i] = ""
		}
	}
	isField := func(kind string) bool { return kind != "" && kind != "skip" }
	stopAt := len(rows)
	for i, k := range kinds {
		if isField(k) {
			stopAt = i
			break
		}
	}
	headerAt := -1
	for i, r := range rows[:stopAt] {
		words := map[string]bool{}
		for _, w := range headerWords.FindAllString(r.Text(), -1) {
			words[strings.ToLower(w)] = true
		}
		if len(words) >= 2 && len(amountsIn(r.Text())) == 0 {
			headerAt = i
			break
		}
	}
	receipt := Receipt{LineItems: []LineItem{}}
	// Summary rows: the last amount of a row is its value. Of several rows with the same label the first wins, unless a later
	// total says "grand" or "amount due": noisy OCR can turn "Total Kembalian" (change) into something that is not skipped.
	for i, row := range rows {
		kind := kinds[i]
		found := amountsIn(row.Text())
		if !isField(kind) || len(found) == 0 {
			continue
		}
		if slot := receipt.field(kind); *slot == nil || (kind == "total" && strongTotal.MatchString(row.Text())) {
			value := found[len(found)-1].Value
			if kind == "discount" {
				value = value.Abs()
			}
			*slot = number(&value)
			receipt.Sources.Set(kind, sourceOf(model, row.Cells))
		}
	}
	var used []Cell
	// A member or card discount is printed among the items and again as the "Discount" summary row: the same amount, negative
	// among the items. It is the discount, not an item.
	items := extractItems(rows, model, width, headerAt, stopAt)
	if d := receipt.Discount; d != nil && d.Cmp(intDec(0)) != 0 {
		items = slices.DeleteFunc(items, func(i LineItem) bool {
			return i.Amount != nil && i.Amount.Cmp(intDec(0)) < 0 && i.Amount.Neg().Cmp(*d) == 0
		})
	}
	for _, item := range items {
		used = append(used, item.cells...)
		item.Source = sourceOf(model, item.cells)
		receipt.LineItems = append(receipt.LineItems, item)
	}
	// Date: the first date in a row that is not the due date.
	for _, row := range rows {
		if !strings.Contains(strings.ToLower(row.Text()), "due") {
			for _, cell := range row.Cells {
				if day, ok := parseDate(cell.Text, true); ok {
					receipt.Date = ptr(day.ISO())
					receipt.Sources.Set("date", sourceOf(model, []Cell{cell}))
					break
				}
			}
		}
		if receipt.Date != nil {
			break
		}
	}
	// Merchant: the largest text near the top that looks like a name rather than a heading or an address.
	var names []Cell
	for _, r := range rows[:min(6, stopAt)] {
		for _, c := range r.Cells {
			letters := countLetters(c.Text)
			if letters >= 3 && !notAName.MatchString(c.Text) && !contains(used, c) &&
				float64(letters) >= 0.6*float64(utf8.RuneCountInString(strings.ReplaceAll(c.Text, " ", ""))) {
				names = append(names, c)
			}
		}
	}
	if len(names) > 0 {
		best := names[0] // blocks have no size: the first candidate wins
		for _, c := range names[1:] {
			if c.Size > best.Size || (c.Size == best.Size && c.Index < best.Index) {
				best = c
			}
		}
		receipt.Merchant = ptr(strings.TrimSpace(best.Text))
		receipt.Sources.Set("merchant", sourceOf(model, []Cell{best}))
	}
	for i, row := range rows {
		if isField(kinds[i]) && len(amountsIn(row.Text())) > 0 {
			code, mark := currencyOf(row.Text())
			receipt.Currency, receipt.CurrencyMark = nil, nil
			if code != "" {
				receipt.Currency = ptr(code)
			}
			if mark != "" {
				receipt.CurrencyMark = ptr(mark)
				break
			}
		}
	}
	return receipt
}

// endregion

func contains(cells []Cell, c Cell) bool {
	for _, u := range cells {
		if u == c {
			return true
		}
	}
	return false
}
