package main

// Tests of the async mode of the embedder against a stub of the API: one job per model, polling, results read by
// cursor, an item that fails and is left for the next run, and the refusal without a key.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"stack-cookbook/05-image-library-search/stackclient"
)

// jobStub answers the calls of an async run. A job is `running` on its first poll and `completed` on the next; the
// item at index failAt of every job (when it is >= 0) ends as an error.
type jobStub struct {
	mu     sync.Mutex
	paths  []string
	keys   []string
	jobs   map[string]*stubJob
	failAt int
}

type stubJob struct {
	model string
	n     int
	polls int
}

func (s *jobStub) job(j *stubJob, id, status string) map[string]any {
	ok := 0
	if status == "completed" {
		ok = j.n
		if s.failAt >= 0 {
			ok--
		}
	}
	failed := j.n - ok
	if status != "completed" {
		failed = 0
	}
	return map[string]any{
		"id": id, "object": "job", "model": j.model, "endpoint": "embeddings", "status": status,
		"counts":   map[string]int{"total": j.n, "pending": j.n - ok - failed, "running": 0, "ok": ok, "error": failed, "expired": 0, "cancelled": 0},
		"estimate": float64(j.n) * 0.005, "cost": float64(ok) * 0.005, "createdAt": "2026-11-18T00:00:00Z",
		"expiresAt": "2026-11-19T00:00:00Z", "metadata": nil,
	}
}

func (s *jobStub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.paths = append(s.paths, r.Method+" "+r.URL.Path)
	w.Header().Set("Content-Type", "application/json")
	switch {
	case r.URL.Path == "/v1/models":
		price := `[{"unit":"image","price":0.05}]`
		async := `[{"unit":"image","price":0.005}]`
		fmt.Fprintf(w, `{"data":[{"id":"dinov3","available":true,"prices":%s,"asyncPrices":%s},{"id":"siglip2","available":true,"prices":%s,"asyncPrices":%s}]}`, price, async, price, async)
	case r.URL.Path == "/v1/images":
		var buf strings.Builder
		b := make([]byte, 4096)
		for {
			n, err := r.Body.Read(b)
			buf.Write(b[:n])
			if err != nil {
				break
			}
		}
		sum := sha256.Sum256([]byte(buf.String()))
		fmt.Fprintf(w, `{"id":"img_%s","width":8,"height":8,"bytes":%d,"contentType":"image/png"}`, hex.EncodeToString(sum[:]), buf.Len())
	case r.URL.Path == "/v1/jobs" && r.Method == "POST":
		var body struct {
			Model string            `json:"model"`
			Items []json.RawMessage `json:"items"`
		}
		json.NewDecoder(r.Body).Decode(&body)
		id := fmt.Sprintf("job_%032x", len(s.jobs)+1)
		s.jobs[id] = &stubJob{model: body.Model, n: len(body.Items)}
		s.keys = append(s.keys, r.Header.Get("Idempotency-Key"))
		w.WriteHeader(202)
		json.NewEncoder(w).Encode(s.job(s.jobs[id], id, "queued"))
	case strings.HasSuffix(r.URL.Path, "/results"):
		id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/v1/jobs/"), "/results")
		j, items := s.jobs[id], []map[string]any{}
		start, _ := strconv.Atoi(r.URL.Query().Get("cursor"))
		limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
		var next any
		for i := start; i < j.n && len(items) < limit; i++ {
			if i == s.failAt {
				items = append(items, map[string]any{"index": i, "status": "error", "error": map[string]string{"code": "bad_model_output", "message": "scripted"}})
				continue
			}
			result := map[string]any{
				"model": j.model, "dimensions": 2, "cost": 0.005, "cache": "miss", "embeddings": []map[string]any{{"index": 0, "vector": []float32{float32(i + 1), 1}}},
				"billing": map[string]any{"total": 0.005, "charged": true}, "timing": map[string]int{"queueMs": 1, "processingMs": 2},
			}
			items = append(items, map[string]any{"index": i, "status": "ok", "result": result})
		}
		if len(items) == limit {
			next = strconv.Itoa(start + limit)
		}
		json.NewEncoder(w).Encode(map[string]any{"items": items, "next": next})
	case strings.HasPrefix(r.URL.Path, "/v1/jobs/"):
		id := strings.TrimPrefix(r.URL.Path, "/v1/jobs/")
		j := s.jobs[id]
		j.polls++
		status := "running"
		if j.polls > 1 {
			status = "completed"
		}
		json.NewEncoder(w).Encode(s.job(j, id, status))
	default:
		http.Error(w, `{"error":{"code":"not_found","message":"no such endpoint"}}`, 404)
	}
}

func keyedClient(t *testing.T, url string) *stackclient.Client {
	c, err := stackclient.New(stackclient.Config{BaseURL: url, APIKey: "fbs_stub"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestAsyncEmbedsWithOneJobPerModel(t *testing.T) {
	stub := &jobStub{jobs: map[string]*stubJob{}, failAt: -1}
	api := httptest.NewServer(stub)
	defer api.Close()
	client := keyedClient(t, api.URL)
	index, dir := freeLibrary(t, 6)
	report, err := indexLibraryAsync(client, index, dir, models, 3, time.Millisecond, func(string) {})
	if err != nil || report.Stopped != nil || len(report.Failed) != 0 || report.Embedded != 12 {
		t.Fatalf("%v %+v", err, report)
	}
	submits := 0
	for _, p := range stub.paths {
		if p == "POST /v1/jobs" {
			submits++
		}
		if strings.Contains(p, "/v1/batch") || p == "POST /v1/embeddings" {
			t.Fatalf("an async run may not call %s", p)
		}
	}
	if submits != len(models) || len(stub.keys) != len(models) || stub.keys[0] == "" || stub.keys[0] == stub.keys[1] {
		t.Fatalf("submits %d keys %v", submits, stub.keys)
	}
	for sha, rows := range report.Rows {
		if len(rows) != 2 || rows[0].Endpoint != "/v1/jobs" || rows[0].Charged != 0.005 {
			t.Fatalf("%s: %+v", sha, rows)
		}
	}
	if s := client.Summary(); s.Calls != 12 || s.ChargedTotal != 0.06 {
		t.Fatalf("%+v", s)
	}
	if len(index.Missing("dinov3")) != 0 || len(index.Missing("siglip2")) != 0 {
		t.Fatal("the index still misses vectors")
	}
	n := len(stub.paths)
	again, err := indexLibraryAsync(client, index, dir, models, 3, time.Millisecond, func(string) {}) // nothing is missing
	if err != nil || again.Embedded != 0 || len(stub.paths) != n {
		t.Fatalf("%v %+v %d", err, again, len(stub.paths)-n)
	}
}

func TestAsyncLeavesAFailedItemForTheNextRun(t *testing.T) {
	stub := &jobStub{jobs: map[string]*stubJob{}, failAt: 1}
	api := httptest.NewServer(stub)
	defer api.Close()
	client := keyedClient(t, api.URL)
	index, dir := freeLibrary(t, 5)
	report, err := indexLibraryAsync(client, index, dir, models, 2, time.Millisecond, func(string) {})
	if err != nil || len(report.Failed) != 1 || report.Embedded != 8 {
		t.Fatalf("%v %+v", err, report)
	}
	for _, f := range report.Failed {
		if f.Code != "bad_model_output" {
			t.Fatalf("%+v", f)
		}
	}
	if len(index.Missing("dinov3")) != 1 || len(index.Missing("siglip2")) != 1 {
		t.Fatalf("missing %v %v", index.Missing("dinov3"), index.Missing("siglip2"))
	}
}

func TestAsyncNeedsAKey(t *testing.T) {
	stub := &jobStub{jobs: map[string]*stubJob{}, failAt: -1}
	api := httptest.NewServer(stub)
	defer api.Close()
	client := freeClient(t, api.URL)
	index, dir := freeLibrary(t, 3)
	_, err := indexLibraryAsync(client, index, dir, models, 2, time.Millisecond, func(string) {})
	if stackclient.Code(err) != "missing_api_key" || len(stub.paths) != 0 {
		t.Fatalf("%v, calls %v", err, stub.paths)
	}
}
