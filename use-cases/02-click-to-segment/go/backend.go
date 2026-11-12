package main

// Key-holding backend of the click-to-segment component. The HTTP contract is contract/02-backend.yaml; the browser
// talks only to this backend, and only this backend talks to the Foldbun Stack API.

import (
	"container/list"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"stack-cookbook/02-click-to-segment/stackclient"
)

// Language is reported by GET /api/health.
const Language = "go"

const (
	maxImageBytes  = 20 << 20 // the Stack upload limit
	maxRefineBytes = 64 << 10
	maxPoints      = 20 // the Stack limit of POST /v1/refine
	maxImages      = 256
	maxSessions    = 1000
	idleTime       = 30 * time.Minute
	maxDrain       = 64 << 20 // an oversized body is read on, up to this, so that the answer can still be sent
)

var (
	sessionIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)
	imageIDPattern   = regexp.MustCompile(`^im_[0-9a-f]{32}$`)
	imageTypes       = map[string]bool{"image/jpeg": true, "image/png": true, "image/webp": true}
)

// Stack is the part of the Stack client the backend uses (the tests pass a stand-in).
type Stack interface {
	Upload(data []byte, contentType string) (stackclient.ImageInfo, error)
	Post(path string, body any, o stackclient.Opts) (json.RawMessage, error)
	Calls(tag string) []stackclient.Row
}

// Options of a Backend. The zero value serves sam-lite, uses the wall clock, logs nowhere and serves no files.
type Options struct {
	Models    []string         // models a request may name (SEGMENTER_MODELS); the first is the default
	Now       func() time.Time // replaceable in tests
	Log       func(line string)
	StaticDir string // directory with the page's files; empty: no static files
}

// region errors

// apiError is sent as {"error": {code, message, retryable, retryAfterMs, upstreamCode?, requestId?}}.
type apiError struct {
	Status       int    `json:"-"`
	Allow        string `json:"-"` // Allow header of a 405
	cause        error  // what went wrong with a 500, for the log only
	Code         string `json:"code"`
	Message      string `json:"message"`
	Retryable    bool   `json:"retryable"`
	RetryAfterMs int    `json:"retryAfterMs"`
	UpstreamCode string `json:"upstreamCode,omitempty"`
	RequestID    string `json:"requestId,omitempty"`
}

func (e *apiError) Error() string { return e.Code + ": " + e.Message }

func newError(status int, code, message string) *apiError {
	return &apiError{Status: status, Code: code, Message: message}
}

// fromStack maps an error of the client file (after its own retries) to the backend's error.
// Stack's message is not forwarded.
func fromStack(err error) *apiError {
	var se *stackclient.Error
	switch {
	case !errors.As(err, &se): // not an answer or a network failure: a bug, no detail for the caller
		e := newError(500, "internal", "internal error")
		e.cause = err
		return e
	case se.Code == "budget_exceeded":
		return newError(503, "credit_cap", "the backend's credit cap is reached")
	case !se.Retryable:
		return &apiError{Status: 502, Code: "upstream_refused", Message: "Foldbun Stack refused the call", UpstreamCode: se.Code, RequestID: se.RequestID}
	}
	return &apiError{Status: 503, Code: "upstream_busy", Message: "Foldbun Stack is busy; try again", Retryable: true,
		RetryAfterMs: max(1000, se.RetryAfterMs), UpstreamCode: se.Code, RequestID: se.RequestID}
}

// endregion

// region session-limit

// rateLimit is a token bucket: perMinute sustained, burst at once. take returns 0 when allowed, else the wait in ms.
type rateLimit struct {
	tokens, perMinute, burst float64
	at                       time.Time
}

func newRateLimit(perMinute, burst int, now time.Time) *rateLimit {
	return &rateLimit{tokens: float64(burst), perMinute: float64(perMinute), burst: float64(burst), at: now}
}

func (l *rateLimit) take(now time.Time) int {
	elapsedMs := float64(now.Sub(l.at).Milliseconds())
	l.tokens = math.Min(l.burst, l.tokens+elapsedMs*l.perMinute/60_000)
	l.at = now
	if l.tokens >= 1 {
		l.tokens--
		return 0
	}
	return int(math.Ceil((1 - l.tokens) * 60_000 / l.perMinute))
}

// session is an accounting and rate-limit unit, not authentication.
type session struct {
	id   string
	tag  string    // ledger tag of this session's calls in the client file
	seen time.Time // guarded by Backend.mu, like elem
	elem *list.Element

	mu              sync.Mutex // guards the rest
	uploads, clicks int
	refine, upload  *rateLimit
}

// limit takes one token from the bucket, or fails with 429 and the wait.
func (s *session) limit(bucket *rateLimit, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if wait := bucket.take(now); wait > 0 {
		return &apiError{Status: 429, Code: "rate_limited", Message: "too many requests in this session", Retryable: true, RetryAfterMs: wait}
	}
	return nil
}

// endregion

// image is one entry of the image cache: the Stack handle of an upload that is running or done.
type image struct {
	id            string
	handle        string
	width, height int
	err           error
	done          chan struct{} // closed when handle, width, height or err are set
	elem          *list.Element
}

// Backend serves the contract. It is safe for concurrent requests.
type Backend struct {
	stack     Stack
	models    []string
	now       func() time.Time
	log       func(string)
	staticDir string

	mu       sync.Mutex // guards sessions, order, images and imageUse
	sessions map[string]*session
	order    *list.List // sessions, least recently used first
	images   map[string]*image
	imageUse *list.List // images, least recently used first
}

// NewBackend creates a backend over a Stack client.
func NewBackend(stack Stack, o Options) *Backend {
	b := &Backend{stack: stack, models: o.Models, now: o.Now, log: o.Log, staticDir: o.StaticDir,
		sessions: map[string]*session{}, order: list.New(), images: map[string]*image{}, imageUse: list.New()}
	if len(b.models) == 0 {
		b.models = []string{"sam-lite"}
	}
	if b.now == nil {
		b.now = time.Now
	}
	if b.log == nil {
		b.log = func(string) {}
	}
	return b
}

// statusRecorder remembers the status for the request log.
type statusRecorder struct {
	http.ResponseWriter
	status int
	wrote  bool
}

func (r *statusRecorder) WriteHeader(status int) {
	if !r.wrote {
		r.status, r.wrote = status, true
	}
	r.ResponseWriter.WriteHeader(status)
}

func (r *statusRecorder) Write(p []byte) (int, error) {
	r.wrote = true
	return r.ResponseWriter.Write(p)
}

// ServeHTTP routes a request and logs one line: method, path, status, milliseconds. It never logs the key,
// image bytes or request bodies.
func (b *Backend) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
	defer func() {
		if p := recover(); p != nil {
			b.log(fmt.Sprintf("internal error: %v", p))
			if !rec.wrote {
				fail(rec, newError(500, "internal", "internal error"))
			}
		}
		b.log(fmt.Sprintf("%s %s %d %dms", r.Method, r.URL.EscapedPath(), rec.status, time.Since(start).Milliseconds()))
	}()
	var err error
	if p := r.URL.Path; p == "/api" || strings.HasPrefix(p, "/api/") {
		var body any
		if body, err = b.api(r); err == nil {
			writeJSON(rec, http.StatusOK, body)
		}
	} else {
		err = b.static(rec, r)
	}
	if err != nil {
		var e *apiError
		if !errors.As(err, &e) {
			b.log("internal error: " + err.Error())
			e = newError(500, "internal", "internal error")
		} else if e.cause != nil {
			b.log("internal error: " + e.cause.Error())
		}
		fail(rec, e)
	}
}

func fail(w http.ResponseWriter, e *apiError) {
	if e.RetryAfterMs > 0 {
		w.Header().Set("Retry-After", strconv.Itoa((e.RetryAfterMs+999)/1000))
	}
	if e.Allow != "" {
		w.Header().Set("Allow", e.Allow)
	}
	writeJSON(w, e.Status, map[string]any{"error": e})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	data, err := json.Marshal(body)
	if err != nil {
		status, data = 500, []byte(`{"error":{"code":"internal","message":"internal error","retryable":false,"retryAfterMs":0}}`)
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	w.Write(data)
}

type handler func(r *http.Request, s *session) (any, error)

// api serves everything under /api/. Only /api/health needs no session.
func (b *Backend) api(r *http.Request) (any, error) {
	var method string
	var h handler
	switch r.URL.Path {
	case "/api/health":
		method, h = http.MethodGet, func(*http.Request, *session) (any, error) {
			return map[string]string{"status": "ok", "language": Language, "client": "stackClient " + stackclient.Version}, nil
		}
	case "/api/images":
		method, h = http.MethodPost, b.uploadImage
	case "/api/refine":
		method, h = http.MethodPost, b.refine
	case "/api/usage":
		method, h = http.MethodGet, func(_ *http.Request, s *session) (any, error) { return b.usage(s), nil }
	default:
		return nil, newError(404, "not_found", "no route "+r.URL.Path)
	}
	if r.Method != method {
		e := newError(405, "method_not_allowed", "use "+method)
		e.Allow = method
		return nil, e
	}
	if r.URL.Path == "/api/health" {
		return h(r, nil)
	}
	s, err := b.session(r.Header.Get("X-Session-Id"))
	if err != nil {
		return nil, err
	}
	return h(r, s)
}

// session finds or creates the session of a caller. It lives in memory and ends after 30 minutes without a request;
// past 1,000 sessions the one idle the longest is dropped.
func (b *Backend) session(id string) (*session, error) {
	if !sessionIDPattern.MatchString(id) {
		return nil, newError(400, "invalid_session", "send X-Session-Id: 1 to 64 characters from A-Z, a-z, 0-9, _ and -")
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	now := b.now()
	drop := func(s *session) {
		b.order.Remove(s.elem)
		delete(b.sessions, s.id)
	}
	for e := b.order.Front(); e != nil; e = b.order.Front() {
		s := e.Value.(*session)
		if now.Sub(s.seen) <= idleTime {
			break
		}
		drop(s)
	}
	s := b.sessions[id]
	if s == nil {
		for len(b.sessions) >= maxSessions {
			drop(b.order.Front().Value.(*session))
		}
		s = &session{id: id, tag: newTag(), refine: newRateLimit(60, 10, now), upload: newRateLimit(10, 5, now)}
		s.elem = b.order.PushBack(s)
		b.sessions[id] = s
	} else {
		b.order.MoveToBack(s.elem)
	}
	s.seen = now
	return s, nil
}

func newTag() string {
	var raw [16]byte
	rand.Read(raw[:])
	return hex.EncodeToString(raw[:])
}

// lookup returns the cache entry of an image and marks it as used; nil when unknown.
func (b *Backend) lookup(id string) *image {
	b.mu.Lock()
	defer b.mu.Unlock()
	img := b.images[id]
	if img != nil {
		b.imageUse.MoveToBack(img.elem)
	}
	return img
}

// begin returns the cache entry of an image, creating it when the content is new; started tells the caller to upload.
func (b *Backend) begin(id string) (img *image, started bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if img = b.images[id]; img != nil {
		b.imageUse.MoveToBack(img.elem)
		return img, false
	}
	img = &image{id: id, done: make(chan struct{})}
	img.elem = b.imageUse.PushBack(img)
	b.images[id] = img
	for b.imageUse.Len() > maxImages {
		old := b.imageUse.Front().Value.(*image)
		b.imageUse.Remove(old.elem)
		delete(b.images, old.id)
	}
	return img, true
}

// finish records the result of an upload and wakes everyone who waits for it; a failed upload leaves the cache.
func (b *Backend) finish(img *image, info stackclient.ImageInfo, err error) {
	b.mu.Lock()
	if err != nil {
		img.err = err
		if b.images[img.id] == img {
			b.imageUse.Remove(img.elem)
			delete(b.images, img.id)
		}
	} else {
		img.handle, img.width, img.height = info.ID, info.Width, info.Height
	}
	b.mu.Unlock()
	close(img.done)
}

// ready waits until the upload of an image has finished.
func ready(ctx context.Context, img *image) error {
	select {
	case <-img.done:
		return img.err
	case <-ctx.Done():
		return ctx.Err()
	}
}

// region upload-once

// uploadImage handles POST /api/images: one Stack upload per distinct content, whatever the session.
func (b *Backend) uploadImage(r *http.Request, s *session) (any, error) {
	if err := s.limit(s.upload, b.now()); err != nil {
		return nil, err
	}
	contentType := strings.ToLower(strings.TrimSpace(strings.SplitN(r.Header.Get("Content-Type"), ";", 2)[0]))
	if !imageTypes[contentType] {
		return nil, newError(415, "unsupported_media_type", "send the raw bytes as image/jpeg, image/png or image/webp")
	}
	data, err := readBody(r, maxImageBytes)
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return nil, invalid("the body is empty: send the raw bytes of the image")
	}
	sum := sha256.Sum256(data)
	imageID := "im_" + hex.EncodeToString(sum[:])[:32]
	img, started := b.begin(imageID)
	if started {
		// the client file keeps the bytes and uploads again if Stack evicts them
		info, err := b.stack.Upload(data, contentType)
		b.finish(img, info, err)
	}
	if err := ready(r.Context(), img); err != nil {
		if r.Context().Err() != nil {
			return nil, err
		}
		return nil, fromStack(err)
	}
	s.mu.Lock()
	s.uploads++
	s.mu.Unlock()
	return map[string]any{"imageId": imageID, "width": img.width, "height": img.height}, nil
}

// endregion

// stackPoint and stackRefine are the body of POST /v1/refine: the same in every language.
type stackPoint struct {
	X        float64 `json:"x"`
	Y        float64 `json:"y"`
	Positive bool    `json:"positive"`
}

type stackRefine struct {
	Model  string            `json:"model"`
	Image  map[string]string `json:"image"`
	Points []stackPoint      `json:"points"`
	Box    []float64         `json:"box,omitempty"`
}

// stackResult is the part of the answer of POST /v1/refine that the backend uses.
type stackResult struct {
	Model     string  `json:"model"`
	Cost      float64 `json:"cost"`
	Cache     string  `json:"cache"`
	Billing   struct{ Total float64 }
	Instances []struct {
		Polygon json.RawMessage `json:"polygon"`
		BBox    json.RawMessage `json:"bbox"`
		Area    float64         `json:"area"`
		Score   float64         `json:"score"`
	} `json:"instances"`
}

// region refine

// refine handles POST /api/refine: all points of the object arrive every time, the backend keeps no click state.
func (b *Backend) refine(r *http.Request, s *session) (any, error) {
	if err := s.limit(s.refine, b.now()); err != nil {
		return nil, err
	}
	raw, err := readBody(r, maxRefineBytes)
	if err != nil {
		return nil, err
	}
	req, err := parseRefine(raw, b.models)
	if err != nil {
		return nil, err
	}
	notFound := newError(404, "image_not_found", "unknown imageId: upload the image again")
	img := b.lookup(req.imageID)
	if img == nil {
		return nil, notFound
	}
	if err := ready(r.Context(), img); err != nil {
		if r.Context().Err() != nil {
			return nil, err
		}
		return nil, notFound
	}
	if err := checkBounds(req.points, req.box, img.width, img.height); err != nil {
		return nil, err
	}
	// Stateless: every click sends all points of the object.
	call := stackRefine{Model: req.model, Image: stackclient.ImageHandle(img.handle), Points: req.points, Box: req.box}
	start := time.Now()
	data, err := b.stack.Post("/v1/refine", call, stackclient.Opts{Tag: s.tag})
	if err != nil {
		b.log("stack error: " + err.Error())
		return nil, fromStack(err)
	}
	latencyMs := int(time.Since(start).Round(time.Millisecond) / time.Millisecond)
	var result stackResult
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, fmt.Errorf("unreadable answer of Stack: %w", err)
	}
	s.mu.Lock()
	s.clicks++
	s.mu.Unlock()
	// Stack returns the outline as a simplified polygon with holes by default: no mask decoding needed here.
	out := map[string]any{
		"polygon":     json.RawMessage("[]"),
		"bbox":        []float64{0, 0, 0, 0},
		"area":        0,
		"score":       0,
		"model":       req.model,
		"costCredits": result.Cost,
		"listCredits": result.Billing.Total,
		"cache":       "miss",
		"latencyMs":   latencyMs,
	}
	if result.Model != "" {
		out["model"] = result.Model
	}
	if result.Cache == "hit" {
		out["cache"] = "hit"
	}
	if len(result.Instances) > 0 {
		inst := result.Instances[0]
		if len(inst.Polygon) > 0 && string(inst.Polygon) != "null" {
			out["polygon"] = inst.Polygon
		}
		if len(inst.BBox) > 0 && string(inst.BBox) != "null" {
			out["bbox"] = inst.BBox
		}
		out["area"], out["score"] = inst.Area, inst.Score
	}
	return out, nil
}

// endregion

// region usage

// usage answers GET /api/usage: the session's counters and its rows of the client's ledger.
func (b *Backend) usage(s *session) any {
	calls := b.stack.Calls(s.tag)
	var cost, list float64
	var hits, misses int
	for _, c := range calls {
		if !c.Replayed {
			cost += c.Charged
			list += c.List
		}
		if c.Cache != nil && *c.Cache == "hit" {
			hits++
		} else if c.Cache != nil && *c.Cache == "miss" {
			misses++
		}
	}
	round6 := func(x float64) float64 { return math.Round(x*1e6) / 1e6 }
	s.mu.Lock()
	defer s.mu.Unlock()
	return map[string]any{
		"sessionId": s.id, "uploads": s.uploads, "clicks": s.clicks,
		"costCredits": round6(cost), "listCredits": round6(list),
		"cacheHits": hits, "cacheMisses": misses, "calls": calls,
	}
}

// endregion

type refineRequest struct {
	imageID string
	points  []stackPoint
	box     []float64
	model   string
}

func invalid(message string) *apiError { return newError(400, "invalid_request", message) }

func finite(v any) (float64, bool) {
	f, ok := v.(float64)
	return f, ok && !math.IsInf(f, 0) && !math.IsNaN(f)
}

// parseRefine validates the body of POST /api/refine (shape only; bounds need the image size).
func parseRefine(raw []byte, models []string) (refineRequest, error) {
	var req refineRequest
	var doc any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return req, invalid("the body is not JSON")
	}
	body, ok := doc.(map[string]any)
	if !ok {
		return req, invalid("the body must be a JSON object")
	}
	for k := range body {
		if k != "imageId" && k != "points" && k != "box" && k != "model" {
			return req, invalid("unknown field " + k)
		}
	}
	if id, ok := body["imageId"].(string); ok && imageIDPattern.MatchString(id) {
		req.imageID = id
	} else {
		return req, invalid("imageId is missing or malformed")
	}
	pts, ok := body["points"].([]any)
	if !ok || len(pts) > maxPoints {
		return req, invalid(fmt.Sprintf("points must be a list of at most %d", maxPoints))
	}
	req.points = make([]stackPoint, 0, len(pts))
	positive := false
	for _, v := range pts {
		p, ok := v.(map[string]any)
		if !ok {
			return req, invalid("each point is {x, y, positive?} with numbers x and y")
		}
		x, okX := finite(p["x"])
		y, okY := finite(p["y"])
		flag, isBool := p["positive"].(bool)
		if _, has := p["positive"]; !okX || !okY || (has && !isBool) {
			return req, invalid("each point is {x, y, positive?} with numbers x and y")
		} else if !has {
			flag = true
		}
		for k := range p {
			if k != "x" && k != "y" && k != "positive" {
				return req, invalid("each point is {x, y, positive?}")
			}
		}
		positive = positive || flag
		req.points = append(req.points, stackPoint{X: x, Y: y, Positive: flag})
	}
	if v, has := body["box"]; has {
		arr, ok := v.([]any)
		if !ok || len(arr) != 4 {
			return req, invalid("box is [x1, y1, x2, y2]")
		}
		for _, e := range arr {
			f, ok := finite(e)
			if !ok {
				return req, invalid("box is [x1, y1, x2, y2]")
			}
			req.box = append(req.box, f)
		}
	}
	if req.box == nil && !positive {
		return req, invalid("give at least one positive point or a box")
	}
	req.model = models[0]
	if v, has := body["model"]; has && v != nil {
		m, isString := v.(string)
		if !isString || !slices.Contains(models, m) {
			return req, newError(400, "model_not_allowed", "this backend serves "+strings.Join(models, ", "))
		}
		req.model = m
	}
	return req, nil
}

// checkBounds: points in [0, width] x [0, height]; box with 0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height.
func checkBounds(points []stackPoint, box []float64, width, height int) error {
	w, h := float64(width), float64(height)
	for _, p := range points {
		if p.X < 0 || p.Y < 0 || p.X > w || p.Y > h {
			return invalid(fmt.Sprintf("point (%g, %g) is outside the %dx%d image", p.X, p.Y, width, height))
		}
	}
	if box != nil && !(box[0] >= 0 && box[1] >= 0 && box[0] < box[2] && box[1] < box[3] && box[2] <= w && box[3] <= h) {
		return invalid(fmt.Sprintf("box is not inside the %dx%d image", width, height))
	}
	return nil
}

// readBody returns the request body, refused with 413 above limit bytes. An oversized body is read on without being
// kept (up to a cap), so that the answer can still be sent.
func readBody(r *http.Request, limit int64) ([]byte, error) {
	tooLarge := newError(413, "payload_too_large", fmt.Sprintf("at most %d bytes", limit))
	if r.ContentLength > limit {
		io.Copy(io.Discard, io.LimitReader(r.Body, maxDrain))
		return nil, tooLarge
	}
	data, err := io.ReadAll(io.LimitReader(r.Body, limit+1))
	if err != nil {
		return nil, invalid("the request body could not be read")
	}
	if int64(len(data)) > limit {
		io.Copy(io.Discard, io.LimitReader(r.Body, maxDrain))
		return nil, tooLarge
	}
	return data, nil
}
