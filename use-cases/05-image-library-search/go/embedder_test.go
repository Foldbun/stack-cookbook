package main

// Tests of the free mode of the embedder against a stub of the API: no key, one inline call per image, no uploads,
// no batches, a size limit, and a clean stop at the daily limit. No network and no API key are needed.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"stack-cookbook/05-image-library-search/stackclient"
)

// freeStub answers /v1/embeddings like the free tier: only the project header, an inline image, a vector, no balance.
// After `daily` answered calls it answers 429 daily_limit_reached.
type freeStub struct {
	mu     sync.Mutex
	paths  []string
	images int
	daily  int
	answer int
}

func (f *freeStub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.paths = append(f.paths, r.URL.Path)
	if r.URL.Path != "/v1/embeddings" || r.Header.Get("Authorization") != "" || r.Header.Get("Idempotency-Key") != "" {
		http.Error(w, `{"error":{"code":"bad_request","message":"not the free tier"}}`, 400)
		return
	}
	var body struct {
		Model string `json:"model"`
		Image struct {
			Base64 string `json:"base64"`
		} `json:"image"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	if body.Image.Base64 == "" {
		http.Error(w, `{"error":{"code":"bad_request","message":"inline base64 image expected"}}`, 400)
		return
	}
	if f.answer >= f.daily {
		w.WriteHeader(429)
		w.Write([]byte(`{"error":{"code":"daily_limit_reached","message":"daily limit","retryable":false}}`))
		return
	}
	f.answer++
	f.images++
	fmt.Fprintf(w, `{"model":%q,"dimensions":2,"embeddings":[{"index":0,"vector":[%d,1]}],"cost":0,"billing":{"tier":"free","listPrice":0.05,"total":0.05,"charged":false},"timing":{"queueMs":1,"processingMs":2}}`, body.Model, f.answer)
}

// freeLibrary writes n distinct files and returns an index over them.
func freeLibrary(t *testing.T, n int) (*LibraryIndex, string) {
	dir := t.TempDir()
	var items []Item
	for i := 0; i < n; i++ {
		name := fmt.Sprintf("img%02d.png", i)
		data := []byte(fmt.Sprintf("not really a png %d", i))
		if err := os.WriteFile(filepath.Join(dir, name), data, 0o644); err != nil {
			t.Fatal(err)
		}
		items = append(items, Item{ID: name, Path: name, Sha256: fmt.Sprintf("%064x", i)})
	}
	index := NewIndex(filepath.Join(dir, "index"))
	index.SetItems(items)
	if err := index.Commit(); err != nil {
		t.Fatal(err)
	}
	return index, dir
}

func freeClient(t *testing.T, url string) *stackclient.Client {
	t.Setenv("FOLDBUN_API_KEY", "")
	client, err := stackclient.New(stackclient.Config{BaseURL: url, Project: "fbp_testProject0001"})
	if err != nil {
		t.Fatal(err)
	}
	if !client.Free() {
		t.Fatal("no key must mean the free tier")
	}
	return client
}

func TestFreeModeEmbedsWithSingleInlineCalls(t *testing.T) {
	stub := &freeStub{daily: 1000}
	api := httptest.NewServer(stub)
	defer api.Close()
	client := freeClient(t, api.URL)
	index, dir := freeLibrary(t, 6)
	report, err := indexLibrary(client, index, dir, models, 4, 3, func(string) {})
	if err != nil || report.Stopped != nil || len(report.Failed) != 0 || report.Embedded != 12 {
		t.Fatalf("%v %+v", err, report)
	}
	for _, p := range stub.paths {
		if p != "/v1/embeddings" {
			t.Fatalf("a free run may only call /v1/embeddings, got %s", p)
		}
	}
	if len(stub.paths) != 12 {
		t.Fatalf("one call per image and model, got %d", len(stub.paths))
	}
	for sha, rows := range report.Rows {
		if len(rows) != 2 || rows[0].Charged != 0 || rows[0].List <= 0 {
			t.Fatalf("%s: %+v", sha, rows)
		}
	}
	s := client.Summary()
	if s.ChargedTotal != 0 || s.ListTotal <= 0 || !strings.Contains(costLine(client), "free tier, not charged") {
		t.Fatalf("%+v %q", s, costLine(client))
	}
	again, err := indexLibrary(client, index, dir, models, 4, 3, func(string) {}) // incremental: nothing is missing
	if err != nil || again.Embedded != 0 || len(stub.paths) != 12 {
		t.Fatalf("%v %+v %d", err, again, len(stub.paths))
	}
}

func TestFreeModeNeedsAKeyForABiggerLibrary(t *testing.T) {
	stub := &freeStub{daily: 1000}
	api := httptest.NewServer(stub)
	defer api.Close()
	client := freeClient(t, api.URL)
	index, dir := freeLibrary(t, freeMaxImages+1)
	_, err := indexLibrary(client, index, dir, models, 50, 4, func(string) {})
	if stackclient.Code(err) != "missing_api_key" || !strings.Contains(err.Error(), fmt.Sprint(freeMaxImages+1)) || len(stub.paths) != 0 {
		t.Fatalf("%v, calls %v", err, stub.paths)
	}
}

func TestFreeModeStopsAtTheDailyLimitAndResumes(t *testing.T) {
	stub := &freeStub{daily: 7}
	api := httptest.NewServer(stub)
	defer api.Close()
	client := freeClient(t, api.URL)
	index, dir := freeLibrary(t, 10)
	report, err := indexLibrary(client, index, dir, models, 4, 1, func(string) {})
	if err != nil || report.Stopped == nil || report.Stopped.Code != "daily_limit_reached" || len(report.Failed) != 0 || report.Embedded != 7 {
		t.Fatalf("%v %+v", err, report)
	}
	loaded, err := LoadIndex(index.Dir) // what finished was committed
	if err != nil || len(loaded.Missing("dinov3")) != 3 || len(loaded.Missing("siglip2")) != 10 {
		t.Fatalf("%v dinov3 missing %v", err, loaded.Missing("dinov3"))
	}
	stub.mu.Lock()
	stub.daily = 1000 // "the next day"
	stub.mu.Unlock()
	report, err = indexLibrary(client, loaded, dir, models, 4, 1, func(string) {})
	if err != nil || report.Stopped != nil || report.Embedded != 13 {
		t.Fatalf("%v %+v", err, report)
	}
}
