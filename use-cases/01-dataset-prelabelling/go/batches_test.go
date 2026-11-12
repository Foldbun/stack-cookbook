package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"stack-cookbook/01-dataset-prelabelling/stackclient"
)

func TestBatchKey(t *testing.T) {
	body := map[string]any{"model": "sam3", "items": []any{map[string]any{"image": map[string]any{"id": "img_1"}}}}
	same := map[string]any{"items": []any{map[string]any{"image": map[string]any{"id": "img_1"}}}, "model": "sam3"} // key order does not matter
	other := map[string]any{"model": "sam3", "items": []any{map[string]any{"image": map[string]any{"id": "img_2"}}}}
	key := batchKey(1, 0, body)
	if !strings.HasPrefix(key, "prelabel-r1-0000-") || len(key) != len("prelabel-r1-0000-")+32 {
		t.Errorf("key = %q", key)
	}
	for _, tc := range []struct {
		name  string
		other string
		equal bool
	}{
		{"same body, other key order", batchKey(1, 0, same), true},
		{"other round", batchKey(2, 0, body), false},
		{"other index", batchKey(1, 1, body), false},
		{"other body", batchKey(1, 0, other), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if (tc.other == key) != tc.equal {
				t.Errorf("%q vs %q: equal = %v, want %v", key, tc.other, tc.other == key, tc.equal)
			}
		})
	}
}

func TestRetryable(t *testing.T) {
	for _, tc := range []struct {
		o    outcome
		want bool
	}{
		{outcome{Status: "ok"}, false},
		{outcome{Status: "expired"}, true},
		{outcome{Status: "no_result"}, true},
		{outcome{Status: "error", Err: &itemError{Code: "model_busy"}}, true},
		{outcome{Status: "error", Err: &itemError{Code: "bad_model_output"}}, false},
	} {
		if got := retryable(&tc.o); got != tc.want {
			t.Errorf("retryable(%s/%v) = %v, want %v", tc.o.Status, tc.o.Err, got, tc.want)
		}
	}
}

func unit(id, digest string) *Unit { return &Unit{Item: manifestItem{ID: id}, Digest: digest} }

func TestCheckpointRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "out", "checkpoint.jsonl")
	cache := "miss"
	done := &outcome{Status: "ok", Result: &segResult{Instances: []instance{{Score: 0.9, BBox: [4]int{1, 2, 3, 4}, Area: 5}}},
		Calls: []stackclient.Row{{Endpoint: "/v1/batch/segment", List: 0.7, Charged: 0.7, Cache: &cache, ProcessingMs: 9}}}

	c, err := openCheckpoint(path, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, u := range []*Unit{unit("a", "d1"), unit("b", "d2")} {
		if err := c.save(u, done); err != nil {
			t.Fatal(err)
		}
	}
	c.Close()

	f, _ := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0) // a crash left half a line at the end
	f.WriteString(`{"id":"c","dig`)
	f.Close()

	c, err = openCheckpoint(path, true)
	if err != nil {
		t.Fatal(err)
	}
	got := c.lookup(unit("a", "d1"))
	if got == nil || got.Status != "ok" || got.Result.Instances[0].BBox != [4]int{1, 2, 3, 4} {
		t.Fatalf("lookup(a) = %+v", got)
	}
	if row := got.Calls[0]; !row.Replayed || row.Charged != 0 || row.List != 0.7 || *row.Cache != "miss" {
		t.Errorf("a resumed row keeps its list price and is not charged again, got %+v", row)
	}
	if c.lookup(unit("a", "changed")) != nil {
		t.Error("an item whose request changed must be asked again")
	}
	if c.lookup(unit("c", "x")) != nil {
		t.Error("the cut line must be ignored")
	}
	if err := c.save(unit("c", "x"), done); err != nil { // appending after the cut line must not corrupt the new record
		t.Fatal(err)
	}
	c.Close()
	c, _ = openCheckpoint(path, true)
	if c.lookup(unit("c", "x")) == nil || len(c.done) != 3 {
		t.Errorf("after appending, %d items are readable, want 3", len(c.done))
	}
	c.Close()
	c, _ = openCheckpoint(path, false) // without --resume the file starts empty
	defer c.Close()
	if data, _ := os.ReadFile(path); len(data) != 0 {
		t.Errorf("a fresh run keeps %d bytes of the old checkpoint", len(data))
	}
}

// A cancelled context starts nothing. No client is needed because no request is made.
func TestCancelledRunSendsNothing(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	units := []*Unit{unit("a", ""), unit("b", "")}
	cfg := &config{Concurrency: 2, BatchSize: 1, Rounds: 3}
	ckpt, err := openCheckpoint(filepath.Join(t.TempDir(), "c.jsonl"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer ckpt.Close()
	if got, err := runRound(ctx, nil, cfg, units, 1, ckpt); err != nil || len(got) != 0 {
		t.Errorf("runRound on a cancelled context = %v, %v", got, err)
	}
	if got, err := runAll(ctx, nil, units, cfg, ckpt); err != nil || len(got) != 0 {
		t.Errorf("runAll on a cancelled context = %v, %v", got, err)
	}
}

func TestScanChecksTheManifestHash(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "a.jpg"), []byte("abc"), 0o644)
	const abc = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
	units, err := scan([]manifestItem{{ID: "a", Image: "a.jpg", SHA256: abc}}, dir)
	if err != nil || units[0].SHA256 != abc {
		t.Fatalf("scan = %v, %v", units, err)
	}
	if _, err := scan([]manifestItem{{ID: "a", Image: "a.jpg", SHA256: strings.Repeat("0", 64)}}, dir); err == nil {
		t.Error("a hash that differs from the manifest must fail")
	}
}
