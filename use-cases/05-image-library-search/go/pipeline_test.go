package main

// Tests of the pure logic: normalisation, top-k ordering, near-duplicate grouping, and the index on disk. They need no
// API key and no network: run them with `go test ./...` in this directory.

import (
	"encoding/binary"
	"encoding/json"
	"math"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func matrixOf(rows ...[]float32) Matrix {
	m := Matrix{N: len(rows)}
	if len(rows) > 0 {
		m.Dim = len(rows[0])
	}
	for _, r := range rows {
		m.Data = append(m.Data, Normalize(r)...)
	}
	return m
}

func rowsOf(hits []Hit) []int {
	out := []int{}
	for _, h := range hits {
		out = append(out, h.Row)
	}
	return out
}

func TestNormalize(t *testing.T) {
	for _, c := range []struct {
		name string
		in   []float32
		want []float32
	}{
		{"3-4-5", []float32{3, 4}, []float32{0.6, 0.8}},
		{"already unit", []float32{0, 1, 0}, []float32{0, 1, 0}},
		{"zero vector stays zero", []float32{0, 0, 0}, []float32{0, 0, 0}},
		{"negative values", []float32{-2, 0}, []float32{-1, 0}},
	} {
		t.Run(c.name, func(t *testing.T) {
			got := Normalize(c.in)
			for i := range c.want {
				if math.Abs(float64(got[i]-c.want[i])) > 1e-6 {
					t.Fatalf("Normalize(%v) = %v, want %v", c.in, got, c.want)
				}
			}
		})
	}
}

func TestTopK(t *testing.T) {
	// rows 0 and 2 are identical, row 3 is the opposite of row 1
	m := matrixOf([]float32{1, 0}, []float32{0, 1}, []float32{1, 0}, []float32{0, -1}, []float32{1, 1})
	for _, c := range []struct {
		name    string
		query   []float32
		k       int
		exclude int
		want    []int
	}{
		{"best first, equal scores by row", []float32{1, 0}, 5, -1, []int{0, 2, 4, 1, 3}},
		{"k cuts the list", []float32{1, 0}, 2, -1, []int{0, 2}},
		{"exclude skips the query row", []float32{1, 0}, 2, 0, []int{2, 4}},
		{"k larger than the library", []float32{0, 1}, 99, 1, []int{4, 0, 2, 3}},
		{"k zero", []float32{1, 0}, 0, -1, []int{}},
		{"negative k", []float32{1, 0}, -3, -1, []int{}},
	} {
		t.Run(c.name, func(t *testing.T) {
			if got := rowsOf(TopK(m, c.query, c.k, c.exclude)); !reflect.DeepEqual(got, c.want) {
				t.Fatalf("got %v, want %v", got, c.want)
			}
		})
	}
	if got := TopK(Matrix{}, nil, 3, -1); len(got) != 0 {
		t.Fatalf("an empty library gives %v", got)
	}
	hits := TopK(m, []float32{1, 0}, 5, -1)
	for i := 1; i < len(hits); i++ {
		if hits[i-1].Score < hits[i].Score {
			t.Fatalf("scores are not in descending order: %v", hits)
		}
	}
}

// chainMatrix is six random vectors where row 4 is a near-copy of row 1 and row 5 a near-copy of row 4: a chain.
func chainMatrix() Matrix {
	rng := rand.New(rand.NewSource(1))
	rows := make([][]float32, 6)
	for i := range rows {
		rows[i] = make([]float32, 32)
		for j := range rows[i] {
			rows[i][j] = float32(rng.NormFloat64())
		}
	}
	for j := range rows[4] {
		rows[4][j] = rows[1][j] + 0.01*float32(rng.NormFloat64())
	}
	for j := range rows[5] {
		rows[5][j] = rows[4][j] + 0.01*float32(rng.NormFloat64())
	}
	return matrixOf(rows...)
}

func TestTopKFindsTheCopies(t *testing.T) {
	m := chainMatrix()
	if got := rowsOf(TopK(m, m.Row(1), 2, 1)); !reflect.DeepEqual(got, []int{4, 5}) {
		t.Fatalf("neighbours of row 1: %v", got)
	}
	if got := len(TopK(m, m.Row(1), 99, 1)); got != 5 {
		t.Fatalf("k beyond the library gives %d rows, want 5", got)
	}
}

func TestDuplicateGroups(t *testing.T) {
	m := chainMatrix()
	for _, c := range []struct {
		name      string
		threshold float64
		want      [][]int
	}{
		{"chain ends up in one group", 0.95, [][]int{{1, 4, 5}}},
		{"nothing is that similar", 0.9999999, [][]int{}},
		{"threshold 0.99 still joins the copies", 0.99, [][]int{{1, 4, 5}}},
	} {
		t.Run(c.name, func(t *testing.T) {
			if got := DuplicateGroups(m, c.threshold); !reflect.DeepEqual(got, c.want) {
				t.Fatalf("got %v, want %v", got, c.want)
			}
		})
	}
}

func TestDuplicateGroupsThreshold(t *testing.T) {
	// cosine to row 0: rows 1 and 3 are at 0.97 and 1, rows 2 and 4 at 0.5 (rows 2 and 4 are copies of each other)
	at := func(c float64) []float32 { return []float32{float32(c), float32(math.Sqrt(1 - c*c))} }
	m := matrixOf([]float32{1, 0}, at(0.97), at(0.5), []float32{1, 0}, at(0.5))
	for _, c := range []struct {
		name      string
		threshold float64
		want      [][]int
	}{
		{"exact copies only", 0.99, [][]int{{0, 3}, {2, 4}}},
		{"the threshold is inclusive", float64(dot(m.Row(0), m.Row(1))), [][]int{{0, 1, 3}, {2, 4}}},
		{"a low threshold joins the rest", 0.3, [][]int{{0, 1, 2, 3, 4}}},
		{"groups are ordered by their first row", 0.9999, [][]int{{0, 3}, {2, 4}}},
	} {
		t.Run(c.name, func(t *testing.T) {
			if got := DuplicateGroups(m, c.threshold); !reflect.DeepEqual(got, c.want) {
				t.Fatalf("got %v, want %v", got, c.want)
			}
		})
	}
}

func TestNewIndexFileIsRowMajorFloat32(t *testing.T) {
	dir := t.TempDir()
	index := NewIndex(dir)
	index.SetItems([]Item{{ID: "a", Path: "a.png", Sha256: "sa"}, {ID: "b", Path: "b.png", Sha256: "sb"}})
	if err := index.Add("m", []string{"sa", "sb"}, [][]float32{{3, 4, 0}, {0, 0, 2}}); err != nil {
		t.Fatal(err)
	}
	if err := index.Commit(); err != nil {
		t.Fatal(err)
	}
	var meta struct {
		Format int
		Models map[string]struct {
			Dimensions int
			File       string
			Sha256s    []string
		}
	}
	raw, _ := os.ReadFile(filepath.Join(dir, "index.json"))
	if err := json.Unmarshal(raw, &meta); err != nil {
		t.Fatal(err)
	}
	info := meta.Models["m"]
	data, _ := os.ReadFile(filepath.Join(dir, info.File))
	if meta.Format != 1 || info.Dimensions != 3 || len(data) != 4*2*3 || !reflect.DeepEqual(info.Sha256s, []string{"sa", "sb"}) {
		t.Fatalf("unexpected index: %+v, %d bytes", meta, len(data))
	}
	want := []float32{0.6, 0.8, 0, 0, 0, 1}
	for i, w := range want {
		got := math.Float32frombits(binary.LittleEndian.Uint32(data[4*i:]))
		if math.Abs(float64(got-w)) > 1e-6 {
			t.Fatalf("value %d is %v, want %v", i, got, w)
		}
	}
}

func TestIndexRoundTripAndIncrementalUpdate(t *testing.T) {
	dir := t.TempDir()
	lib := []Item{{ID: "a", Path: "a.png", Sha256: "s1"}, {ID: "b", Path: "b.png", Sha256: "s2"}, {ID: "c", Path: "c.png", Sha256: "s3"}}
	index := NewIndex(dir)
	index.SetItems(lib)
	if err := index.Commit(); err != nil {
		t.Fatal(err)
	}
	if got := index.Missing("m"); !reflect.DeepEqual(got, []string{"s1", "s2", "s3"}) {
		t.Fatalf("missing at the start: %v", got)
	}
	if err := index.Add("m", []string{"s2", "s1"}, [][]float32{{0, 2}, {2, 0}}); err != nil {
		t.Fatal(err)
	}
	if err := index.Commit(); err != nil {
		t.Fatal(err)
	}

	loaded, err := LoadIndex(dir)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(loaded.Items, lib) || loaded.Dimensions("m") != 2 || !loaded.Has("m", "s1") || loaded.Has("m", "s3") {
		t.Fatalf("round trip lost something: %+v", loaded.Items)
	}
	if got := loaded.Missing("m"); !reflect.DeepEqual(got, []string{"s3"}) {
		t.Fatalf("missing after the round trip: %v", got)
	}
	items, m := loaded.Matrix("m") // catalogue order, not insertion order
	if len(items) != 2 || items[0].ID != "a" || items[1].ID != "b" || !reflect.DeepEqual(m.Row(0), []float32{1, 0}) || !reflect.DeepEqual(m.Row(1), []float32{0, 1}) {
		t.Fatalf("matrix: %v %+v", items, m)
	}

	// a copy shares its twin's row; a removed file's row is dropped; the generation moves on and the old file goes
	loaded.SetItems([]Item{{ID: "a", Path: "a.png", Sha256: "s1"}, {ID: "a-copy", Path: "copy.png", Sha256: "s1"}, {ID: "c", Path: "c.png", Sha256: "s3"}})
	if err := loaded.Commit(); err != nil {
		t.Fatal(err)
	}
	again, _ := LoadIndex(dir)
	if again.Has("m", "s2") || !again.Has("m", "s1") || again.Generation != 3 {
		t.Fatalf("after the update: generation %d, models %+v", again.Generation, again.Models["m"])
	}
	items, _ = again.Matrix("m")
	if len(items) != 2 || items[1].ID != "a-copy" {
		t.Fatalf("a copy must have the vector of its twin: %v", items)
	}
	files, _ := filepath.Glob(filepath.Join(dir, "vectors-*"))
	if len(files) != 1 {
		t.Fatalf("stale vector files were not removed: %v", files)
	}
	if _, err := os.Stat(filepath.Join(dir, "index.json.tmp")); err == nil {
		t.Fatal("temporary file left behind")
	}
}

func TestIndexRefusals(t *testing.T) {
	dir := t.TempDir()
	index := NewIndex(dir)
	index.SetItems([]Item{{ID: "a", Sha256: "s1"}, {ID: "b", Sha256: "s2"}})
	if err := index.Add("m", []string{"s1"}, [][]float32{{1, 2}}); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		name    string
		sha     []string
		vectors [][]float32
		want    string
	}{
		{"another vector length", []string{"s2"}, [][]float32{{1, 2, 3}}, "the model changed"},
		{"ragged batch", []string{"s2", "s3"}, [][]float32{{1, 2}, {1}}, "different lengths"},
		{"count mismatch", []string{"s2"}, [][]float32{{1, 2}, {3, 4}}, "vectors for"},
	} {
		t.Run(c.name, func(t *testing.T) {
			err := index.Add("m", c.sha, c.vectors)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("got %v, want an error containing %q", err, c.want)
			}
		})
	}
	if err := os.WriteFile(filepath.Join(dir, "index.json"), []byte(`{"format": 99}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadIndex(dir); err == nil || !strings.Contains(err.Error(), "unknown index format") {
		t.Fatalf("an unknown format must be refused, got %v", err)
	}
}

func TestBatchKeyIsStable(t *testing.T) {
	a, b := batchKey("dinov3", []string{"h1", "h2"}, 0), batchKey("dinov3", []string{"h1", "h2"}, 0)
	if a != b || len(a) != 40 {
		t.Fatalf("keys %q and %q", a, b)
	}
	for _, other := range []string{batchKey("siglip2", []string{"h1", "h2"}, 0), batchKey("dinov3", []string{"h1", "h2"}, 1), batchKey("dinov3", []string{"h2", "h1"}, 0)} {
		if other == a {
			t.Fatal("a different batch must have a different key")
		}
	}
}
