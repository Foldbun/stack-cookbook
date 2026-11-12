package main

import (
	"math/big"
	"strings"
)

// Dec is an exact decimal number: coef / 10^scale, with an arbitrary-size integer coefficient. Money never goes through
// float64 and nothing can wrap around: sums and products are exact whatever the size of the digits. The zero value is 0.
type Dec struct {
	coef  *big.Int
	scale int
}

var (
	zero = new(big.Int)
	ten  = big.NewInt(10)
)

func (a Dec) c() *big.Int {
	if a.coef == nil {
		return zero
	}
	return a.coef
}

func pow10(n int) *big.Int { return new(big.Int).Exp(ten, big.NewInt(int64(n)), nil) }

// newDec is coef / 10^scale.
func newDec(coef int64, scale int) Dec { return Dec{big.NewInt(coef), scale} }

func intDec(n int64) Dec { return newDec(n, 0) }

// parseDec reads digits with at most one ".": "6000", "16.00". It reports false for anything else.
func parseDec(s string) (Dec, bool) {
	scale := 0
	if i := strings.IndexByte(s, '.'); i >= 0 {
		scale = len(s) - i - 1
		s = s[:i] + s[i+1:]
	}
	if s == "" || strings.Trim(s, "0123456789") != "" {
		return Dec{}, false
	}
	n, ok := new(big.Int).SetString(s, 10)
	return Dec{n, scale}, ok
}

// align brings two numbers to the same scale.
func align(a, b Dec) (x, y *big.Int, scale int) {
	x, y, scale = a.c(), b.c(), max(a.scale, b.scale)
	if a.scale < scale {
		x = new(big.Int).Mul(x, pow10(scale-a.scale))
	}
	if b.scale < scale {
		y = new(big.Int).Mul(y, pow10(scale-b.scale))
	}
	return x, y, scale
}

func (a Dec) Add(b Dec) Dec { x, y, s := align(a, b); return Dec{new(big.Int).Add(x, y), s} }
func (a Dec) Sub(b Dec) Dec { x, y, s := align(a, b); return Dec{new(big.Int).Sub(x, y), s} }
func (a Dec) Mul(b Dec) Dec { return Dec{new(big.Int).Mul(a.c(), b.c()), a.scale + b.scale} }
func (a Dec) Neg() Dec      { return Dec{new(big.Int).Neg(a.c()), a.scale} }
func (a Dec) Abs() Dec      { return Dec{new(big.Int).Abs(a.c()), a.scale} }

// Cmp returns -1, 0 or 1.
func (a Dec) Cmp(b Dec) int { x, y, _ := align(a, b); return x.Cmp(y) }

func (a Dec) IsInteger() bool {
	return new(big.Int).Rem(a.c(), pow10(a.scale)).Sign() == 0
}

// String keeps every digit of the scale: "7.00", "6000".
func (a Dec) String() string {
	digits := new(big.Int).Abs(a.c()).String()
	sign := ""
	if a.c().Sign() < 0 {
		sign = "-"
	}
	if a.scale == 0 {
		return sign + digits
	}
	if len(digits) <= a.scale {
		digits = strings.Repeat("0", a.scale-len(digits)+1) + digits
	}
	return sign + digits[:len(digits)-a.scale] + "." + digits[len(digits)-a.scale:]
}

// Number is the form a receipt prints a value in JSON: an integer stays an integer ("6000" for "6.000"), a fraction loses
// its trailing zeros but keeps one digit ("16.0" for "16.00", "0.5" for "0.500").
func (a Dec) Number() Dec {
	coef, rem := new(big.Int).Set(a.c()), new(big.Int)
	for a.scale > 1 {
		q, m := new(big.Int).QuoRem(coef, ten, rem)
		if m.Sign() != 0 {
			break
		}
		coef, a.scale = q, a.scale-1
	}
	return Dec{coef, a.scale}
}

func (a Dec) MarshalJSON() ([]byte, error) { return []byte(a.String()), nil }
