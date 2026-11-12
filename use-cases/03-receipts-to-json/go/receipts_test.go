package main

import (
	"reflect"
	"strings"
	"testing"
)

func mustDec(t *testing.T, s string) Dec {
	t.Helper()
	d, ok := parseDec(s)
	if !ok {
		t.Fatalf("bad test value %q", s)
	}
	return d
}

func TestParseAmount(t *testing.T) {
	tests := []struct {
		text string
		want string // "" when there is no amount
	}{
		{"6.000", "6000"}, // one mark and three digits: thousands
		{"4,500", "4500"},
		{"1.250.000", "1250000"},
		{"$1,234.50", "1234.50"},
		{"1.234,50", "1234.50"},
		{"Rp 45.000", "45000"},
		{"12,5", "12.5"},
		{"0.500", "0.500"},
		{"-$23.56", "-23.56"},
		{"−$23.56", "-23.56"},
		{"(12.00)", "-12.00"},
		{"12.00-", "-12.00"},
		{"12.00 CR", "-12.00"},
		{"1\u00a0234,50", "1234.50"}, // a no-break space groups thousands; a plain space does not
		{"Tax (6%) $26.86", "26.86"}, // the 6 of "6%" is not an amount
		{"4/12/2019 6:31", ""},       // dates and times are not amounts
	}
	for _, tc := range tests {
		got, ok := parseAmount(tc.text)
		if tc.want == "" {
			if ok {
				t.Errorf("parseAmount(%q) = %v, want none", tc.text, got)
			}
			continue
		}
		if want := mustSigned(t, tc.want); !ok || got.Cmp(want) != 0 {
			t.Errorf("parseAmount(%q) = %v, want %s", tc.text, got, tc.want)
		}
	}
}

func mustSigned(t *testing.T, s string) Dec {
	t.Helper()
	if strings.HasPrefix(s, "-") {
		return mustDec(t, s[1:]).Neg()
	}
	return mustDec(t, s)
}

func TestDecimalIsExact(t *testing.T) {
	// 0.1 + 0.2 is not 0.3 in float64; here it is.
	if sum := mustDec(t, "0.1").Add(mustDec(t, "0.2")); sum.Cmp(mustDec(t, "0.3")) != 0 {
		t.Errorf("0.1 + 0.2 = %v", sum)
	}
	tests := []struct{ in, want string }{{"6000", "6000"}, {"16.00", "16.0"}, {"0.500", "0.5"}, {"7.25", "7.25"}}
	for _, tc := range tests {
		if got := mustDec(t, tc.in).Number().String(); got != tc.want {
			t.Errorf("Number(%s) = %s, want %s", tc.in, got, tc.want)
		}
	}
}

func TestDecimalBeyondInt64(t *testing.T) {
	long := "1234567890123456789012345" // 25 digits: more than int64 holds
	d, ok := parseDec(long)
	if !ok || d.String() != long {
		t.Fatalf("parseDec(%s) = %v, %t", long, d, ok)
	}
	if got, _ := parseNumber(long); got.String() != long {
		t.Errorf("parseNumber(%s) = %v", long, got)
	}
	if got, ok := parseAmount("Total " + long); !ok || got.String() != long {
		t.Errorf("parseAmount of a 25-digit number = %v, %t", got, ok)
	}
	// quantity x unit price that would wrap around in int64
	big1, big2 := mustDec(t, "99999999999.99"), mustDec(t, "99999999999.99")
	if got, want := big1.Mul(big2).String(), "9999999999998000000000.0001"; got != want {
		t.Errorf("product = %s, want %s", got, want)
	}
	if got := mustDec(t, long).Add(mustDec(t, long)).Sub(mustDec(t, long)); got.Cmp(mustDec(t, long)) != 0 {
		t.Errorf("(x + x) - x = %v, want x", got)
	}
	if got := mustDec(t, long+".50").Number().String(); got != long+".5" {
		t.Errorf("Number = %s", got)
	}
	if mustDec(t, long).Mul(mustDec(t, "0.01")).IsInteger() {
		t.Errorf("%s x 0.01 is not an integer", long)
	}
	var zero Dec
	if zero.String() != "0" || zero.Add(mustDec(t, "1.5")).String() != "1.5" {
		t.Errorf("the zero value is not 0")
	}
}

func TestParseDate(t *testing.T) {
	tests := []struct {
		text     string
		dayFirst bool
		want     string // "" when there is no date
	}{
		{"Date: 2026-08-04", true, "2026-08-04"},
		{"04/08/2026", true, "2026-08-04"}, // day first
		{"04/08/2026", false, "2026-04-08"},
		{"4.8.26", true, "2026-08-04"},
		{"12/13/2026", true, "2026-12-13"}, // 13 cannot be a month
		{"Aug 4, 2026", true, "2026-08-04"},
		{"4 August 2026", true, "2026-08-04"},
		{"31/02/2026", true, ""},
		{"Tel 0812345678", true, ""},
	}
	for _, tc := range tests {
		got, ok := parseDate(tc.text, tc.dayFirst)
		if tc.want == "" {
			if ok {
				t.Errorf("parseDate(%q) = %s, want none", tc.text, got.ISO())
			}
			continue
		}
		if !ok || got.ISO() != tc.want {
			t.Errorf("parseDate(%q, %t) = %s (%t), want %s", tc.text, tc.dayFirst, got.ISO(), ok, tc.want)
		}
	}
}

type cellSpec struct {
	text           string
	x1, y1, x2, y2 float64
}

// ocrRows builds rows from boxes, as the OCR lines of a small receipt.
func ocrRows(cells ...cellSpec) []Row {
	lines := make([]ocrLine, len(cells))
	for i, c := range cells {
		lines[i] = ocrLine{c.text, 0.95, [4]float64{c.x1, c.y1, c.x2, c.y2}}
	}
	return rowsFromOCR(lines)
}

var today = calendarDate{2026, 11, 12}

func checkResults(checks []Check) map[string]bool {
	out := map[string]bool{}
	for _, c := range checks {
		out[c.Name] = c.Passed
	}
	return out
}

func decOf(t *testing.T, d *Dec) string {
	t.Helper()
	if d == nil {
		return "nil"
	}
	return d.String()
}

func TestTwoLineItemAndSummary(t *testing.T) {
	rows := ocrRows(
		cellSpec{"Coffee beans", 10, 100, 200, 120},
		cellSpec{"2", 10, 130, 20, 150},
		cellSpec{"3,50", 100, 130, 150, 150},
		cellSpec{"7,00", 300, 130, 350, 150},
		cellSpec{"Subtotal", 10, 200, 100, 220},
		cellSpec{"7,00", 300, 200, 350, 220},
		cellSpec{"VAT 19%", 10, 230, 100, 250},
		cellSpec{"1,33", 300, 230, 350, 250},
		cellSpec{"TOTAL", 10, 260, 100, 280},
		cellSpec{"8,33", 300, 260, 350, 280},
		cellSpec{"Cash", 10, 290, 100, 310},
		cellSpec{"10,00", 300, 290, 350, 310},
	)
	receipt := extractReceipt(rows, "ppocr-v6", 400)
	if len(receipt.LineItems) != 1 {
		t.Fatalf("got %d line items, want 1", len(receipt.LineItems))
	}
	item := receipt.LineItems[0]
	got := []string{item.Description, decOf(t, item.Quantity), decOf(t, item.UnitPrice), decOf(t, item.Amount)}
	if want := []string{"Coffee beans", "2", "3.5", "7.0"}; !reflect.DeepEqual(got, want) {
		t.Errorf("line item = %v, want %v", got, want)
	}
	// cash is not the total
	got = []string{decOf(t, receipt.Subtotal), decOf(t, receipt.Tax), decOf(t, receipt.Total)}
	if want := []string{"7.0", "1.33", "8.33"}; !reflect.DeepEqual(got, want) {
		t.Errorf("summary = %v, want %v", got, want)
	}
	if checks := validate(receipt, today); !passed(checks) {
		t.Errorf("checks failed: %+v", checks)
	}
}

func TestValidationFailsWhenSumsDoNotAddUp(t *testing.T) {
	rows := ocrRows(cellSpec{"Tea 4.00", 10, 100, 200, 120}, cellSpec{"Total 5.00", 10, 200, 200, 220})
	checks := checkResults(validate(extractReceipt(rows, "ppocr-v6", 0), today))
	if checks["total_matches_parts"] {
		t.Errorf("total_matches_parts passed for items of 4.00 and a total of 5.00")
	}
}

func TestFutureDateAndLowConfidenceFail(t *testing.T) {
	rows := ocrRows(cellSpec{"Tea 4.00", 10, 100, 200, 120}, cellSpec{"Total 4.00", 10, 200, 200, 220}, cellSpec{"Date 01/01/2030", 10, 300, 200, 320})
	receipt := extractReceipt(rows, "ppocr-v6", 0)
	if checkResults(validate(receipt, today))["date_not_in_future"] {
		t.Errorf("a date in 2030 passed")
	}
	low := 0.5
	source := receipt.Sources.values["total"]
	source.Confidence = &low
	receipt.Sources.Set("total", source)
	if checkResults(validate(receipt, today))["confidence_floor"] {
		t.Errorf("a confidence of 0.5 passed")
	}
}

func TestSumValidation(t *testing.T) {
	item := func(amount string) LineItem {
		d := mustDec(t, amount)
		return LineItem{Description: "x", Amount: &d, Source: Source{}}
	}
	tests := []struct {
		name                           string
		items                          []string
		subtotal, discount, tax, total string
		want                           bool
	}{
		{"adds up", []string{"10.00", "5.50"}, "15.50", "", "1.55", "17.05", true},
		{"one cent off", []string{"10.00", "5.50"}, "15.50", "", "", "15.51", true},
		{"two cents off", []string{"10.00", "5.50"}, "15.50", "", "", "15.52", false},
		{"discount", []string{"20"}, "20", "5", "", "15", true},
		{"no subtotal uses the items", []string{"3.00", "4.00"}, "", "", "", "7.00", true},
	}
	opt := func(s string) *Dec {
		if s == "" {
			return nil
		}
		d := mustDec(t, s)
		return &d
	}
	for _, tc := range tests {
		receipt := Receipt{Subtotal: opt(tc.subtotal), Discount: opt(tc.discount), Tax: opt(tc.tax), Total: opt(tc.total)}
		for _, a := range tc.items {
			receipt.LineItems = append(receipt.LineItems, item(a))
		}
		if got := checkResults(validate(receipt, today))["total_matches_parts"]; got != tc.want {
			t.Errorf("%s: total_matches_parts = %t, want %t", tc.name, got, tc.want)
		}
	}
}

func TestHTMLTableRows(t *testing.T) {
	text := "<table><tr><th>Description</th><th>Qty</th><th>Amount</th></tr><tr><td>Tea</td><td>2</td><td>$4.00</td></tr></table>"
	rows := rowsFromBlocks([]block{{Type: "table", Order: 0, BBox: [4]float64{0, 0, 10, 10}, Text: &text}})
	var got [][]string
	for _, r := range rows {
		var cells []string
		for _, c := range r.Cells {
			cells = append(cells, c.Text)
		}
		got = append(got, cells)
	}
	if want := [][]string{{"Description", "Qty", "Amount"}, {"Tea", "2", "$4.00"}}; !reflect.DeepEqual(got, want) {
		t.Errorf("rows = %v, want %v", got, want)
	}
}

func cellRow(text string) Row { return Row{[]Cell{{Text: text}}} }

func itemsOf(t *testing.T, receipt Receipt, quantity, unit bool) []string {
	t.Helper()
	var out []string
	for _, i := range receipt.LineItems {
		parts := []string{i.Description}
		if quantity {
			parts = append(parts, decOf(t, i.Quantity))
		}
		if unit {
			parts = append(parts, decOf(t, i.UnitPrice))
		}
		out = append(out, strings.Join(append(parts, decOf(t, i.Amount)), "|"))
	}
	return out
}

func TestSummaryKindNeedsAnAmountAfterTheLabel(t *testing.T) {
	tests := []struct{ text, want string }{
		{"Tax invoice", ""},
		{"9 Rue des Tilleuls Tax invoice", ""},
		{"VAT (20%) 336,55", "tax"},
		{"Cash 50.000", "skip"},
		// the receipt vocabulary
		{"Net Total: Rp194,726", "subtotal"},
		{"NET Sales 35.000", "subtotal"},
		{"ITEMS:13 561,800", "subtotal"}, // a count of items followed by their sum
		{"Srv Ch Rp9,736", "service"},
		{"P.Rest 10% 5,592", "tax"},
		{"PResto 10/ 2,700", "tax"},
		{"Pb 1 18,900", "tax"},
		{"3 T O T A L 174.983", "total"},
		{"Total Qty 7", "total"},
		{"Prestige Coffee 4.000", ""}, // "P" and "rest" inside a longer word are not the restaurant tax
		{"3 Items,", ""},              // no amount after the label
	}
	for _, tc := range tests {
		if got := summaryKind(cellRow(tc.text)); got != tc.want {
			t.Errorf("summaryKind(%q) = %q, want %q", tc.text, got, tc.want)
		}
	}
}

func TestHeadingWithTheWordTaxDoesNotEndTheItems(t *testing.T) {
	rows := ocrRows(
		cellSpec{"9 Rue des Tilleuls", 10, 20, 200, 40},
		cellSpec{"Tax invoice", 300, 20, 400, 40},
		cellSpec{"Description", 10, 100, 100, 120},
		cellSpec{"Qty", 200, 100, 230, 120},
		cellSpec{"Amount", 300, 100, 380, 120},
		cellSpec{"Tea", 10, 130, 100, 150},
		cellSpec{"2", 200, 130, 210, 150},
		cellSpec{"2,00 €", 250, 130, 290, 150},
		cellSpec{"4,00 €", 300, 130, 380, 150},
		cellSpec{"Total", 10, 200, 100, 220},
		cellSpec{"4,00 €", 300, 200, 380, 220},
	)
	receipt := extractReceipt(rows, "ppocr-v6", 400)
	if receipt.Tax != nil {
		t.Errorf("tax = %s, want none", receipt.Tax)
	}
	if got, want := itemsOf(t, receipt, true, false), []string{"Tea|2|4.0"}; !reflect.DeepEqual(got, want) {
		t.Errorf("items = %v, want %v", got, want)
	}
	if checks := validate(receipt, today); !passed(checks) {
		t.Errorf("checks failed: %+v", checks)
	}
}

func TestCurrencyMarkApartFromItsNumber(t *testing.T) {
	tests := []struct{ text, qty, description, unit, amount string }{
		{"Consulting 5 82,37 € 411,85 €", "5", "Consulting", "82.37", "411.85"},
		{"Tea 2 $ 3.00 $ 6.00", "2", "Tea", "3.00", "6.00"},
	}
	for _, tc := range tests {
		qty, description, unit, amount := parseItemRow(cellRow(tc.text))
		got := []string{decOf(t, qty), description, decOf(t, unit), decOf(t, amount)}
		if want := []string{tc.qty, tc.description, tc.unit, tc.amount}; !reflect.DeepEqual(got, want) {
			t.Errorf("parseItemRow(%q) = %v, want %v", tc.text, got, want)
		}
	}
}

func TestTableRowWrappedAfterTheQuantity(t *testing.T) {
	rows := ocrRows(
		cellSpec{"Description", 10, 100, 100, 120},
		cellSpec{"Qty Unit price Amount", 200, 100, 380, 120},
		cellSpec{"Rental, per day 1", 10, 130, 150, 150},
		cellSpec{"20,00 € 20,00 €", 200, 160, 380, 180},
		cellSpec{"Brochure, box of 250", 10, 190, 150, 210},
		cellSpec{"3 4,00 € 12,00 €", 200, 220, 380, 240},
		cellSpec{"Total", 10, 300, 100, 320},
		cellSpec{"32,00 €", 300, 300, 380, 320},
	)
	receipt := extractReceipt(rows, "ppocr-v6", 400)
	want := []string{"Rental, per day|1|20.0|20.0", "Brochure, box of|3|4.0|12.0"}
	if got := itemsOf(t, receipt, true, true); !reflect.DeepEqual(got, want) {
		t.Errorf("items = %v, want %v", got, want)
	}
	if checks := validate(receipt, today); !passed(checks) {
		t.Errorf("checks failed: %+v", checks)
	}
}

func TestDiscountBeforeTheSubtotalIsAnItem(t *testing.T) {
	rows := ocrRows(
		cellSpec{"1 Tea", 10, 100, 100, 120},
		cellSpec{"Rp10.000", 300, 100, 380, 120},
		cellSpec{"1 Cake", 10, 130, 100, 150},
		cellSpec{"Rp20.000", 300, 130, 380, 150},
		cellSpec{"1 Discount 10%", 10, 160, 150, 180},
		cellSpec{"-Rp3.000", 300, 160, 380, 180},
		cellSpec{"Net Total:", 10, 200, 100, 220},
		cellSpec{"Rp27.000", 300, 200, 380, 220},
		cellSpec{"Srv Ch", 10, 230, 100, 250},
		cellSpec{"Rp1.350", 300, 230, 380, 250},
		cellSpec{"TOTAL:", 10, 260, 100, 280},
		cellSpec{"Rp28.350", 300, 260, 380, 280},
	)
	receipt := extractReceipt(rows, "ppocr-v6", 400)
	var amounts []string
	for _, i := range receipt.LineItems {
		amounts = append(amounts, decOf(t, i.Amount))
	}
	if want := []string{"10000", "20000", "-3000"}; !reflect.DeepEqual(amounts, want) {
		t.Errorf("amounts = %v, want %v", amounts, want)
	}
	got := []string{decOf(t, receipt.Subtotal), decOf(t, receipt.Discount), decOf(t, receipt.Service), decOf(t, receipt.Total)}
	if want := []string{"27000", "nil", "1350", "28350"}; !reflect.DeepEqual(got, want) {
		t.Errorf("summary = %v, want %v", got, want)
	}
	if checks := validate(receipt, today); !passed(checks) {
		t.Errorf("checks failed: %+v", checks)
	}
}

func TestDiscountEchoedAmongTheItemsIsNotAnItem(t *testing.T) {
	rows := ocrRows(
		cellSpec{"Cake", 10, 100, 100, 120},
		cellSpec{"63,636", 300, 100, 380, 120},
		cellSpec{"Club Card", 10, 130, 100, 150},
		cellSpec{"-9,545", 300, 130, 380, 150},
		cellSpec{"SubTotal:", 10, 200, 100, 220},
		cellSpec{"63,636", 300, 200, 380, 220},
		cellSpec{"Discount:", 10, 230, 100, 250},
		cellSpec{"-9,545", 300, 230, 380, 250},
		cellSpec{"Total:", 10, 260, 100, 280},
		cellSpec{"54,091", 300, 260, 380, 280},
	)
	receipt := extractReceipt(rows, "ppocr-v6", 400)
	var names []string
	for _, i := range receipt.LineItems {
		names = append(names, i.Description)
	}
	if want := []string{"Cake"}; !reflect.DeepEqual(names, want) {
		t.Errorf("items = %v, want %v", names, want)
	}
	if got := decOf(t, receipt.Discount); got != "9545" {
		t.Errorf("discount = %s, want 9545", got)
	}
	if checks := validate(receipt, today); !passed(checks) {
		t.Errorf("checks failed: %+v", checks)
	}
}

func TestAmountWithAStrayLeadingDot(t *testing.T) {
	tests := []struct{ text, want string }{
		{"TOTAL .20.000", "20000"},
		{"TOTAL ,20.000", "20000"},
		{"Tea, 3.50", "3.50"}, // a comma before a decimal number changes nothing
	}
	for _, tc := range tests {
		d, ok := parseAmount(tc.text)
		if !ok || d.String() != tc.want {
			t.Errorf("parseAmount(%q) = %s (%t), want %s", tc.text, d, ok, tc.want)
		}
	}
}

func TestItemsMayDifferFromTheSubtotalByTheRoundingOfWholeNumbers(t *testing.T) {
	item := func(amount string) LineItem {
		d := mustDec(t, amount)
		return LineItem{Description: "x", Amount: &d}
	}
	tests := []struct {
		name     string
		items    []string
		subtotal string
		want     bool
	}{
		{"one unit off, two items", []string{"7727", "33636"}, "41364", true},
		{"six units off", []string{"7727", "33630"}, "41364", false},
		{"decimals keep the cent", []string{"1000.50", "20"}, "1021", false},
		{"small amounts keep the cent", []string{"10"}, "11", false},
		{"exact", []string{"7727", "33637"}, "41364", true},
	}
	for _, tc := range tests {
		subtotal := mustDec(t, tc.subtotal)
		receipt := Receipt{Subtotal: &subtotal}
		for _, a := range tc.items {
			receipt.LineItems = append(receipt.LineItems, item(a))
		}
		if got := checkResults(validate(receipt, today))["items_sum_to_subtotal"]; got != tc.want {
			t.Errorf("%s: items_sum_to_subtotal = %t, want %t", tc.name, got, tc.want)
		}
	}
}
