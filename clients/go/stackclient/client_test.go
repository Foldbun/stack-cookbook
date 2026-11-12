// The Go client against the fake API. The scenarios come from harness/scenarios.json, which the Python and
// TypeScript client tests use as well. Run by `harness check` (tests/test_client_conformance.py), which starts
// the fake API and sets FAKE_API_URL; by hand: FAKE_API_URL=http://127.0.0.1:8787 go test ./...
package stackclient

import (
	"bytes"
	"encoding/json"
	"image"
	"image/png"
	"net/http"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"
)

const key = "fbs_fake"

var scenarios map[string]json.RawMessage

func fakeURL(t *testing.T) string {
	t.Helper()
	u := os.Getenv("FAKE_API_URL")
	if u == "" {
		t.Skip("FAKE_API_URL is not set: run `uv run python -m harness check`, or start `python -m harness fakeapi` and export it")
	}
	return u
}

// scenario resets the fake API; name "" resets it to plain behaviour.
func scenario(t *testing.T, name string) string {
	t.Helper()
	base := fakeURL(t)
	if scenarios == nil {
		raw, err := os.ReadFile("../../../harness/scenarios.json")
		if err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(raw, &scenarios); err != nil {
			t.Fatal(err)
		}
	}
	body := "{}"
	if name != "" {
		body = string(scenarios[name])
	}
	resp, err := http.Post(base+"/_fake/reset", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	t.Cleanup(func() {
		if v := fakeLog(t).Violations; len(v) > 0 {
			t.Errorf("the fake API reports violations of the API description: %v", v)
		}
	})
	return base
}

type logEntry struct {
	Path           string  `json:"path"`
	Status         int     `json:"status"`
	UserAgent      string  `json:"userAgent"`
	IdempotencyKey string  `json:"idempotencyKey"`
	Accept         *string `json:"accept"`
}

type fakeLogDoc struct {
	Log        []logEntry `json:"log"`
	Violations []string   `json:"violations"`
}

func fakeLog(t *testing.T) fakeLogDoc {
	t.Helper()
	// The fake API writes its log entry after it has sent the response: let the last one land.
	time.Sleep(100 * time.Millisecond)
	resp, err := http.Get(fakeURL(t) + "/_fake/log")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var doc fakeLogDoc
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		t.Fatal(err)
	}
	return doc
}

func calls(t *testing.T, path string) []logEntry {
	var out []logEntry
	for _, e := range fakeLog(t).Log {
		if e.Path == path {
			out = append(out, e)
		}
	}
	return out
}

func statuses(es []logEntry) []int {
	out := []int{}
	for _, e := range es {
		out = append(out, e.Status)
	}
	return out
}

// pngOf is a distinct small PNG per salt.
func pngOf(salt int) []byte {
	var buf bytes.Buffer
	if err := png.Encode(&buf, image.NewNRGBA(image.Rect(0, 0, 64+salt, 48))); err != nil {
		panic(err)
	}
	return buf.Bytes()
}

func newClient(t *testing.T, base string, cfg Config) *Client {
	t.Helper()
	cfg.BaseURL = base
	if cfg.APIKey == "" {
		cfg.APIKey = key
	}
	c, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func uploadPNG(t *testing.T, c *Client, salt int) string {
	t.Helper()
	info, err := c.Upload(pngOf(salt), "image/png")
	if err != nil {
		t.Fatal(err)
	}
	return info.ID
}

func wantCode(t *testing.T, err error, code string) *Error {
	t.Helper()
	var e *Error
	if err == nil {
		t.Fatalf("expected error %s, got none", code)
	}
	if got := Code(err); got != code {
		t.Fatalf("expected error %s, got %v", code, err)
	}
	e, _ = err.(*Error)
	return e
}

func ocr(image any) map[string]any { return map[string]any{"image": image} }

func TestMissingKey(t *testing.T) {
	t.Setenv("FOLDBUN_API_KEY", "")
	_, err := New(Config{})
	if e := wantCode(t, err, "missing_api_key"); e.Message != MissingKey {
		t.Errorf("message %q", e.Message)
	}
}

func TestEnvConfiguration(t *testing.T) {
	base := scenario(t, "")
	t.Setenv("FOLDBUN_API_KEY", key)
	t.Setenv("FOLDBUN_API_BASE", base)
	t.Setenv("FOLDBUN_MAX_CREDITS", "1.5")
	c, err := New(Config{})
	if err != nil {
		t.Fatal(err)
	}
	models, err := c.Models()
	if err != nil {
		t.Fatal(err)
	}
	if c.MaxCredits() == nil || *c.MaxCredits() != 1.5 || models["ppocr-v6"].ID == "" {
		t.Errorf("cap %v, models %v", c.MaxCredits(), models)
	}
}

func TestUploadOCRAndLedger(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{})
	first, err := c.Upload(pngOf(0), "image/png")
	if err != nil {
		t.Fatal(err)
	}
	again, _ := c.Upload(pngOf(0), "image/png")
	if first != again || !strings.HasPrefix(first.ID, "img_") || first.Width != 64 || first.Height != 48 {
		t.Fatalf("uploads %+v %+v", first, again)
	}
	raw1, err := c.Post("/v1/ocr", ocr(ImageHandle(first.ID)), Opts{Tag: "a"})
	if err != nil {
		t.Fatal(err)
	}
	raw2, err := c.Post("/v1/ocr", ocr(ImageBase64(pngOf(0))), Opts{Tag: "b"})
	if err != nil {
		t.Fatal(err)
	}
	var r1, r2 struct {
		Image   struct{ ID string }
		Cost    float64
		Billing struct{ Pool string }
	}
	_ = json.Unmarshal(raw1, &r1)
	_ = json.Unmarshal(raw2, &r2)
	if r1.Image.ID != first.ID || r2.Image.ID != first.ID || r1.Billing.Pool != "free" || r1.Cost != 0 {
		t.Errorf("results %+v %+v", r1, r2)
	}
	model := "ppocr-v6"
	want := []Row{{Endpoint: "/v1/ocr", Model: &model, List: 0.25, Charged: 0, Cache: nil, QueueMs: 4, ProcessingMs: 700, Replayed: false}}
	if got := c.Calls("a"); !reflect.DeepEqual(got, want) {
		t.Errorf("rows %+v", got)
	}
	if got := c.Summary(); got != (Summary{Calls: 2, ListTotal: 0.5, ChargedTotal: 0}) {
		t.Errorf("summary %+v", got)
	}
	var images, keys []logEntry
	for _, e := range fakeLog(t).Log {
		if e.UserAgent != UserAgent {
			t.Errorf("user agent %q", e.UserAgent)
		}
		if e.Path == "/v1/images" {
			images = append(images, e)
		} else if e.Path == "/v1/ocr" {
			keys = append(keys, e)
		}
	}
	if !reflect.DeepEqual(statuses(images), []int{201, 200}) || len(keys) != 2 || keys[0].IdempotencyKey == "" || keys[0].IdempotencyKey == keys[1].IdempotencyKey {
		t.Errorf("images %v keys %v", images, keys)
	}
}

func TestBilledModelAndEncodeCache(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{})
	body := map[string]any{"model": "sam3", "image": ImageHandle(uploadPNG(t, c, 0)), "prompts": []any{map[string]string{"text": "cat"}, map[string]string{"text": "dog"}}}
	type seg struct {
		Cache    string
		ImageFee float64
		Cost     float64
	}
	var got [2]seg
	for i := range got {
		raw, err := c.Post("/v1/segment", body, Opts{})
		if err != nil {
			t.Fatal(err)
		}
		_ = json.Unmarshal(raw, &got[i])
	}
	if got[0] != (seg{"miss", 0.5, 0.6}) || got[1] != (seg{"hit", 0, 0.1}) {
		t.Errorf("results %+v", got)
	}
	if rows := c.Calls(""); *rows[0].Cache != "miss" || *rows[1].Cache != "hit" || c.Summary().ChargedTotal != 0.7 {
		t.Errorf("rows %+v summary %+v", rows, c.Summary())
	}
}

func TestRetryHonoursRetryAfterAndReusesKey(t *testing.T) {
	c := newClient(t, scenario(t, "retry-model-busy"), Config{})
	start := time.Now()
	if _, err := c.Post("/v1/ocr", ocr(ImageBase64(pngOf(0))), Opts{}); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) < 1200*time.Millisecond {
		t.Errorf("waited only %v", time.Since(start))
	}
	attempts := calls(t, "/v1/ocr")
	if !reflect.DeepEqual(statuses(attempts), []int{503, 200}) || attempts[0].IdempotencyKey != attempts[1].IdempotencyKey {
		t.Errorf("attempts %+v", attempts)
	}
}

func TestRetryFloorOneSecond(t *testing.T) {
	c := newClient(t, scenario(t, "rate-limited"), Config{})
	h := uploadPNG(t, c, 0)
	start := time.Now()
	body := map[string]any{"model": "sam-lite", "image": ImageHandle(h), "prompts": []any{map[string]any{"box": []int{1, 1, 20, 20}}}}
	if _, err := c.Post("/v1/segment", body, Opts{}); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) < 2*time.Second {
		t.Errorf("waited only %v", time.Since(start))
	}
	if got := statuses(calls(t, "/v1/segment")); !reflect.DeepEqual(got, []int{429, 429, 200}) {
		t.Errorf("statuses %v", got)
	}
}

func TestNotRetryableRaisesTypedError(t *testing.T) {
	c := newClient(t, scenario(t, "not-retryable"), Config{})
	_, err := c.Post("/v1/ocr", ocr(ImageBase64(pngOf(0))), Opts{})
	e := wantCode(t, err, "bad_model_output")
	if e.Status != 502 || e.Retryable || len(e.RequestID) != 16 || len(calls(t, "/v1/ocr")) != 1 {
		t.Errorf("error %+v", e)
	}
}

func TestIdempotentReplayAndConflict(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{})
	h := uploadPNG(t, c, 0)
	body := map[string]any{"model": "sam3", "image": ImageHandle(h), "prompts": []any{map[string]string{"text": "cat"}}}
	first, err := c.Post("/v1/segment", body, Opts{Key: "same-key"})
	if err != nil {
		t.Fatal(err)
	}
	again, err := c.Post("/v1/segment", body, Opts{Key: "same-key"})
	if err != nil || !bytes.Equal(first, again) {
		t.Fatalf("replay differs: %v", err)
	}
	rows := c.Calls("")
	if rows[0].Replayed || !rows[1].Replayed || rows[1].Charged != 0 || c.Summary().ChargedTotal != 0.55 {
		t.Errorf("rows %+v summary %+v", rows, c.Summary())
	}
	body["prompts"] = []any{map[string]string{"text": "dog"}}
	_, err = c.Post("/v1/segment", body, Opts{Key: "same-key"})
	wantCode(t, err, "idempotency_conflict")
}

func TestReuploadAfterEviction(t *testing.T) {
	c := newClient(t, scenario(t, "image-evicted"), Config{})
	h := uploadPNG(t, c, 0)
	for range 2 {
		if _, err := c.Post("/v1/ocr", ocr(ImageHandle(h)), Opts{}); err != nil {
			t.Fatal(err)
		}
	}
	ocrCalls := calls(t, "/v1/ocr")
	if !reflect.DeepEqual(statuses(ocrCalls), []int{200, 404, 200}) || ocrCalls[1].IdempotencyKey == ocrCalls[2].IdempotencyKey || len(calls(t, "/v1/images")) != 2 {
		t.Errorf("ocr %+v", ocrCalls)
	}
}

func TestBudgetRefusesBeforeSending(t *testing.T) {
	limit := 0.3
	c := newClient(t, scenario(t, ""), Config{MaxCredits: &limit})
	if _, err := c.Post("/v1/ocr", ocr(ImageBase64(pngOf(0))), Opts{}); err != nil {
		t.Fatal(err)
	}
	_, err := c.Post("/v1/ocr", ocr(ImageBase64(pngOf(0))), Opts{})
	e := wantCode(t, err, "budget_exceeded")
	if e.Message != "call refused: up to 0.25 credits would pass FOLDBUN_MAX_CREDITS=0.3 (spent 0.25)" || len(calls(t, "/v1/ocr")) != 1 {
		t.Errorf("message %q", e.Message)
	}
	h := uploadPNG(t, c, 0)
	_, err = c.Post("/v1/segment", map[string]any{"model": "sam3", "image": ImageHandle(h), "prompts": []any{map[string]string{"text": "x"}}}, Opts{})
	wantCode(t, err, "budget_exceeded")
}

func TestEstimateMatchesBillingExamples(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{})
	models, err := c.Models()
	if err != nil {
		t.Fatal(err)
	}
	text, box := map[string]any{"text": "a"}, map[string]any{"box": []int{0, 0, 1, 1}}
	point := func(x int) map[string]any { return map[string]any{"point": map[string]int{"x": x, "y": x}} }
	seg := func(model string, prompts ...any) float64 {
		return Estimate(models, "/v1/segment", map[string]any{"prompts": prompts}, model)
	}
	for _, c := range []struct {
		got, want float64
	}{
		{seg("sam3", text, text, text), 0.65},
		{seg("sam3", text, text, box, box, box), 0.6},
		{seg("sam3", box, box, box, box), 0.55},
		{seg("sam3", text, point(1), point(2)), 0.59},
		{seg("sam-lite", box, box, box, box), 0.24},
		{Estimate(models, "/v1/embeddings", map[string]any{"texts": []string{"a", "b"}}, "siglip2"), 0.02},
		{Estimate(models, "/v1/documents/parse", map[string]any{}, "paddleocr-vl"), 2},
	} {
		if c.got != c.want {
			t.Errorf("estimate %v, want %v", c.got, c.want)
		}
	}
}

func TestPublicTierModelLocked(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{APIKey: "fbs_fake_public"})
	models, err := c.Models()
	if err != nil {
		t.Fatal(err)
	}
	if models["sam3"].Available || models["ppocr-v6"].PerMinute != 10 {
		t.Errorf("models %+v", models)
	}
	h := uploadPNG(t, c, 0)
	_, err = c.Post("/v1/segment", map[string]any{"model": "sam3", "image": ImageHandle(h), "prompts": []any{map[string]string{"text": "cat"}}}, Opts{})
	if e := wantCode(t, err, "model_locked"); e.Status != 403 {
		t.Errorf("status %d", e.Status)
	}
	raw, err := c.Post("/v1/ocr", ocr(ImageHandle(h)), Opts{})
	if err != nil {
		t.Fatal(err)
	}
	var r struct {
		Billing struct {
			Pool      string
			Allowance struct{ Used, Limit int }
		}
	}
	_ = json.Unmarshal(raw, &r)
	if r.Billing.Pool != "public" || r.Billing.Allowance.Used != 1 || r.Billing.Allowance.Limit != 100 {
		t.Errorf("billing %+v", r.Billing)
	}
}

func TestUnauthorized(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{APIKey: "not-a-key"})
	_, err := c.Models()
	if e := wantCode(t, err, "unauthorized"); e.Status != 401 {
		t.Errorf("status %d", e.Status)
	}
}

func batchBody(t *testing.T, c *Client, n int) map[string]any {
	items := []any{}
	for i := range n {
		items = append(items, map[string]any{"image": ImageHandle(uploadPNG(t, c, i)), "prompts": []any{map[string]string{"text": "cat"}}})
	}
	return map[string]any{"model": "sam3", "items": items}
}

func collect(c *Client, body map[string]any, o Opts) ([]Event, error) {
	var events []Event
	err := c.Batch("segment", body, o, func(e Event) error { events = append(events, e); return nil })
	return events, err
}

func TestBatchEventsAndPartialFailure(t *testing.T) {
	c := newClient(t, scenario(t, "batch-partial"), Config{})
	events, err := collect(c, batchBody(t, c, 4), Opts{Tag: "b1"})
	if err != nil {
		t.Fatal(err)
	}
	var names, statusesSeen []string
	var errorCode string
	for _, e := range events {
		names = append(names, e.Name)
		if e.Name == "item" {
			var item struct {
				Status string
				Error  struct{ Code string }
			}
			_ = json.Unmarshal(e.Data, &item)
			statusesSeen = append(statusesSeen, item.Status)
			if item.Status == "error" {
				errorCode = item.Error.Code
			}
		}
	}
	if names[0] != "progress" || names[len(names)-1] != "summary" || len(statusesSeen) != 4 || !reflect.DeepEqual(statusesSeen, []string{"ok", "error", "ok", "expired"}) || errorCode != "bad_model_output" {
		t.Errorf("events %v statuses %v error %q", names, statusesSeen, errorCode)
	}
	var summary struct{ Counts map[string]int }
	_ = json.Unmarshal(events[len(events)-1].Data, &summary)
	if !reflect.DeepEqual(summary.Counts, map[string]int{"ok": 2, "error": 1, "expired": 1}) {
		t.Errorf("counts %v", summary.Counts)
	}
	rows := c.Calls("b1")
	if len(rows) != 2 || rows[0].Endpoint != "/v1/batch/segment" || rows[1].Endpoint != "/v1/batch/segment" {
		t.Errorf("rows %+v", rows)
	}
	if a := calls(t, "/v1/batch/segment")[0].Accept; a == nil || *a != "text/event-stream" {
		t.Errorf("accept %v", a)
	}
}

func TestBatchResumeReplaysFinishedItems(t *testing.T) {
	c := newClient(t, scenario(t, "batch-dropped"), Config{})
	body := batchBody(t, c, 3)
	first, err := collect(c, body, Opts{Key: "batch-key"})
	if err != nil {
		t.Fatal(err)
	}
	if len(first) != 3 || first[0].Name != "progress" || first[1].Name != "item" || first[2].Name != "item" {
		t.Fatalf("first run: %v", first)
	}
	second, err := collect(c, body, Opts{Key: "batch-key"})
	if err != nil {
		t.Fatal(err)
	}
	var replayed []bool
	for _, e := range second {
		if e.Name == "item" {
			var item struct{ Replayed bool }
			_ = json.Unmarshal(e.Data, &item)
			replayed = append(replayed, item.Replayed)
		}
	}
	var ledger []bool
	for _, r := range c.Calls("") {
		ledger = append(ledger, r.Replayed)
	}
	if !reflect.DeepEqual(replayed, []bool{true, true, false}) || !reflect.DeepEqual(ledger, []bool{false, false, true, true, false}) || c.Summary().ChargedTotal != 1.65 {
		t.Errorf("replayed %v ledger %v summary %+v", replayed, ledger, c.Summary())
	}
}

func TestBatchBudget(t *testing.T) {
	limit := 1.0
	c := newClient(t, scenario(t, ""), Config{MaxCredits: &limit})
	_, err := collect(c, batchBody(t, c, 2), Opts{})
	wantCode(t, err, "budget_exceeded")
	if n := len(calls(t, "/v1/batch/segment")); n != 0 {
		t.Errorf("%d batch requests sent", n)
	}
}

func TestBatchStopEarly(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{})
	n := 0
	err := c.Batch("segment", batchBody(t, c, 3), Opts{}, func(Event) error { n++; return ErrStop })
	if err != nil || n != 1 {
		t.Errorf("err %v after %d events", err, n)
	}
}

func TestTokenBucketPaces(t *testing.T) {
	b := newBucket(60)
	for range 60 {
		b.take()
	}
	start := time.Now()
	b.take()
	if d := time.Since(start); d < 800*time.Millisecond || d > 1500*time.Millisecond {
		t.Errorf("waited %v", d)
	}
}

func TestFormatG(t *testing.T) {
	for in, want := range map[float64]string{0.25: "0.25", 0.1: "0.1", 100: "100", 0: "0", 1e7: "1e+07", 123456: "123456", 2.5e-7: "2.5e-07", 1.5: "1.5"} {
		if got := g(in); got != want {
			t.Errorf("g(%v) = %q, want %q", in, got, want)
		}
	}
}
