package main

// Tests of the batch path of the embedder against a stub of the API in which a batch item can be charged while its
// event never reaches the client: the connection ends after the server ran the item and before it wrote the event.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// batchStub keeps a charge per (idempotency key, item index) like the real API: sending the same batch again replays
// the items that finished and runs the rest. lose[i] is how many streams lose the event of item i.
type batchStub struct {
	mu      sync.Mutex
	lose    map[int]int
	charged map[string]bool
	keys    []string
	charges int
}

func (s *batchStub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch r.URL.Path {
	case "/v1/models":
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"data":[{"id":"dinov3","available":true,"prices":[{"unit":"image","price":0.05}]}]}`)
	case "/v1/images":
		data, _ := io.ReadAll(r.Body)
		sum := sha256.Sum256(data)
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"id":"img_%s","width":8,"height":8,"bytes":%d,"contentType":"image/png"}`, hex.EncodeToString(sum[:]), len(data))
	case "/v1/batch/embeddings":
		key := r.Header.Get("Idempotency-Key")
		s.keys = append(s.keys, key)
		var body struct {
			Items []json.RawMessage `json:"items"`
		}
		json.NewDecoder(r.Body).Decode(&body)
		w.Header().Set("Content-Type", "text/event-stream")
		fl := w.(http.Flusher)
		for i := range body.Items {
			id := fmt.Sprintf("%s %d", key, i)
			replayed := s.charged[id]
			if !replayed {
				s.charged[id] = true
				s.charges++
			}
			if s.lose[i] > 0 {
				s.lose[i]--
				return // charged, but the stream ends before the event
			}
			item := map[string]any{"index": i, "status": "ok", "replayed": replayed, "result": map[string]any{
				"model": "dinov3", "dimensions": 2, "cost": 0.05, "cache": "miss", "embeddings": []map[string]any{{"index": 0, "vector": []float32{float32(i + 1), 1}}},
				"billing": map[string]any{"total": 0.05, "charged": true}, "timing": map[string]int{"queueMs": 1, "processingMs": 2},
			}}
			data, _ := json.Marshal(item)
			fmt.Fprintf(w, "event: item\ndata: %s\n\n", data)
			fl.Flush()
		}
		fmt.Fprint(w, "event: summary\ndata: {\"counts\":{\"ok\":0,\"error\":0,\"expired\":0},\"cost\":0,\"elapsedMs\":1}\n\n")
	default:
		http.Error(w, `{"error":{"code":"not_found","message":"no such endpoint"}}`, 404)
	}
}

func distinct(keys []string) int {
	set := map[string]bool{}
	for _, k := range keys {
		set[k] = true
	}
	return len(set)
}

func TestItemChargedButEventLostIsRecoveredUnderTheSameKey(t *testing.T) {
	pause = func(time.Duration) {}
	stub := &batchStub{lose: map[int]int{2: 1}, charged: map[string]bool{}}
	api := httptest.NewServer(stub)
	defer api.Close()
	client := keyedClient(t, api.URL)
	index, dir := freeLibrary(t, 6)
	report, err := indexLibrary(client, index, dir, []string{"dinov3"}, 6, 2, func(string) {})
	if err != nil || report.Stopped != nil || len(report.Failed) != 0 || report.Embedded != 6 {
		t.Fatalf("%v %+v", err, report)
	}
	// the lost item is asked again under the key of the first batch: two sends, one key, one charge per image
	if len(stub.keys) != 2 || distinct(stub.keys) != 1 || stub.charges != 6 {
		t.Fatalf("keys %v charges %d", stub.keys, stub.charges)
	}
	replayed := 0
	for _, rows := range report.Rows {
		for _, r := range rows {
			if r.Replayed {
				replayed++
			}
		}
	}
	if replayed != 1 {
		t.Fatalf("only the item whose event was lost comes back as a replay, got %d", replayed)
	}
}

func TestItemWithUnknownOutcomeIsNeverSentUnderANewKey(t *testing.T) {
	pause = func(time.Duration) {}
	stub := &batchStub{lose: map[int]int{1: 1 + recoverySends}, charged: map[string]bool{}}
	api := httptest.NewServer(stub)
	defer api.Close()
	client := keyedClient(t, api.URL)
	index, dir := freeLibrary(t, 4)
	report, err := indexLibrary(client, index, dir, []string{"dinov3"}, 4, 2, func(string) {})
	if err != nil || report.Embedded != 1 || len(report.Failed) != 3 {
		t.Fatalf("%v %+v", err, report)
	}
	for sha, e := range report.Failed {
		if e.Code != "outcome_unknown" {
			t.Errorf("%s: %+v", sha, e)
		}
	}
	// four sends of one batch under one key; item 0 and the item whose event was lost were charged once each
	if len(stub.keys) != 1+recoverySends || distinct(stub.keys) != 1 || stub.charges != 2 {
		t.Fatalf("keys %v charges %d", stub.keys, stub.charges)
	}
}
