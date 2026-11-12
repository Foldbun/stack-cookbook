package main

// The backend against a stand-in for the Stack client (no network, no key): go test ./...

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"stack-cookbook/02-click-to-segment/stackclient"
)

const fakeHandle = "img_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

type fakeRow struct {
	tag string
	row stackclient.Row
}

// fakeStack answers like Stack for an image of 100 x 80 pixels and records what it was asked.
type fakeStack struct {
	mu      sync.Mutex
	uploads int
	bodies  []string // JSON of every body sent to /v1/refine
	fail    error
	rows    []fakeRow
}

func (f *fakeStack) Upload(data []byte, contentType string) (stackclient.ImageInfo, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.uploads++
	return stackclient.ImageInfo{ID: fakeHandle, Width: 100, Height: 80, Bytes: len(data), ContentType: contentType}, nil
}

func (f *fakeStack) Post(path string, body any, o stackclient.Opts) (json.RawMessage, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail != nil {
		return nil, f.fail
	}
	data, _ := json.Marshal(body)
	f.bodies = append(f.bodies, string(data))
	cache, list := "miss", 0.21
	if len(f.bodies) > 1 {
		cache, list = "hit", 0.01
	}
	model := "sam-lite"
	f.rows = append(f.rows, fakeRow{o.Tag, stackclient.Row{Endpoint: path, Model: &model, List: list, Cache: &cache, QueueMs: 4, ProcessingMs: 60}})
	return json.RawMessage(fmt.Sprintf(`{"model":"sam-lite","cost":0,"cache":%q,"billing":{"total":%g},
		"instances":[{"score":0.9,"bbox":[10,10,51,41],"area":1271,"polygon":[{"outer":[10,10,50,10,50,40,10,40]}]}]}`, cache, list)), nil
}

func (f *fakeStack) Calls(tag string) []stackclient.Row {
	f.mu.Lock()
	defer f.mu.Unlock()
	rows := []stackclient.Row{}
	for _, r := range f.rows {
		if tag == "" || r.tag == tag {
			rows = append(rows, r.row)
		}
	}
	return rows
}

func (f *fakeStack) sent() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.bodies)
}

// rig is a backend behind httptest, with a clock the test moves.
type rig struct {
	t      *testing.T
	stack  *fakeStack
	server *httptest.Server
	clock  atomic.Int64 // milliseconds
}

var epoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func newRig(t *testing.T, opts Options) *rig {
	t.Helper()
	r := &rig{t: t, stack: &fakeStack{}}
	opts.Now = func() time.Time { return epoch.Add(time.Duration(r.clock.Load()) * time.Millisecond) }
	r.server = httptest.NewServer(NewBackend(r.stack, opts))
	t.Cleanup(r.server.Close)
	return r
}

type reply struct {
	status int
	body   map[string]any
	header http.Header
	raw    []byte
}

func (r *rig) do(method, path, session, contentType string, body io.Reader) reply {
	r.t.Helper()
	req, err := http.NewRequest(method, r.server.URL+path, body)
	if err != nil {
		r.t.Fatal(err)
	}
	if session != "" {
		req.Header.Set("X-Session-Id", session)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		r.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	out := reply{status: res.StatusCode, header: res.Header, raw: raw}
	json.Unmarshal(raw, &out.body)
	return out
}

func (r *rig) get(path, session string) reply { return r.do("GET", path, session, "", nil) }

func (r *rig) upload(session string, salt byte) reply {
	return r.do("POST", "/api/images", session, "image/jpeg", bytes.NewReader([]byte{0xff, 0xd8, 0xff, salt}))
}

func (r *rig) refine(session string, body any) reply {
	data, _ := json.Marshal(body)
	return r.do("POST", "/api/refine", session, "application/json", bytes.NewReader(data))
}

func code(rep reply) string {
	e, _ := rep.body["error"].(map[string]any)
	c, _ := e["code"].(string)
	return c
}

type obj = map[string]any

func TestHealthNeedsNoSession(t *testing.T) {
	r := newRig(t, Options{})
	rep := r.get("/api/health", "")
	want := obj{"status": "ok", "language": "go", "client": "stackClient " + stackclient.Version}
	if rep.status != 200 || !reflect.DeepEqual(rep.body, want) {
		t.Fatalf("got %d %v", rep.status, rep.body)
	}
	if rep := r.do("POST", "/api/health", "", "", nil); rep.status != 405 || code(rep) != "method_not_allowed" {
		t.Fatalf("POST health: %d %v", rep.status, rep.body)
	}
}

func TestRoutesNeedAWellFormedSession(t *testing.T) {
	r := newRig(t, Options{})
	long := strings.Repeat("a", 65)
	for _, session := range []string{"", "bad id!", "ü", long} {
		if rep := r.get("/api/usage", session); rep.status != 400 || code(rep) != "invalid_session" {
			t.Errorf("session %q: %d %v", session, rep.status, rep.body)
		}
	}
	if rep := r.get("/api/usage", strings.Repeat("a", 64)); rep.status != 200 {
		t.Errorf("64 characters: %d", rep.status)
	}
	cases := []struct {
		method, path string
		status       int
		code         string
	}{
		{"GET", "/api/nothing", 404, "not_found"},
		{"GET", "/api", 404, "not_found"},
		{"GET", "/api/refine", 405, "method_not_allowed"},
		{"POST", "/api/usage", 405, "method_not_allowed"},
		{"DELETE", "/api/images", 405, "method_not_allowed"},
	}
	for _, c := range cases {
		rep := r.do(c.method, c.path, "s", "", nil)
		if rep.status != c.status || code(rep) != c.code {
			t.Errorf("%s %s: %d %v", c.method, c.path, rep.status, rep.body)
		}
	}
}

func TestImageIDIsDerivedFromTheBytes(t *testing.T) {
	r := newRig(t, Options{})
	data := []byte{0xff, 0xd8, 0xff, 7}
	sum := sha256.Sum256(data)
	want := "im_" + hex.EncodeToString(sum[:])[:32]
	rep := r.upload("id", 7)
	if rep.body["imageId"] != want {
		t.Fatalf("got %v want %s", rep.body["imageId"], want)
	}
	// known vector: sha256("abc") starts with ba7816bf8f01cfea414140de5dae2223
	abc := r.do("POST", "/api/images", "id", "image/png", strings.NewReader("abc"))
	if abc.body["imageId"] != "im_ba7816bf8f01cfea414140de5dae2223" {
		t.Fatalf("got %v", abc.body["imageId"])
	}
}

func TestAnImageIsUploadedToStackOncePerContent(t *testing.T) {
	r := newRig(t, Options{})
	a, b := r.upload("up-1", 1), r.upload("up-2", 1)
	if a.status != 200 || b.status != 200 || !reflect.DeepEqual(a.body, b.body) {
		t.Fatalf("%v %v", a, b)
	}
	if a.body["width"] != 100.0 || a.body["height"] != 80.0 {
		t.Fatalf("size %v", a.body)
	}
	if r.stack.uploads != 1 {
		t.Fatalf("uploads %d", r.stack.uploads)
	}
	r.upload("up-1", 2)
	if r.stack.uploads != 2 {
		t.Fatalf("uploads %d", r.stack.uploads)
	}
	if rep := r.do("POST", "/api/images", "up-1", "text/plain", strings.NewReader("x")); rep.status != 415 || code(rep) != "unsupported_media_type" {
		t.Fatalf("%d %v", rep.status, rep.body)
	}
	if rep := r.do("POST", "/api/images", "up-1", "image/webp; charset=binary", strings.NewReader("x")); rep.status != 200 {
		t.Fatalf("parameters of the media type: %d %v", rep.status, rep.body)
	}
	if u := r.get("/api/usage", "up-1").body["uploads"]; u != 3.0 {
		t.Fatalf("uploads of up-1: %v", u)
	}
}

func TestConcurrentUploadsOfOneContentShareOneStackUpload(t *testing.T) {
	r := newRig(t, Options{})
	var wg sync.WaitGroup
	for i := range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if rep := r.upload(fmt.Sprintf("conc-%d", i), 9); rep.status != 200 {
				t.Errorf("%d", rep.status)
			}
		}()
	}
	wg.Wait()
	if r.stack.uploads != 1 {
		t.Fatalf("uploads %d", r.stack.uploads)
	}
}

func TestTheImageCacheKeepsAtMostTheLastUsed256(t *testing.T) {
	r := newRig(t, Options{})
	first := ""
	for i := range maxImages + 1 {
		data := []byte(fmt.Sprintf("image %d", i))
		rep := r.do("POST", "/api/images", fmt.Sprintf("lru-%d", i), "image/png", bytes.NewReader(data))
		if i == 0 {
			first = rep.body["imageId"].(string)
		}
	}
	rep := r.refine("lru-0", obj{"imageId": first, "points": []obj{{"x": 1, "y": 1}}})
	if rep.status != 404 || code(rep) != "image_not_found" {
		t.Fatalf("%d %v", rep.status, rep.body)
	}
}

func TestRefineSendsAllPointsAndReturnsPolygonCostAndUsage(t *testing.T) {
	r := newRig(t, Options{})
	img := r.upload("click", 2).body["imageId"]
	points := []obj{{"x": 20, "y": 20}, {"x": 70.5, "y": 60, "positive": false}}
	rep := r.refine("click", obj{"imageId": img, "points": points})
	if rep.status != 200 {
		t.Fatalf("%d %v", rep.status, rep.body)
	}
	want := `{"model":"sam-lite","image":{"id":"` + fakeHandle + `"},"points":[{"x":20,"y":20,"positive":true},{"x":70.5,"y":60,"positive":false}]}`
	if r.stack.bodies[0] != want {
		t.Fatalf("body sent to Stack:\n got %s\nwant %s", r.stack.bodies[0], want)
	}
	poly, _ := json.Marshal(rep.body["polygon"])
	if string(poly) != `[{"outer":[10,10,50,10,50,40,10,40]}]` {
		t.Fatalf("polygon %s", poly)
	}
	if rep.body["cache"] != "miss" || rep.body["listCredits"] != 0.21 || rep.body["area"] != 1271.0 || rep.body["model"] != "sam-lite" {
		t.Fatalf("%v", rep.body)
	}
	if l, _ := rep.body["latencyMs"].(float64); l != float64(int(l)) {
		t.Fatalf("latencyMs %v", rep.body["latencyMs"])
	}
	// a box without points: points is sent as [], box is passed through
	if rep := r.refine("click", obj{"imageId": img, "points": []obj{}, "box": []float64{1, 2, 30, 40}}); rep.status != 200 {
		t.Fatalf("%d %v", rep.status, rep.body)
	}
	if got := r.stack.bodies[1]; got != `{"model":"sam-lite","image":{"id":"`+fakeHandle+`"},"points":[],"box":[1,2,30,40]}` {
		t.Fatalf("box body %s", got)
	}
	u := r.get("/api/usage", "click").body
	if u["clicks"] != 2.0 || u["uploads"] != 1.0 || u["listCredits"] != 0.22 || u["cacheHits"] != 1.0 || u["cacheMisses"] != 1.0 || u["sessionId"] != "click" {
		t.Fatalf("%v", u)
	}
	if n := len(u["calls"].([]any)); n != 2 {
		t.Fatalf("calls %d", n)
	}
	fresh := r.get("/api/usage", "fresh").body
	if n := len(fresh["calls"].([]any)); n != 0 || fresh["clicks"] != 0.0 {
		t.Fatalf("a new session is empty: %v", fresh)
	}
}

func TestRefineChecksTheRequestBeforeCallingStack(t *testing.T) {
	r := newRig(t, Options{})
	img := r.upload("check", 3).body["imageId"]
	one := []obj{{"x": 1, "y": 1}}
	many := make([]obj, 21)
	for i := range many {
		many[i] = obj{"x": 1, "y": 1}
	}
	cases := []struct {
		name   string
		body   any
		status int
		code   string
	}{
		{"unknown image", obj{"imageId": "im_" + strings.Repeat("0", 32), "points": one}, 404, "image_not_found"},
		{"malformed image id", obj{"imageId": "x", "points": one}, 400, "invalid_request"},
		{"no positive point", obj{"imageId": img, "points": []obj{{"x": 1, "y": 1, "positive": false}}}, 400, "invalid_request"},
		{"no point and no box", obj{"imageId": img, "points": []obj{}}, 400, "invalid_request"},
		{"points missing", obj{"imageId": img}, 400, "invalid_request"},
		{"point outside right", obj{"imageId": img, "points": []obj{{"x": 101, "y": 1}}}, 400, "invalid_request"},
		{"point outside top", obj{"imageId": img, "points": []obj{{"x": 1, "y": -1}}}, 400, "invalid_request"},
		{"too many points", obj{"imageId": img, "points": many}, 400, "invalid_request"},
		{"point with a string", obj{"imageId": img, "points": []obj{{"x": "1", "y": 1}}}, 400, "invalid_request"},
		{"point with a non-boolean positive", obj{"imageId": img, "points": []obj{{"x": 1, "y": 1, "positive": 1}}}, 400, "invalid_request"},
		{"point with an extra key", obj{"imageId": img, "points": []obj{{"x": 1, "y": 1, "z": 1}}}, 400, "invalid_request"},
		{"inverted box", obj{"imageId": img, "points": []obj{}, "box": []float64{10, 10, 5, 20}}, 400, "invalid_request"},
		{"box outside", obj{"imageId": img, "points": []obj{}, "box": []float64{0, 0, 101, 20}}, 400, "invalid_request"},
		{"box of three", obj{"imageId": img, "points": []obj{}, "box": []float64{0, 0, 20}}, 400, "invalid_request"},
		{"model not allowed", obj{"imageId": img, "points": one, "model": "sam3"}, 400, "model_not_allowed"},
		{"model not a string", obj{"imageId": img, "points": one, "model": 3}, 400, "model_not_allowed"},
		{"extra field", obj{"imageId": img, "points": one, "extra": 1}, 400, "invalid_request"},
		{"not an object", []int{1}, 400, "invalid_request"},
	}
	for i, c := range cases { // a session of its own for each: refusals count against the limit too
		rep := r.refine(fmt.Sprintf("check-%d", i), c.body)
		if rep.status != c.status || code(rep) != c.code {
			t.Errorf("%s: %d %v", c.name, rep.status, rep.body)
		}
	}
	if rep := r.do("POST", "/api/refine", "check-json", "application/json", strings.NewReader("{nope")); rep.status != 400 || code(rep) != "invalid_request" {
		t.Errorf("not JSON: %d %v", rep.status, rep.body)
	}
	if n := r.stack.sent(); n != 0 {
		t.Fatalf("%d calls reached Stack", n)
	}
	// edges that are allowed: a point on the far border, the whole image as a box, an allowed model
	for _, body := range []obj{
		{"imageId": img, "points": []obj{{"x": 100, "y": 80}}},
		{"imageId": img, "points": []obj{}, "box": []float64{0, 0, 100, 80}},
	} {
		if rep := r.refine("check-edge", body); rep.status != 200 {
			t.Errorf("%v: %d %v", body, rep.status, rep.body)
		}
	}
}

func TestModelsComeFromTheAllowList(t *testing.T) {
	r := newRig(t, Options{Models: []string{"sam3", "sam-lite"}})
	img := r.upload("models", 4).body["imageId"]
	one := []obj{{"x": 1, "y": 1}}
	if rep := r.refine("models", obj{"imageId": img, "points": one}); rep.status != 200 || !strings.HasPrefix(r.stack.bodies[0], `{"model":"sam3"`) {
		t.Fatalf("the first model is the default: %d %s", rep.status, r.stack.bodies)
	}
	if rep := r.refine("models", obj{"imageId": img, "points": one, "model": "sam-lite"}); rep.status != 200 {
		t.Fatalf("%d", rep.status)
	}
	if rep := r.refine("models", obj{"imageId": img, "points": one, "model": "other"}); code(rep) != "model_not_allowed" {
		t.Fatalf("%v", rep.body)
	}
}

func TestBodyLimits(t *testing.T) {
	r := newRig(t, Options{})
	img := r.upload("limits", 5).body["imageId"]
	big := `{"imageId":"` + img.(string) + `","points":[{"x":1,"y":1}],"pad":"` + strings.Repeat("x", maxRefineBytes) + `"}`
	if rep := r.do("POST", "/api/refine", "limits", "application/json", strings.NewReader(big)); rep.status != 413 || code(rep) != "payload_too_large" {
		t.Errorf("refine body: %d %v", rep.status, rep.body)
	}
	// chunked, so that the length is not known in advance
	req, _ := http.NewRequest("POST", r.server.URL+"/api/refine", io.NopCloser(strings.NewReader(big)))
	req.Header.Set("X-Session-Id", "limits")
	res, err := http.DefaultClient.Do(req)
	if err != nil || res.StatusCode != 413 {
		t.Errorf("chunked: %v %v", res, err)
	}
	if res != nil {
		res.Body.Close()
	}
	huge := bytes.NewReader(make([]byte, maxImageBytes+1))
	if rep := r.do("POST", "/api/images", "limits", "image/png", huge); rep.status != 413 || code(rep) != "payload_too_large" {
		t.Errorf("image body: %d %v", rep.status, rep.body)
	}
	exact := bytes.NewReader(make([]byte, maxImageBytes))
	if rep := r.do("POST", "/api/images", "limits-2", "image/png", exact); rep.status != 200 {
		t.Errorf("20 MiB exactly: %d %v", rep.status, rep.body)
	}
}

func TestARejectedRequestConsumesARateLimitToken(t *testing.T) {
	r := newRig(t, Options{})
	img := r.upload("tokens-img", 6).body["imageId"].(string)
	bad := func(body string) reply {
		return r.do("POST", "/api/refine", "tokens", "application/json", strings.NewReader(body))
	}
	var statuses []int
	for range 8 {
		statuses = append(statuses, bad("not json").status)
	}
	statuses = append(statuses, bad(strings.Repeat("x", maxRefineBytes+1)).status, bad(`{"imageId":"`+img+`","points":[]}`).status)
	if !reflect.DeepEqual(statuses, []int{400, 400, 400, 400, 400, 400, 400, 400, 413, 400}) {
		t.Fatalf("%v", statuses)
	}
	if rep := bad("not json"); code(rep) != "rate_limited" {
		t.Errorf("refine burst not used up: %d %v", rep.status, rep.body)
	}
	wrong := func(ctype string, n int) reply {
		return r.do("POST", "/api/images", "tokens-up", ctype, bytes.NewReader(make([]byte, n)))
	}
	got := []int{wrong("text/plain", 1).status, wrong("image/png", 0).status, wrong("image/png", maxImageBytes+1).status, wrong("text/plain", 1).status, wrong("text/plain", 1).status}
	if !reflect.DeepEqual(got, []int{415, 400, 413, 415, 415}) {
		t.Fatalf("%v", got)
	}
	if rep := wrong("text/plain", 1); code(rep) != "rate_limited" {
		t.Errorf("upload burst not used up: %d %v", rep.status, rep.body)
	}
}

func TestAnEmptyImageBodyIsRejectedAndNotSentToStack(t *testing.T) {
	r := newRig(t, Options{})
	rep := r.do("POST", "/api/images", "empty", "image/jpeg", bytes.NewReader(nil))
	if rep.status != 400 || code(rep) != "invalid_request" || r.stack.uploads != 0 {
		t.Errorf("%d %v uploads=%d", rep.status, rep.body, r.stack.uploads)
	}
}

func TestModelNullMeansNotGiven(t *testing.T) {
	r := newRig(t, Options{})
	img := r.upload("null-model", 7).body["imageId"]
	rep := r.refine("null-model", obj{"imageId": img, "points": []obj{{"x": 5, "y": 5}}, "model": nil})
	if rep.status != 200 || !strings.Contains(r.stack.bodies[len(r.stack.bodies)-1], `"model":"sam-lite"`) {
		t.Errorf("%d %v %v", rep.status, rep.body, r.stack.bodies)
	}
}

func TestAnUnexpectedErrorIs500InternalWithNoDetail(t *testing.T) {
	r := newRig(t, Options{})
	img := r.upload("internal", 8).body["imageId"]
	r.stack.fail = errors.New("secret detail")
	rep := r.refine("internal", obj{"imageId": img, "points": []obj{{"x": 5, "y": 5}}})
	want := obj{"error": obj{"code": "internal", "message": "internal error", "retryable": false, "retryAfterMs": 0.0}}
	if rep.status != 500 || !reflect.DeepEqual(rep.body, want) {
		t.Errorf("%d %v", rep.status, rep.body)
	}
}

func TestASessionIsLimitedToABurstOf10RefinesWithRetryAfter(t *testing.T) {
	r := newRig(t, Options{})
	img := r.upload("burst", 6).body["imageId"]
	body := obj{"imageId": img, "points": []obj{{"x": 5, "y": 5}}}
	for i := range 10 {
		if rep := r.refine("burst", body); rep.status != 200 {
			t.Fatalf("refine %d: %d", i, rep.status)
		}
	}
	rep := r.refine("burst", body)
	e, _ := rep.body["error"].(obj)
	if rep.status != 429 || code(rep) != "rate_limited" || e["retryAfterMs"] != 1000.0 || e["retryable"] != true || rep.header.Get("Retry-After") != "1" {
		t.Fatalf("%d %v %v", rep.status, rep.body, rep.header)
	}
	r.clock.Add(1000)
	if rep := r.refine("burst", body); rep.status != 200 {
		t.Fatalf("after a second: %d", rep.status)
	}
}

func TestUploadsAreLimitedToABurstOf5(t *testing.T) {
	r := newRig(t, Options{})
	for i := range 5 {
		if rep := r.upload("ups", byte(i)); rep.status != 200 {
			t.Fatalf("upload %d: %d", i, rep.status)
		}
	}
	rep := r.upload("ups", 5)
	if rep.status != 429 || rep.header.Get("Retry-After") != "6" {
		t.Fatalf("%d %v", rep.status, rep.header)
	}
}

func TestASessionEndsAfter30MinutesWithoutARequest(t *testing.T) {
	r := newRig(t, Options{})
	r.upload("idle", 1)
	r.clock.Add(29 * 60_000)
	if u := r.get("/api/usage", "idle").body["uploads"]; u != 1.0 {
		t.Fatalf("still alive: %v", u)
	}
	r.clock.Add(30*60_000 + 1)
	if u := r.get("/api/usage", "idle").body["uploads"]; u != 0.0 {
		t.Fatalf("expired: %v", u)
	}
}

func TestTheSessionTableIsCappedAt1000(t *testing.T) {
	b := NewBackend(&fakeStack{}, Options{Now: func() time.Time { return epoch }})
	for i := range 1001 {
		if _, err := b.session(fmt.Sprintf("s%d", i)); err != nil {
			t.Fatal(err)
		}
	}
	if len(b.sessions) != maxSessions || b.sessions["s0"] != nil || b.sessions["s1000"] == nil {
		t.Fatalf("%d sessions, s0 %v", len(b.sessions), b.sessions["s0"])
	}
}

func TestStackErrorsMapToTheBackendsCodes(t *testing.T) {
	r := newRig(t, Options{})
	img := r.upload("errors", 5).body["imageId"]
	body := obj{"imageId": img, "points": []obj{{"x": 5, "y": 5}}}
	r.stack.fail = &stackclient.Error{Status: 403, Code: "model_locked", Message: "model sam-lite is not available to this account (balance 12)", RequestID: "req_1"}
	rep := r.refine("errors", body)
	want := obj{"code": "upstream_refused", "message": "Foldbun Stack refused the call", "retryable": false, "retryAfterMs": 0.0, "upstreamCode": "model_locked", "requestId": "req_1"}
	if rep.status != 502 || !reflect.DeepEqual(rep.body["error"], want) {
		t.Fatalf("%d %v", rep.status, rep.body)
	}
	if strings.Contains(string(rep.raw), "balance") {
		t.Fatal("Stack's message was forwarded")
	}
	r.stack.fail = &stackclient.Error{Status: 429, Code: "rate_limited", Message: "slow", Retryable: true, RetryAfterMs: 2500}
	rep = r.refine("errors", body)
	if rep.status != 503 || code(rep) != "upstream_busy" || rep.header.Get("Retry-After") != "3" || rep.body["error"].(obj)["retryAfterMs"] != 2500.0 {
		t.Fatalf("%d %v %v", rep.status, rep.body, rep.header)
	}
	r.stack.fail = nil
	if r.get("/api/usage", "errors").body["clicks"] != 0.0 {
		t.Fatal("a failed call is not a click")
	}
}

func TestFromStack(t *testing.T) {
	cases := []struct {
		name         string
		err          error
		status       int
		code         string
		retryable    bool
		retryAfterMs int
		upstream     string
	}{
		{"timeout", &stackclient.Error{Status: 504, Code: "upstream_timeout", Retryable: true}, 503, "upstream_busy", true, 1000, "upstream_timeout"},
		{"busy with a long wait", &stackclient.Error{Status: 503, Code: "overloaded", Retryable: true, RetryAfterMs: 4000}, 503, "upstream_busy", true, 4000, "overloaded"},
		{"network", &stackclient.Error{Code: "network_error", Retryable: true}, 503, "upstream_busy", true, 1000, "network_error"},
		{"not an API error", errors.New("boom"), 500, "internal", false, 0, ""},
		{"cap", &stackclient.Error{Code: "budget_exceeded"}, 503, "credit_cap", false, 0, ""},
		{"balance", &stackclient.Error{Status: 402, Code: "insufficient_balance"}, 502, "upstream_refused", false, 0, "insufficient_balance"},
		{"daily limit", &stackclient.Error{Status: 429, Code: "daily_limit_reached"}, 502, "upstream_refused", false, 0, "daily_limit_reached"},
		{"key", &stackclient.Error{Status: 401, Code: "unauthorized"}, 502, "upstream_refused", false, 0, "unauthorized"},
	}
	for _, c := range cases {
		e := fromStack(c.err)
		if e.Status != c.status || e.Code != c.code || e.Retryable != c.retryable || e.RetryAfterMs != c.retryAfterMs || e.UpstreamCode != c.upstream {
			t.Errorf("%s: %+v", c.name, e)
		}
	}
}

func TestRateLimitRefillsAtPerMinute(t *testing.T) {
	bucket := newRateLimit(60, 2, epoch)
	at := func(ms int) time.Time { return epoch.Add(time.Duration(ms) * time.Millisecond) }
	for i, c := range []struct{ ms, wait int }{{0, 0}, {0, 0}, {0, 1000}, {500, 500}, {1000, 0}, {1000, 1000}} {
		if got := bucket.take(at(c.ms)); got != c.wait {
			t.Errorf("step %d: take(%d) = %d, want %d", i, c.ms, got, c.wait)
		}
	}
}

func TestStaticFiles(t *testing.T) {
	dir := t.TempDir()
	files := map[string]string{
		"index.html": "<!doctype html><title>page</title>",
		"app.js":     "console.log(1)",
		"style.css":  "body{}",
		"logo.svg":   "<svg/>",
		"a.png":      "png",
		"b.jpg":      "jpg",
		"c.webp":     "webp",
		"notes.txt":  "text",
		"m.mjs":      "mjs",
		"d.json":     "{}",
		"e.jpeg":     "jpeg",
		"f.ico":      "ico",
		".secret":    "hidden",
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	os.Mkdir(filepath.Join(dir, "sub"), 0o755)
	os.WriteFile(filepath.Join(dir, "sub", "inner.js"), []byte("inner"), 0o644)
	outside := filepath.Join(t.TempDir(), "outside.txt")
	os.WriteFile(outside, []byte("outside"), 0o644)
	os.Symlink(outside, filepath.Join(dir, "link.txt"))
	r := newRig(t, Options{StaticDir: dir})

	for _, c := range []struct{ path, ctype, body string }{
		{"/", "text/html; charset=utf-8", files["index.html"]},
		{"/index.html", "text/html; charset=utf-8", files["index.html"]},
		{"/app.js", "text/javascript; charset=utf-8", files["app.js"]},
		{"/style.css", "text/css; charset=utf-8", files["style.css"]},
		{"/logo.svg", "image/svg+xml", files["logo.svg"]},
		{"/a.png", "image/png", "png"},
		{"/b.jpg", "image/jpeg", "jpg"},
		{"/c.webp", "image/webp", "webp"},
		{"/notes.txt", "application/octet-stream", "text"},
		{"/m.mjs", "text/javascript; charset=utf-8", "mjs"},
		{"/d.json", "application/json", "{}"},
		{"/e.jpeg", "image/jpeg", "jpeg"},
		{"/f.ico", "image/x-icon", "ico"},
		{"/app.js?v=3", "text/javascript; charset=utf-8", files["app.js"]},
	} {
		rep := r.get(c.path, "")
		if rep.status != 200 || rep.header.Get("Content-Type") != c.ctype || string(rep.raw) != c.body {
			t.Errorf("%s: %d %q %q", c.path, rep.status, rep.header.Get("Content-Type"), rep.raw)
		}
		if rep.header.Get("Cache-Control") != "no-cache" {
			t.Errorf("%s: Cache-Control %q", c.path, rep.header.Get("Cache-Control"))
		}
		if rep.header.Get("X-Content-Type-Options") != "nosniff" {
			t.Errorf("%s: X-Content-Type-Options %q", c.path, rep.header.Get("X-Content-Type-Options"))
		}
		if rep.header.Get("Access-Control-Allow-Origin") != "" {
			t.Errorf("%s: CORS header", c.path)
		}
	}
	if rep := r.do("HEAD", "/app.js", "", "", nil); rep.status != 200 || len(rep.raw) != 0 || rep.header.Get("Cache-Control") != "no-cache" || rep.header.Get("X-Content-Type-Options") != "nosniff" {
		t.Errorf("HEAD: %d %q %v", rep.status, rep.raw, rep.header)
	}
	if rep := r.do("HEAD", "/missing.js", "", "", nil); rep.status != 404 {
		t.Errorf("HEAD missing: %d", rep.status)
	}
	// any other method outside /api is 405 with Allow, whether or not the name exists
	for _, path := range []string{"/", "/app.js", "/missing.js", "/sub", "/.secret"} {
		for _, method := range []string{"POST", "PUT", "DELETE", "PATCH", "OPTIONS"} {
			if rep := r.do(method, path, "", "", nil); rep.status != 405 || code(rep) != "method_not_allowed" || rep.header.Get("Allow") != "GET, HEAD" {
				t.Errorf("%s %s: %d %v %q", method, path, rep.status, rep.body, rep.header.Get("Allow"))
			}
		}
	}

	// everything that must not come from a file: 404 with the contract's error shape
	for _, path := range []string{
		"/missing.js", "/sub", "/sub/", "/sub/inner.js", "/.secret", "/link.txt",
		"/../outside.txt", "/..", "/%2e%2e/outside.txt", "/%2e%2e%2foutside.txt", "/..%2foutside.txt", "/sub%2finner.js",
		"/a%00.png", "/%5c..%5coutside.txt", "//index.html", "/index.html/", "/api/index.html", "/api/nothing",
	} {
		rep := r.get(path, "")
		if rep.status != 404 || code(rep) != "not_found" || rep.header.Get("Content-Type") != "application/json" {
			t.Errorf("%s: %d %q", path, rep.status, rep.raw)
		}
	}
	// the raw path with dot segments, which a client library would normalise away
	conn, err := httpRaw(r.server.URL, "GET /../outside.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
	if err != nil || !strings.Contains(conn, "404") || strings.Contains(conn, "outside\n") {
		t.Errorf("raw traversal: %v %q", err, conn)
	}
}

func TestStaticFilesAreNotServedWhenTheDirectoryIsMissing(t *testing.T) {
	for _, dir := range []string{"", filepath.Join(t.TempDir(), "nope")} {
		r := newRig(t, Options{StaticDir: dir})
		for _, path := range []string{"/", "/index.html", "/app.js"} {
			if rep := r.get(path, ""); rep.status != 404 || code(rep) != "not_found" {
				t.Errorf("dir %q %s: %d %q", dir, path, rep.status, rep.raw)
			}
		}
		if rep := r.do("POST", "/", "", "", nil); rep.status != 405 || code(rep) != "method_not_allowed" || rep.header.Get("Allow") != "GET, HEAD" {
			t.Errorf("dir %q POST: %d %q", dir, rep.status, rep.raw)
		}
		if rep := r.get("/api/health", ""); rep.status != 200 {
			t.Errorf("health: %d", rep.status)
		}
	}
}

func TestAPIPathsAreNeverServedFromFiles(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "index.html"), []byte("page"), 0o644)
	os.Mkdir(filepath.Join(dir, "api"), 0o755)
	os.WriteFile(filepath.Join(dir, "api", "health"), []byte("fake"), 0o644)
	r := newRig(t, Options{StaticDir: dir})
	if rep := r.get("/api/health", ""); rep.body["status"] != "ok" {
		t.Fatalf("%s", rep.raw)
	}
	if rep := r.get("/api/usage", ""); code(rep) != "invalid_session" {
		t.Fatalf("%s", rep.raw)
	}
}

// httpRaw sends a request line as it is, without the normalisation of an HTTP client, and returns the answer.
func httpRaw(url, request string) (string, error) {
	srv := strings.TrimPrefix(url, "http://")
	c, err := net.Dial("tcp", srv)
	if err != nil {
		return "", err
	}
	defer c.Close()
	c.Write([]byte(request))
	out, _ := io.ReadAll(c)
	return string(out), nil
}
