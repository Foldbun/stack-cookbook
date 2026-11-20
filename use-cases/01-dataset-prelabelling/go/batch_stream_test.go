package main

// A batch item that the server runs and charges, but whose event never reaches the client (the connection ends first),
// against a stub of the API that keeps one charge per (idempotency key, item index) like the real one.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"stack-cookbook/01-dataset-prelabelling/stackclient"
)

type lossyBatchStub struct {
	mu      sync.Mutex
	lose    map[int]int // item index -> how many streams lose its event
	charged map[string]bool
	keys    []string
	charges int
}

func (s *lossyBatchStub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch r.URL.Path {
	case "/v1/models":
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"data":[{"id":"sam3","available":true,"prices":[]}]}`)
	case "/v1/batch/segment":
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
				"model": "sam3", "instances": []any{}, "cost": 0.7, "billing": map[string]any{"total": 0.7, "charged": true},
				"timing": map[string]int{"queueMs": 1, "processingMs": 2},
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

func lossyUnits(n int) []*Unit {
	units := make([]*Unit, n)
	for i := range units {
		units[i] = &Unit{Item: manifestItem{ID: fmt.Sprintf("u%d", i)}, Body: map[string]any{"image": map[string]string{"id": fmt.Sprintf("img_%d", i)}}}
	}
	return units
}

func distinctKeys(keys []string) int {
	set := map[string]bool{}
	for _, k := range keys {
		set[k] = true
	}
	return len(set)
}

func TestItemChargedButEventLostIsRecoveredUnderTheSameKey(t *testing.T) {
	pause = func(time.Duration) {}
	stub := &lossyBatchStub{lose: map[int]int{1: 1}, charged: map[string]bool{}}
	api := httptest.NewServer(stub)
	defer api.Close()
	client, err := stackclient.New(stackclient.Config{BaseURL: api.URL, APIKey: "fbs_stub"})
	if err != nil {
		t.Fatal(err)
	}
	out, err := runBatch(client, "sam3", lossyUnits(4), "k1", "t", func(*Unit, *outcome) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	for id, o := range out {
		if o.Status != "ok" {
			t.Errorf("%s: %+v", id, o)
		}
	}
	if len(stub.keys) != 2 || distinctKeys(stub.keys) != 1 || stub.charges != 4 {
		t.Fatalf("keys %v charges %d", stub.keys, stub.charges)
	}
	if rows := out["u1"].Calls; len(rows) != 1 || !rows[0].Replayed {
		t.Fatalf("the lost item comes back as a replay: %+v", rows)
	}
}

func TestItemWithUnknownOutcomeIsNotRetryableInANewRound(t *testing.T) {
	pause = func(time.Duration) {}
	stub := &lossyBatchStub{lose: map[int]int{1: streamAttempts}, charged: map[string]bool{}}
	api := httptest.NewServer(stub)
	defer api.Close()
	client, err := stackclient.New(stackclient.Config{BaseURL: api.URL, APIKey: "fbs_stub"})
	if err != nil {
		t.Fatal(err)
	}
	out, err := runBatch(client, "sam3", lossyUnits(4), "k1", "t", func(*Unit, *outcome) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	if out["u0"].Status != "ok" {
		t.Errorf("u0: %+v", out["u0"])
	}
	for _, id := range []string{"u1", "u2", "u3"} {
		o := out[id]
		if o.Status != "error" || o.Err.Code != "outcome_unknown" || retryable(o) {
			t.Errorf("%s: %+v %+v", id, o, o.Err)
		}
	}
	// three sends of one batch under one key; item 0 and the item whose event was lost were charged once each
	if len(stub.keys) != streamAttempts || distinctKeys(stub.keys) != 1 || stub.charges != 2 {
		t.Fatalf("keys %v charges %d", stub.keys, stub.charges)
	}
}
