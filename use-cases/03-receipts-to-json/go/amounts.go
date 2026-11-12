// Parsing the strings that receipts print: amounts in both number formats, currency marks and dates.
package main

import (
	"fmt"
	"strings"
	"unicode"
)

// region amount-regex

// Go's regexp package has no lookbehind or lookahead, so the amount pattern is matched by hand. The code below tries the
// same alternatives in the same order as the pattern in the Python version, so both read the same token from the same text:
//
//	(?<![\w/:.,])                                     not preceded by a letter, digit, "_", "/", ":", "." or ","
//	(?:[.,](?=\d{1,3}[.,]\d{3}(?!\d)))?               a stray "." or "," just before a grouped number (".20.000") is skipped
//	(?P<pre>[-−(]?)\s*                                an optional sign or "(" before the number
//	(?P<cur>Rp\.?|IDR|USD|EUR|GBP|AUD|CAD|[$€£¥])?\s* an optional currency mark
//	(?P<sign>[-−]?)
//	(?P<num>\d{1,3}([\u00a0\u202f]\d{3})+([.,]\d{1,2})?|\d[\d.,]*\d|\d)
//	(?P<post>-|\)|\s?CR)?                             a trailing minus, ")" or "CR"
//	(?![\w%/:])                                       not followed by a letter, digit, "_", "%", "/" or ":"
//
// The pattern is case-insensitive.

type amountMatch struct {
	pre, cur, sign, num, post string
}

var currencyMarks = []string{"Rp.", "Rp", "IDR", "USD", "EUR", "GBP", "AUD", "CAD", "$", "€", "£", "¥"}

func isWord(r rune) bool  { return r == '_' || unicode.IsLetter(r) || unicode.IsNumber(r) }
func isDigit(r rune) bool { return r >= '0' && r <= '9' }

func at(rs []rune, i int) rune {
	if i < 0 || i >= len(rs) {
		return -1
	}
	return rs[i]
}

// spaceLens lists the lengths a greedy "\s*" can take, longest first.
func spaceLens(rs []rune, p int) []int {
	n := 0
	for p+n < len(rs) && unicode.IsSpace(rs[p+n]) {
		n++
	}
	out := make([]int, 0, n+1)
	for k := n; k >= 0; k-- {
		out = append(out, k)
	}
	return out
}

// hasFold reports whether the runes at p spell word, ignoring case.
func hasFold(rs []rune, p int, word string) bool {
	w := []rune(word)
	return p+len(w) <= len(rs) && strings.EqualFold(string(rs[p:p+len(w)]), word)
}

// digitLens lists the lengths hi..lo that fit the digits available at p, longest first.
func digitLens(rs []rune, p, lo, hi int) []int {
	avail := 0
	for avail < hi && isDigit(at(rs, p+avail)) {
		avail++
	}
	var out []int
	for k := avail; k >= lo; k-- {
		out = append(out, k)
	}
	return out
}

// numberEnds lists the ends of the number alternatives at p in the order the pattern tries them.
func numberEnds(rs []rune, p int) []int {
	var ends []int
	// \d{1,3}([\u00a0\u202f]\d{3})+([.,]\d{1,2})?
	for _, k := range digitLens(rs, p, 1, 3) {
		q, groups := p+k, 0
		for (at(rs, q) == '\u00a0' || at(rs, q) == '\u202f') && len(digitLens(rs, q+1, 3, 3)) == 1 {
			q, groups = q+4, groups+1
		}
		for g := groups; g >= 1; g-- {
			e := p + k + 4*g
			if m := at(rs, e); m == '.' || m == ',' {
				for _, d := range digitLens(rs, e+1, 1, 2) {
					ends = append(ends, e+1+d)
				}
			}
			ends = append(ends, e)
		}
	}
	// \d[\d.,]*\d
	if isDigit(at(rs, p)) {
		run := 0
		for c := at(rs, p+1+run); isDigit(c) || c == '.' || c == ','; c = at(rs, p+1+run) {
			run++
		}
		for j := run; j >= 0; j-- {
			if isDigit(at(rs, p+1+j)) {
				ends = append(ends, p+2+j)
			}
		}
		// \d
		ends = append(ends, p+1)
	}
	return ends
}

// matchAmount tries to match one token starting exactly at i; it returns the groups and the end.
func matchAmount(rs []rune, i int) (amountMatch, int, bool) {
	if i > 0 && (isWord(rs[i-1]) || strings.ContainsRune("/:.,", rs[i-1])) {
		return amountMatch{}, 0, false
	}
	// The optional stray mark is greedy: it is tried first, and only when a grouped number follows it.
	strays := []int{0}
	if c := at(rs, i); (c == '.' || c == ',') && strayBeforeGroup(rs, i+1) {
		strays = []int{1, 0}
	}
	for _, stray := range strays {
		if m, end, ok := matchAmountAt(rs, i, i+stray); ok {
			return m, end, true
		}
	}
	return amountMatch{}, 0, false
}

// strayBeforeGroup is the lookahead \d{1,3}[.,]\d{3}(?!\d) at p.
func strayBeforeGroup(rs []rune, p int) bool {
	for _, k := range digitLens(rs, p, 1, 3) {
		if m := at(rs, p+k); (m == '.' || m == ',') && len(digitLens(rs, p+k+1, 3, 3)) == 1 && !isDigit(at(rs, p+k+4)) {
			return true
		}
	}
	return false
}

// matchAmountAt matches the rest of the pattern, after the optional stray mark, starting at j; the token began at i.
func matchAmountAt(rs []rune, i, j int) (amountMatch, int, bool) {
	pres := []int{0}
	if c := at(rs, j); c == '-' || c == '−' || c == '(' {
		pres = []int{1, 0}
	}
	for _, pl := range pres {
		p1 := j + pl
		for _, w1 := range spaceLens(rs, p1) {
			p2 := p1 + w1
			curs := []int{}
			for _, mark := range currencyMarks {
				if hasFold(rs, p2, mark) {
					curs = append(curs, len([]rune(mark)))
				}
			}
			curs = append(curs, 0)
			for _, cl := range curs {
				p3 := p2 + cl
				for _, w2 := range spaceLens(rs, p3) {
					p4 := p3 + w2
					signs := []int{0}
					if c := at(rs, p4); c == '-' || c == '−' {
						signs = []int{1, 0}
					}
					for _, sl := range signs {
						p5 := p4 + sl
						for _, e := range numberEnds(rs, p5) {
							posts := []int{}
							if at(rs, e) == '-' || at(rs, e) == ')' {
								posts = append(posts, 1)
							}
							if unicode.IsSpace(at(rs, e)) && hasFold(rs, e+1, "CR") {
								posts = append(posts, 3)
							}
							if hasFold(rs, e, "CR") {
								posts = append(posts, 2)
							}
							posts = append(posts, 0)
							for _, pol := range posts {
								end := e + pol
								if c := at(rs, end); c != -1 && (isWord(c) || strings.ContainsRune("%/:", c)) {
									continue
								}
								return amountMatch{string(rs[j:p1]), string(rs[p2:p3]), string(rs[p4:p5]), string(rs[p5:e]), string(rs[e:end])}, end, true
							}
						}
					}
				}
			}
		}
	}
	return amountMatch{}, 0, false
}

// endregion

// findAmountTokens returns every match of the amount pattern, left to right, without overlap.
func findAmountTokens(text string) []amountMatch {
	rs := []rune(text)
	var out []amountMatch
	for i := 0; i < len(rs); {
		if m, end, ok := matchAmount(rs, i); ok {
			out = append(out, m)
			i = end
		} else {
			i++
		}
	}
	return out
}

var currency = map[string]string{"rp": "IDR", "rp.": "IDR", "idr": "IDR", "usd": "USD", "eur": "EUR", "gbp": "GBP", "aud": "AUD", "cad": "CAD", "€": "EUR", "£": "GBP", "¥": "JPY"}

// region parse-amount

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

// endregion

// currencyOf returns (ISO code, printed mark) of the first currency mark in the text; "$" alone is ambiguous, so it gives no code.
func currencyOf(text string) (code, mark string) {
	for _, m := range findAmountTokens(text) {
		if m.cur != "" {
			return currency[strings.ToLower(m.cur)], m.cur
		}
	}
	return "", ""
}

var months = map[string]int{"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

// calendarDate is a year, month and day that exist.
type calendarDate struct{ Year, Month, Day int }

func (d calendarDate) ISO() string { return fmt.Sprintf("%04d-%02d-%02d", d.Year, d.Month, d.Day) }

func newDate(y, m, d int) (calendarDate, bool) {
	if y < 1 || y > 9999 || m < 1 || m > 12 || d < 1 {
		return calendarDate{}, false
	}
	days := []int{31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31}[m-1]
	if m == 2 && (y%4 == 0 && (y%100 != 0 || y%400 == 0)) {
		days = 29
	}
	return calendarDate{y, m, d}, d <= days
}

func atoi(rs []rune) int {
	n := 0
	for _, r := range rs {
		n = n*10 + int(r-'0')
	}
	return n
}

func isSep(r rune, set string) bool { return r != -1 && strings.ContainsRune(set, r) }

// dateMatch is the three groups of one date pattern and where the match ends.
type dateMatch struct {
	a, b, c []rune
	end     int
}

// The four patterns, matched by hand for the same reason as the amounts. Each returns the first match that starts at i.

// ymd: (?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)
func matchYMD(rs []rune, i int) (dateMatch, bool) {
	if isDigit(at(rs, i-1)) || len(digitLens(rs, i, 4, 4)) == 0 || !isSep(at(rs, i+4), "-/.") {
		return dateMatch{}, false
	}
	for _, l2 := range digitLens(rs, i+5, 1, 2) {
		p := i + 5 + l2
		if !isSep(at(rs, p), "-/.") {
			continue
		}
		for _, l3 := range digitLens(rs, p+1, 1, 2) {
			if end := p + 1 + l3; !isDigit(at(rs, end)) {
				return dateMatch{rs[i : i+4], rs[i+5 : i+5+l2], rs[p+1 : end], end}, true
			}
		}
	}
	return dateMatch{}, false
}

// dmy: (?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)
func matchDMY(rs []rune, i int) (dateMatch, bool) {
	if isDigit(at(rs, i-1)) {
		return dateMatch{}, false
	}
	for _, l1 := range digitLens(rs, i, 1, 2) {
		p := i + l1
		if !isSep(at(rs, p), "-/.") {
			continue
		}
		for _, l2 := range digitLens(rs, p+1, 1, 2) {
			q := p + 1 + l2
			if !isSep(at(rs, q), "-/.") {
				continue
			}
			for _, l3 := range []int{4, 2} {
				if len(digitLens(rs, q+1, l3, l3)) == 1 && !isDigit(at(rs, q+1+l3)) {
					return dateMatch{rs[i:p], rs[p+1 : q], rs[q+1 : q+1+l3], q + 1 + l3}, true
				}
			}
		}
	}
	return dateMatch{}, false
}

func isLetterASCII(r rune) bool { return (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') }

// monthWord matches [A-Za-z]{3}[a-z]*\.? at p and returns the three letters and the end.
func monthWord(rs []rune, p int) ([]rune, int, bool) {
	if !isLetterASCII(at(rs, p)) || !isLetterASCII(at(rs, p+1)) || !isLetterASCII(at(rs, p+2)) {
		return nil, 0, false
	}
	q := p + 3
	for c := at(rs, q); c >= 'a' && c <= 'z'; c = at(rs, q) {
		q++
	}
	if at(rs, q) == '.' {
		q++
	}
	return rs[p : p+3], q, true
}

// d-mon-y: (?<!\w)(\d{1,2})[ -]([A-Za-z]{3})[a-z]*\.?,?[ -](\d{4})(?!\d)
func matchDMonY(rs []rune, i int) (dateMatch, bool) {
	if isWord(at(rs, i-1)) {
		return dateMatch{}, false
	}
	for _, l1 := range digitLens(rs, i, 1, 2) {
		p := i + l1
		if !isSep(at(rs, p), " -") {
			continue
		}
		mon, q, ok := monthWord(rs, p+1)
		if !ok {
			continue
		}
		if at(rs, q) == ',' {
			q++
		}
		if !isSep(at(rs, q), " -") || len(digitLens(rs, q+1, 4, 4)) == 0 || isDigit(at(rs, q+5)) {
			continue
		}
		return dateMatch{rs[i:p], mon, rs[q+1 : q+5], q + 5}, true
	}
	return dateMatch{}, false
}

// mon-d-y: (?<!\w)([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})(?!\d)
func matchMonDY(rs []rune, i int) (dateMatch, bool) {
	if isWord(at(rs, i-1)) {
		return dateMatch{}, false
	}
	mon, q, ok := monthWord(rs, i)
	if !ok || at(rs, q) != ' ' {
		return dateMatch{}, false
	}
	for _, l2 := range digitLens(rs, q+1, 1, 2) {
		p := q + 1 + l2
		if at(rs, p) == ',' {
			p++
		}
		if at(rs, p) == ' ' && len(digitLens(rs, p+1, 4, 4)) == 1 && !isDigit(at(rs, p+5)) {
			return dateMatch{mon, rs[q+1 : q+1+l2], rs[p+1 : p+5], p + 5}, true
		}
	}
	return dateMatch{}, false
}

// parseDate returns the first calendar date in the text: ISO, 04/08/2026, 4.8.26, 4 Aug 2026 or Aug 4, 2026.
//
// Numeric day/month/year is read day first unless the second number cannot be a month; dayFirst=false flips the default.
func parseDate(text string, dayFirst bool) (calendarDate, bool) {
	rs := []rune(text)
	patterns := []struct {
		match func([]rune, int) (dateMatch, bool)
		kind  string
	}{{matchYMD, "ymd"}, {matchDMY, "dmy"}, {matchDMonY, "d-mon-y"}, {matchMonDY, "mon-d-y"}}
	for _, pat := range patterns {
		for i := 0; i < len(rs); {
			m, ok := pat.match(rs, i)
			if !ok {
				i++
				continue
			}
			i = m.end
			var day calendarDate
			var valid bool
			switch pat.kind {
			case "ymd":
				day, valid = newDate(atoi(m.a), atoi(m.b), atoi(m.c))
			case "dmy":
				first, second, year := atoi(m.a), atoi(m.b), atoi(m.c)
				if len(m.c) == 2 {
					year += 2000
				}
				if first > 12 || (dayFirst && second <= 12) {
					day, valid = newDate(year, second, first)
				} else {
					day, valid = newDate(year, first, second)
				}
			case "d-mon-y":
				if mo, known := months[strings.ToLower(string(m.b))]; known {
					day, valid = newDate(atoi(m.c), mo, atoi(m.a))
				}
			default:
				if mo, known := months[strings.ToLower(string(m.a))]; known {
					day, valid = newDate(atoi(m.c), mo, atoi(m.b))
				}
			}
			if valid {
				return day, true
			}
		}
	}
	return calendarDate{}, false
}
