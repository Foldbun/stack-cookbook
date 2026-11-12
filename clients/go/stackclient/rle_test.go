package stackclient

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// testdata/rle_vectors.json is written by testdata/gen_rle_vectors.py, an independent implementation of the format.
type vector struct {
	Name   string   `json:"name"`
	Height int      `json:"height"`
	Width  int      `json:"width"`
	Rows   []string `json:"rows"`
	Counts string   `json:"counts"`
}

func fromRows(rows []string) *Bitmap {
	b := NewBitmap(len(rows), len(rows[0]))
	for r, row := range rows {
		for c, ch := range row {
			if ch == '1' {
				b.Pix[r*b.Width+c] = 1
			}
		}
	}
	return b
}

func toRows(b *Bitmap) []string {
	rows := make([]string, b.Height)
	for r := range rows {
		var sb strings.Builder
		for c := 0; c < b.Width; c++ {
			if b.At(r, c) {
				sb.WriteByte('1')
			} else {
				sb.WriteByte('0')
			}
		}
		rows[r] = sb.String()
	}
	return rows
}

func TestRLEVectors(t *testing.T) {
	raw, err := os.ReadFile("testdata/rle_vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var vectors []vector
	if err := json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	if len(vectors) < 10 {
		t.Fatalf("only %d vectors", len(vectors))
	}
	for _, v := range vectors {
		t.Run(v.Name, func(t *testing.T) {
			got, err := DecodeCOCORLE(v.Height, v.Width, v.Counts)
			if err != nil {
				t.Fatal(err)
			}
			for r, row := range toRows(got) {
				if row != v.Rows[r] {
					t.Fatalf("row %d: got %s, want %s", r, row, v.Rows[r])
				}
			}
			if enc := EncodeCOCORLE(fromRows(v.Rows)); enc != v.Counts {
				t.Errorf("encoded %q, want %q", enc, v.Counts)
			}
		})
	}
}

// Worked by hand from the format: runs alternate background and object, starting with background; a number is
// 5-bit groups, least significant first, with 0x20 = more groups and 0x10 of the last group = negative; + 48.
func TestRLEByHand(t *testing.T) {
	for _, c := range []struct {
		counts string
		runs   []int
	}{
		{"6", []int{6}},
		{"01", []int{0, 1}},
		{"121O", []int{1, 2, 1, 1}},
		{"b1", []int{50}},
		{"3", []int{3}},
		{"P1", []int{32}}, // 32 = 0b100000: group 0 with "more" -> 48+32 = 'P', then group 1 -> '1'
	} {
		runs, err := RunsFromString(c.counts)
		if err != nil {
			t.Fatalf("%q: %v", c.counts, err)
		}
		if len(runs) != len(c.runs) {
			t.Fatalf("%q decoded to %v, want %v", c.counts, runs, c.runs)
		}
		for i := range runs {
			if runs[i] != c.runs[i] {
				t.Fatalf("%q decoded to %v, want %v", c.counts, runs, c.runs)
			}
		}
		if got := RunsToString(c.runs); got != c.counts {
			t.Errorf("%v encoded to %q, want %q", c.runs, got, c.counts)
		}
	}
}

func TestRLEErrors(t *testing.T) {
	for name, c := range map[string]struct {
		h, w   int
		counts string
	}{
		"runs too short":       {2, 3, "5"},
		"runs too long":        {2, 3, "7"},
		"ends inside a number": {2, 3, "P"},
		"invalid character":    {2, 3, "~"},
		"space":                {2, 3, " "},
	} {
		if _, err := DecodeCOCORLE(c.h, c.w, c.counts); err == nil {
			t.Errorf("%s: no error", name)
		}
	}
}
