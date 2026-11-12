// Validation of an extracted receipt: the checks that decide whether a result can be trusted without a person.
package main

import (
	"fmt"
	"strings"
)

var tolerance = newDec(1, 2) // sums must agree to one cent (to one unit for currencies without decimals)

const confidenceFloor = 0.80 // minimum OCR confidence of every line a field was read from

// Check is one validation rule and whether the receipt passed it.
type Check struct {
	Name   string `json:"name"`
	Passed bool   `json:"passed"`
	Detail string `json:"detail"`
}

func orZero(d *Dec) Dec {
	if d == nil {
		return intDec(0)
	}
	return *d
}

func either(cond bool, yes, no string) string {
	if cond {
		return yes
	}
	return no
}

func listOf[T any](items []T, format func(T) string) string {
	parts := make([]string, len(items))
	for i, it := range items {
		parts[i] = format(it)
	}
	return "[" + strings.Join(parts, ", ") + "]"
}

// region validate

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

// endregion
