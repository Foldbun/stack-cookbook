package main

import (
	"bytes"
	"context"
	"encoding/json"
	"image"
	"image/png"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"stack-cookbook/01-dataset-prelabelling/stackclient"
)

func writePNG(t *testing.T, dir string, w, h int) string {
	t.Helper()
	var buf bytes.Buffer
	if err := png.Encode(&buf, image.NewGray(image.Rect(0, 0, w, h))); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "a.png")
	if err := os.WriteFile(path, buf.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

// On the free tier the size is read from the file and the image goes inline; with a handle it is the handle.
func TestFreeTierMeasuresLocallyAndSendsTheImageInline(t *testing.T) {
	path := writePNG(t, t.TempDir(), 7, 5)
	u := &Unit{Item: manifestItem{ID: "a", Image: "a.png"}, Path: path, Prompts: []map[string]any{{"text": "x"}}}
	if err := measureAll([]*Unit{u}); err != nil || u.Width != 7 || u.Height != 5 {
		t.Fatalf("measureAll = %v, size %dx%d", err, u.Width, u.Height)
	}
	body, err := buildBody(u, &config{Mode: "text", Review: 0.3})
	if err != nil {
		t.Fatal(err)
	}
	if img, ok := body["image"].(map[string]string); !ok || img["base64"] == "" || img["id"] != "" {
		t.Errorf("free image = %v, want inline base64", body["image"])
	}
	u.Handle = "img_1"
	body, _ = buildBody(u, &config{Mode: "text"})
	if img := body["image"].(map[string]string); img["id"] != "img_1" || img["base64"] != "" {
		t.Errorf("keyed image = %v, want the handle", body["image"])
	}
}

// A free run makes one call per image, with the project header and no Authorization.
func TestFreeRoundSendsSingleCalls(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/v1/segment" || r.Header.Get("Authorization") != "" || r.Header.Get("X-Foldbun-Project") == "" {
			t.Errorf("unexpected request %s auth=%q project=%q", r.URL.Path, r.Header.Get("Authorization"), r.Header.Get("X-Foldbun-Project"))
		}
		var in map[string]any
		json.NewDecoder(r.Body).Decode(&in)
		if img, ok := in["image"].(map[string]any); in["model"] != "sam3" || !ok || img["base64"] == nil {
			t.Errorf("body = %v", in)
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"instances":[],"billing":{"tier":"free","listPrice":0.7,"total":0.7,"lines":[],"charged":false},"cost":0}`))
	}))
	defer srv.Close()
	t.Setenv("FOLDBUN_API_KEY", "")
	t.Setenv("FOLDBUN_MAX_CREDITS", "")
	client, err := stackclient.New(stackclient.Config{BaseURL: srv.URL, MaxConcurrency: 2})
	if err != nil || !client.Free() {
		t.Fatalf("New = %v, free = %v", err, client != nil && client.Free())
	}
	dir := t.TempDir()
	path := writePNG(t, dir, 4, 4)
	units := []*Unit{}
	for _, id := range []string{"a", "b", "c"} {
		u := &Unit{Item: manifestItem{ID: id}, Path: path, Prompts: []map[string]any{{"text": "x"}}, Digest: id}
		u.Body, _ = buildBody(u, &config{Mode: "text"})
		units = append(units, u)
	}
	ckpt, err := openCheckpoint(filepath.Join(dir, "c.jsonl"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer ckpt.Close()
	got, err := runRound(context.Background(), client, &config{Model: "sam3", Concurrency: 2}, units, 1, ckpt)
	if err != nil || len(got) != 3 || calls.Load() != 3 {
		t.Fatalf("runRound = %v, %v after %d calls", got, err, calls.Load())
	}
	if o := got["a"]; o.Status != "ok" || len(o.Calls) != 1 || o.Calls[0].Charged != 0 {
		t.Errorf("outcome a = %+v", o)
	}
	if s := client.Summary(); s.Calls != 3 || s.ChargedTotal != 0 {
		t.Errorf("summary = %+v", s)
	}
}

func TestFreeTierRefusesWhatNeedsAKey(t *testing.T) {
	t.Setenv("FOLDBUN_API_KEY", "")
	client, _ := stackclient.New(stackclient.Config{BaseURL: "http://127.0.0.1:1"})
	err := client.RequireKey("this manifest has 11")
	if stackclient.Code(err) != "missing_api_key" || !strings.Contains(err.Error(), "this manifest has 11") {
		t.Errorf("RequireKey = %v", err)
	}
}
