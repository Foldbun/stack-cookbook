package main

// Tests of the search service with httptest: an index built in a temporary directory and a stubbed embedder, plus one
// test of the real embedder against a stub of the API.

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"stack-cookbook/05-image-library-search/stackclient"
)

// testService has four images: a and b are close, c is far from both, and d is an exact copy of c. The siglip2 vectors
// make "pear" closest to c and d, then a.
func testService(t *testing.T, embed func(string) ([]float32, error)) *service {
	t.Helper()
	index := NewIndex(t.TempDir())
	index.SetItems([]Item{{ID: "a", Sha256: "sa"}, {ID: "b", Sha256: "sb"}, {ID: "c", Sha256: "sc"}, {ID: "d", Sha256: "sc"}})
	shas := []string{"sa", "sb", "sc"}
	if err := index.Add("dinov3", shas, [][]float32{{1, 0.1}, {1, 0.2}, {0, 1}}); err != nil {
		t.Fatal(err)
	}
	if err := index.Add("siglip2", shas, [][]float32{{0.5, 0.5}, {1, 0}, {0, 1}}); err != nil {
		t.Fatal(err)
	}
	if err := index.Commit(); err != nil {
		t.Fatal(err)
	}
	loaded, err := LoadIndex(index.Dir) // what `serve` does: read the files an earlier run wrote
	if err != nil {
		t.Fatal(err)
	}
	return newService(loaded, embed)
}

func pear(string) ([]float32, error) { return []float32{0, 1}, nil }

func get(t *testing.T, h http.Handler, method, target string) (int, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(method, target, nil))
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("%s %s: content type %q", method, target, ct)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("%s %s: not JSON: %v: %s", method, target, err, rec.Body.String())
	}
	return rec.Code, body
}

func ids(body map[string]any) []string {
	out := []string{}
	for _, r := range body["results"].([]any) {
		out = append(out, r.(map[string]any)["id"].(string))
	}
	return out
}

func TestSearch(t *testing.T) {
	h := testService(t, pear).handler()
	for _, c := range []struct {
		name, target string
		want         []string
	}{
		{"ranked by score", "/search?q=pear", []string{"c", "d", "a", "b"}},
		{"k cuts the list", "/search?q=pear&k=1", []string{"c"}},
		{"url-encoded text", "/search?q=a%20pear%20on%20a%20table&k=2", []string{"c", "d"}},
		{"k beyond the library", "/search?q=pear&k=100000", []string{"c", "d", "a", "b"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			status, body := get(t, h, "GET", c.target)
			if status != 200 || !reflect.DeepEqual(ids(body), c.want) {
				t.Fatalf("%d %v", status, body)
			}
			if body["model"] != "siglip2" {
				t.Fatalf("model: %v", body["model"])
			}
		})
	}
}

func TestSearchPassesTheTextToTheEmbedder(t *testing.T) {
	var got []string
	h := testService(t, func(text string) ([]float32, error) { got = append(got, text); return []float32{0, 1}, nil }).handler()
	get(t, h, "GET", "/search?q=a+red+bicycle")
	if !reflect.DeepEqual(got, []string{"a red bicycle"}) {
		t.Fatalf("embedder saw %q", got)
	}
}

func TestSimilar(t *testing.T) {
	h := testService(t, pear).handler()
	status, body := get(t, h, "GET", "/similar?id=c")
	if status != 200 || !reflect.DeepEqual(ids(body), []string{"d", "b", "a"}) { // the query image itself is left out; its copy scores 1
		t.Fatalf("%d %v", status, body)
	}
	if first := body["results"].([]any)[0].(map[string]any)["score"].(float64); first != 1 {
		t.Fatalf("an exact copy scores %v", first)
	}
	status, body = get(t, h, "GET", "/similar?id=a&k=1")
	if status != 200 || !reflect.DeepEqual(ids(body), []string{"b"}) {
		t.Fatalf("%d %v", status, body)
	}
}

func TestDuplicates(t *testing.T) {
	h := testService(t, pear).handler()
	for _, c := range []struct {
		name, target string
		want         [][]string
	}{
		{"default threshold", "/duplicates", [][]string{{"a", "b"}, {"c", "d"}}},
		{"strict", "/duplicates?threshold=0.9999", [][]string{{"c", "d"}}},
	} {
		t.Run(c.name, func(t *testing.T) {
			status, body := get(t, h, "GET", c.target)
			got := [][]string{}
			for _, g := range body["groups"].([]any) {
				ids := []string{}
				for _, id := range g.([]any) {
					ids = append(ids, id.(string))
				}
				got = append(got, ids)
			}
			if status != 200 || !reflect.DeepEqual(got, c.want) {
				t.Fatalf("%d %v", status, body)
			}
		})
	}
}

func TestHealthz(t *testing.T) {
	status, body := get(t, testService(t, pear).handler(), "GET", "/healthz")
	if status != 200 || body["status"] != "ok" || body["images"] != 4.0 || body["textSearch"] != true {
		t.Fatalf("%d %v", status, body)
	}
	if _, body = get(t, testService(t, nil).handler(), "GET", "/healthz"); body["textSearch"] != false {
		t.Fatalf("%v", body)
	}
}

func TestBadInput(t *testing.T) {
	h := testService(t, pear).handler()
	for _, c := range []struct {
		name, method, target string
		status               int
		code                 string
	}{
		{"missing q", "GET", "/search", 400, "bad_request"},
		{"empty q", "GET", "/search?q=", 400, "bad_request"},
		{"q too long", "GET", "/search?q=" + strings.Repeat("x", maxQueryLen+1), 400, "bad_request"},
		{"k is not a number", "GET", "/search?q=pear&k=ten", 400, "bad_request"},
		{"k is zero", "GET", "/similar?id=a&k=0", 400, "bad_request"},
		{"k is negative", "GET", "/similar?id=a&k=-1", 400, "bad_request"},
		{"unknown image", "GET", "/similar?id=nope", 404, "not_found"},
		{"missing id", "GET", "/similar", 400, "bad_request"},
		{"threshold is not a number", "GET", "/duplicates?threshold=high", 400, "bad_request"},
		{"threshold above 1", "GET", "/duplicates?threshold=1.5", 400, "bad_request"},
		{"threshold is zero", "GET", "/duplicates?threshold=0", 400, "bad_request"},
		{"threshold is NaN", "GET", "/duplicates?threshold=NaN", 400, "bad_request"},
		{"unknown path", "GET", "/index", 404, "not_found"},
		{"no indexing over HTTP", "POST", "/search?q=pear", 405, "method_not_allowed"},
		{"no deleting", "DELETE", "/similar?id=a", 405, "method_not_allowed"},
	} {
		t.Run(c.name, func(t *testing.T) {
			status, body := get(t, h, c.method, c.target)
			e, _ := body["error"].(map[string]any)
			if status != c.status || e["code"] != c.code || e["message"] == "" || len(body) != 1 {
				t.Fatalf("%d %v", status, body)
			}
		})
	}
}

func TestSearchErrors(t *testing.T) {
	for _, c := range []struct {
		name   string
		embed  func(string) ([]float32, error)
		status int
		code   string
	}{
		{"no key", nil, 503, "text_search_off"},
		{"allowance used up", func(string) ([]float32, error) {
			return nil, &stackclient.Error{Status: 429, Code: "daily_limit_reached", Message: "used up"}
		}, 429, "daily_limit_reached"},
		{"credit cap", func(string) ([]float32, error) {
			return nil, &stackclient.Error{Code: "budget_exceeded", Message: "cap"}
		}, 429, "budget_exceeded"},
		{"API failure", func(string) ([]float32, error) {
			return nil, &stackclient.Error{Status: 503, Code: "model_busy", Message: "busy"}
		}, 502, "model_busy"},
		{"other failure", func(string) ([]float32, error) { return nil, errors.New("boom") }, 502, "upstream_error"},
		{"another vector length", func(string) ([]float32, error) { return []float32{1, 2, 3}, nil }, 502, "dimension_mismatch"},
	} {
		t.Run(c.name, func(t *testing.T) {
			status, body := get(t, testService(t, c.embed).handler(), "GET", "/search?q=pear")
			if e, _ := body["error"].(map[string]any); status != c.status || e["code"] != c.code {
				t.Fatalf("%d %v", status, body)
			}
		})
	}
}

// The real embedder against a stub of the API: one call per query, with siglip2 and the text.
func TestEmbedQuery(t *testing.T) {
	var posted []map[string]any
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/models":
			w.Write([]byte(`{"data":[{"id":"siglip2","available":true,"pool":"free","perMinute":6000,"prices":[{"unit":"text","price":0.01}]}]}`))
		case "/v1/embeddings":
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			posted = append(posted, body)
			w.Write([]byte(`{"model":"siglip2","dimensions":2,"embeddings":[{"index":0,"vector":[0,1]}],"cost":0,"billing":{"total":0.01},"timing":{"queueMs":1,"processingMs":2}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer api.Close()
	client, err := stackclient.New(stackclient.Config{APIKey: "fbs_test", BaseURL: api.URL, NoThrottle: true})
	if err != nil {
		t.Fatal(err)
	}
	h := testService(t, embedQuery(client)).handler()
	status, body := get(t, h, "GET", "/search?q=pear&k=2")
	if status != 200 || !reflect.DeepEqual(ids(body), []string{"c", "d"}) {
		t.Fatalf("%d %v", status, body)
	}
	if len(posted) != 1 || posted[0]["model"] != "siglip2" || !reflect.DeepEqual(posted[0]["texts"], []any{"pear"}) {
		t.Fatalf("API requests: %v", posted)
	}
}

func TestGracefulShutdown(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, stop := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- listenAndServe(ctx, ln, testService(t, pear).handler()) }()
	resp, err := http.Get("http://" + ln.Addr().String() + "/healthz")
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("%v %v", resp, err)
	}
	resp.Body.Close()
	stop()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("shutdown: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the server did not stop")
	}
	if _, err := http.Get("http://" + ln.Addr().String() + "/healthz"); err == nil {
		t.Fatal("still answering after shutdown")
	}
}
