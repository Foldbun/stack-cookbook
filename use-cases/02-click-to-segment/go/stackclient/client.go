// Copied from clients/go/ in github.com/Foldbun/stack-cookbook. Edit the original there.

// Package stackclient is a minimal client for the Foldbun Stack API (Go standard library only).
//
// Semantics shared with the Python and TypeScript client files: API key from FOLDBUN_API_KEY, base URL from
// FOLDBUN_API_BASE, retries only for `retryable` errors, an idempotency key per call that is reused on retry,
// a per-pool token bucket, a concurrency cap, a credit cap (FOLDBUN_MAX_CREDITS) and a ledger of every call.
// Requests are built from any JSON-encodable value; results are returned as json.RawMessage, to be decoded
// into whatever struct the caller needs.
package stackclient

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	Version     = "1.0.0"
	DefaultBase = "https://api.stack.foldbun.com"
	UserAgent   = "foldbun-stack-cookbook/" + Version + " (go; +https://github.com/Foldbun/stack-cookbook)"
	MissingKey  = `FOLDBUN_API_KEY is not set. See "API keys" in the README; every use case page shows the output and cost of a real run, so it can be read without a key.`

	batchMax   = 600 * time.Second
	maxRetries = 5
	maxWait    = 60 * time.Second
	connect    = 10 * time.Second
)

// Read timeouts (the connect timeout is always 10 s).
const (
	readModels = 10 * time.Second
	readUpload = 60 * time.Second
	readCall   = 90 * time.Second
	readBatch  = 45 * time.Second // idle time between two events
)

var defaultModel = map[string]string{"/v1/ocr": "ppocr-v6", "/v1/documents/parse": "paddleocr-vl", "/v1/remove-background": "birefnet", "/v1/refine": "sam3"}

// Error is an API error (Status, Code, ...) or a local refusal (Status 0, Code "missing_api_key",
// "budget_exceeded", "network_error" or "batch_timeout"). Branch on Code only.
type Error struct {
	Status       int
	Code         string
	Message      string
	Retryable    bool
	RetryAfterMs int
	RequestID    string // from X-Request-Id
}

func (e *Error) Error() string {
	s := fmt.Sprintf("%d %s: %s", e.Status, e.Code, e.Message)
	if e.RequestID != "" {
		s += " (request " + e.RequestID + ")"
	}
	return s
}

// Code returns the error code of err when it is an *Error, else "".
func Code(err error) string {
	var e *Error
	if errors.As(err, &e) {
		return e.Code
	}
	return ""
}

// ImageBase64 is the inline image input: simplest for a single call.
func ImageBase64(data []byte) map[string]string {
	return map[string]string{"base64": base64.StdEncoding.EncodeToString(data)}
}

// ImageHandle is the uploaded image input: reuse one upload across calls and models; required in batches.
func ImageHandle(handle string) map[string]string { return map[string]string{"id": handle} }

// Config of a client. Zero values mean: key, base URL and cap from the environment, 4 requests in flight.
type Config struct {
	APIKey         string
	BaseURL        string
	MaxConcurrency int
	MaxCredits     *float64
	NoThrottle     bool
}

// Opts of one call. The zero value is fine.
type Opts struct {
	Key         string        // idempotency key, reused on every retry; a new UUID when empty
	Tag         string        // ledger tag (an item id); Calls(tag) returns the rows made with it
	ReadTimeout time.Duration // replaces the default read timeout of a call (90 s)
}

// Row is one ledger row: a successful call or an `ok` batch item. It is the `calls[]` entry of result.json.
type Row struct {
	Endpoint     string  `json:"endpoint"`
	Model        *string `json:"model"`
	List         float64 `json:"list"`
	Charged      float64 `json:"charged"`
	Cache        *string `json:"cache"`
	QueueMs      int     `json:"queueMs"`
	ProcessingMs int     `json:"processingMs"`
	Replayed     bool    `json:"replayed"`
}

// Summary is the `ledger` object of result.json.
type Summary struct {
	Calls        int     `json:"calls"`
	ListTotal    float64 `json:"listTotal"`
	ChargedTotal float64 `json:"chargedTotal"`
}

// Model is the part of an entry of GET /v1/models the client uses.
type Model struct {
	ID        string   `json:"id"`
	Available bool     `json:"available"`
	Pool      string   `json:"pool"`
	PerMinute int      `json:"perMinute"`
	Prices    []Price  `json:"prices"`
	Endpoints []string `json:"endpoints"`
}

type Price struct {
	Unit  string  `json:"unit"`
	Price float64 `json:"price"`
}

// ImageInfo is the answer of an upload.
type ImageInfo struct {
	ID          string `json:"id"`
	Width       int    `json:"width"`
	Height      int    `json:"height"`
	Bytes       int    `json:"bytes"`
	ContentType string `json:"contentType"`
}

// Event is one Server-Sent Event of a batch: "progress", "item" or "summary".
type Event struct {
	Name string
	Data json.RawMessage
}

// ErrStop returned by a Batch callback ends the stream early without an error.
var ErrStop = errors.New("stop")

type entry struct {
	tag string
	row Row
}

type Client struct {
	apiKey, base string
	maxCredits   *float64
	throttle     bool
	http         *http.Client
	sem          chan struct{}

	mu       sync.Mutex // ledger, uploads, buckets, spent, reserved
	ledger   []entry
	uploads  map[string]upload
	buckets  map[string]*bucket
	spent    float64
	reserved float64

	modelsMu sync.Mutex
	models   map[string]Model
}

type upload struct {
	data        []byte
	contentType string
}

// New creates a client. It fails with Code "missing_api_key" when no key is configured.
func New(cfg Config) (*Client, error) {
	key := cfg.APIKey
	if key == "" {
		key = os.Getenv("FOLDBUN_API_KEY")
	}
	if key == "" {
		return nil, &Error{Code: "missing_api_key", Message: MissingKey}
	}
	base := cfg.BaseURL
	if base == "" {
		base = os.Getenv("FOLDBUN_API_BASE")
	}
	if base == "" {
		base = DefaultBase
	}
	c := &Client{apiKey: key, base: strings.TrimRight(base, "/"), maxCredits: cfg.MaxCredits, throttle: !cfg.NoThrottle,
		uploads: map[string]upload{}, buckets: map[string]*bucket{}}
	if c.maxCredits == nil {
		if s := os.Getenv("FOLDBUN_MAX_CREDITS"); s != "" {
			v, err := strconv.ParseFloat(s, 64)
			if err != nil {
				return nil, fmt.Errorf("FOLDBUN_MAX_CREDITS: %w", err)
			}
			c.maxCredits = &v
		}
	}
	n := cfg.MaxConcurrency
	if n <= 0 {
		n = 4
	}
	c.sem = make(chan struct{}, n)
	c.http = &http.Client{
		Transport:     &http.Transport{DialContext: (&net.Dialer{Timeout: connect}).DialContext, Proxy: http.ProxyFromEnvironment},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	return c, nil
}

// MaxCredits is the configured credit cap, or nil.
func (c *Client) MaxCredits() *float64 { return c.maxCredits }

// region http
type reply struct {
	status int
	header http.Header
	raw    []byte
	body   io.ReadCloser      // streams only
	cancel context.CancelFunc // streams only
}

func (c *Client) request(method, path string, body []byte, headers map[string]string, read time.Duration, stream bool) (*reply, error) {
	ctx, cancel := context.WithCancel(context.Background())
	// One timer for the whole exchange, so connect and read time add up. A stream gets its own idle timer.
	timer := time.AfterFunc(connect+read, cancel)
	defer timer.Stop()
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, bytes.NewReader(body))
	if err != nil {
		cancel()
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("User-Agent", UserAgent)
	req.Header.Set("Accept", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		cancel()
		return nil, err
	}
	if stream && resp.StatusCode < 300 {
		return &reply{status: resp.StatusCode, header: resp.Header, body: resp.Body, cancel: cancel}, nil
	}
	defer cancel()
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	return &reply{status: resp.StatusCode, header: resp.Header, raw: raw}, nil
}

// send retries `retryable` API errors always and network errors only when retryNetwork.
func (c *Client) send(method, path string, body []byte, headers map[string]string, read time.Duration, retryNetwork, stream bool) (*reply, error) {
	for attempt := 0; ; attempt++ {
		rep, err := c.request(method, path, body, headers, read, stream)
		if err != nil {
			if !retryNetwork || attempt == maxRetries {
				return nil, &Error{Code: "network_error", Message: err.Error(), Retryable: true}
			}
			time.Sleep(min(time.Duration(1<<attempt)*time.Second, maxWait))
			continue
		}
		if rep.status < 300 {
			return rep, nil
		}
		e := apiError(rep)
		if !e.Retryable || attempt == maxRetries {
			return nil, e
		}
		wait := time.Duration(e.RetryAfterMs) * time.Millisecond
		if h := rep.header.Get("Retry-After"); h != "" {
			if secs, err := strconv.Atoi(h); err == nil && secs >= 0 && !strings.HasPrefix(h, "+") {
				wait = max(wait, time.Duration(secs)*time.Second)
			}
		}
		time.Sleep(min(max(wait, time.Second), maxWait))
	}
}

func apiError(rep *reply) *Error {
	rid := rep.header.Get("X-Request-Id")
	var doc struct {
		Error *struct {
			Code         *string `json:"code"`
			Message      *string `json:"message"`
			Retryable    bool    `json:"retryable"`
			RetryAfterMs float64 `json:"retryAfterMs"`
		} `json:"error"`
	}
	if json.Unmarshal(rep.raw, &doc) == nil && doc.Error != nil && doc.Error.Code != nil && doc.Error.Message != nil {
		e := doc.Error
		return &Error{rep.status, *e.Code, *e.Message, e.Retryable, int(e.RetryAfterMs), rid}
	}
	text := rep.raw
	if len(text) > 200 {
		text = text[:200]
	}
	return &Error{rep.status, "http_error", strings.ToValidUTF8(string(text), "�"), rep.status >= 500, 0, rid}
}

// endregion

// Get returns the JSON body of GET path.
func (c *Client) Get(path string) (json.RawMessage, error) {
	rep, err := c.send("GET", path, nil, nil, readModels, true, false)
	if err != nil {
		return nil, err
	}
	return rep.raw, nil
}

// Models is GET /v1/models, fetched once, keyed by model id. Free.
func (c *Client) Models() (map[string]Model, error) {
	c.modelsMu.Lock()
	defer c.modelsMu.Unlock()
	if c.models == nil {
		raw, err := c.Get("/v1/models")
		if err != nil {
			return nil, err
		}
		var doc struct {
			Data []Model `json:"data"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			return nil, err
		}
		c.models = map[string]Model{}
		for _, m := range doc.Data {
			c.models[m.ID] = m
		}
	}
	return c.models, nil
}

// region upload

// Upload is POST /v1/images with the raw bytes. Free.
func (c *Client) Upload(data []byte, contentType string) (ImageInfo, error) {
	var info ImageInfo
	if contentType == "" {
		contentType = "image/jpeg"
	}
	rep, err := c.send("POST", "/v1/images", data, map[string]string{"Content-Type": contentType}, readUpload, true, false)
	if err != nil {
		return info, err
	}
	if err := json.Unmarshal(rep.raw, &info); err != nil {
		return info, err
	}
	c.mu.Lock()
	c.uploads[info.ID] = upload{data, contentType}
	c.mu.Unlock()
	return info, nil
}

// endregion

// request view of what the client needs to price a call
type view struct {
	Model   string                       `json:"model"`
	Image   struct{ ID string }          `json:"image"`
	Prompts []map[string]json.RawMessage `json:"prompts"`
	Texts   []json.RawMessage            `json:"texts"`
	Items   []view                       `json:"items"`
}

func encode(body any) ([]byte, view, error) {
	data, err := json.Marshal(body)
	if err != nil {
		return nil, view{}, err
	}
	var v view
	return data, v, json.Unmarshal(data, &v)
}

// region post

// Post is one inference call. The idempotency key is reused on every retry, so a retry never charges twice.
// The result is the JSON body of the answer.
func (c *Client) Post(path string, body any, o Opts) (json.RawMessage, error) {
	data, v, err := encode(body)
	if err != nil {
		return nil, err
	}
	model := v.Model
	if model == "" {
		model = defaultModel[path]
	}
	models, err := c.Models()
	if err != nil {
		return nil, err
	}
	worst := estimate(models, path, v, model)
	if err := c.reserve(worst); err != nil {
		return nil, err
	}
	key := o.Key
	if key == "" {
		key = newUUID()
	}
	read := readCall
	if o.ReadTimeout > 0 {
		read = o.ReadTimeout
	}
	headers := map[string]string{"Content-Type": "application/json", "Idempotency-Key": key}
	rep, err := func() (*reply, error) {
		defer c.release(worst)
		c.sem <- struct{}{}
		defer func() { <-c.sem }()
		c.take(models, model)
		rep, err := c.send("POST", path, data, headers, read, true, false)
		if Code(err) == "image_not_found" {
			c.mu.Lock()
			saved, ok := c.uploads[v.Image.ID]
			c.mu.Unlock()
			if ok {
				// Evicted: upload again (same handle) and repeat once. A 404 is stored under the idempotency key
				// and was not charged, so the repeat needs a new key.
				if _, err := c.Upload(saved.data, saved.contentType); err != nil {
					return nil, err
				}
				headers["Idempotency-Key"] = newUUID()
				rep, err = c.send("POST", path, data, headers, read, true, false)
			}
		}
		return rep, err
	}()
	if err != nil {
		return nil, err
	}
	if err := c.record(path, rep.raw, rep.header.Get("Idempotent-Replayed") == "true", o.Tag); err != nil {
		return nil, err
	}
	return rep.raw, nil
}

// endregion

// region batch

// Batch is POST /v1/batch/{endpoint} as Server-Sent Events. fn is called for every progress, item and
// summary event; return ErrStop to end early. `ok` items are recorded in the ledger before fn sees them.
func (c *Client) Batch(endpoint string, body any, o Opts, fn func(Event) error) error {
	path, single := "/v1/batch/"+endpoint, "/v1/"+endpoint
	data, v, err := encode(body)
	if err != nil {
		return err
	}
	model := v.Model
	if model == "" {
		model = defaultModel[single]
	}
	models, err := c.Models()
	if err != nil {
		return err
	}
	worst := 0.0
	for _, it := range v.Items {
		worst += estimate(models, single, it, model)
	}
	if err := c.reserve(worst); err != nil {
		return err
	}
	defer c.release(worst)
	key := o.Key
	if key == "" {
		key = newUUID()
	}
	headers := map[string]string{"Content-Type": "application/json", "Accept": "text/event-stream", "Idempotency-Key": key}
	c.sem <- struct{}{}
	defer func() { <-c.sem }()
	c.take(models, model)
	rep, err := c.send("POST", path, data, headers, readBatch, true, true)
	if err != nil {
		return err
	}
	var timedOut atomic.Bool
	idle := time.AfterFunc(readBatch, rep.cancel)
	deadline := time.AfterFunc(batchMax, func() { timedOut.Store(true); rep.cancel() })
	defer func() { idle.Stop(); deadline.Stop(); rep.cancel(); rep.body.Close() }()
	r, event := bufio.NewReader(rep.body), ""
	for {
		line, err := r.ReadString('\n')
		idle.Reset(readBatch)
		line = strings.TrimRight(line, "\r\n")
		switch {
		case strings.HasPrefix(line, "event:"):
			event = strings.TrimSpace(line[6:])
		case strings.HasPrefix(line, "data:") && event != "":
			payload := json.RawMessage(line[5:])
			if event == "item" {
				var item struct {
					Status   string          `json:"status"`
					Replayed bool            `json:"replayed"`
					Result   json.RawMessage `json:"result"`
				}
				if err := json.Unmarshal(payload, &item); err != nil {
					return err
				}
				if item.Status == "ok" {
					if err := c.record(path, item.Result, item.Replayed, o.Tag); err != nil {
						return err
					}
				}
			}
			if err := fn(Event{event, payload}); err != nil {
				if errors.Is(err, ErrStop) {
					return nil
				}
				return err
			}
			if event == "summary" {
				return nil
			}
		}
		if err == io.EOF {
			return nil
		}
		if err != nil {
			if timedOut.Load() {
				return &Error{Code: "batch_timeout", Message: fmt.Sprintf("batch exceeded %d s", int(batchMax.Seconds()))}
			}
			return &Error{Code: "network_error", Message: "batch stream: " + err.Error(), Retryable: true}
		}
	}
}

// endregion

// region ledger
func (c *Client) record(endpoint string, raw []byte, replayed bool, tag string) error {
	var r struct {
		Model   *string  `json:"model"`
		Cost    *float64 `json:"cost"`
		Cache   *string  `json:"cache"`
		Billing *struct {
			Total *float64 `json:"total"`
		} `json:"billing"`
		Timing struct {
			QueueMs      float64 `json:"queueMs"`
			ProcessingMs float64 `json:"processingMs"`
		} `json:"timing"`
	}
	if err := json.Unmarshal(raw, &r); err != nil {
		return err
	}
	cost := 0.0
	if r.Cost != nil {
		cost = *r.Cost
	}
	row := Row{Endpoint: endpoint, Model: r.Model, List: cost, Cache: r.Cache, QueueMs: int(r.Timing.QueueMs), ProcessingMs: int(r.Timing.ProcessingMs), Replayed: replayed}
	if r.Billing != nil && r.Billing.Total != nil {
		row.List = *r.Billing.Total
	}
	if !replayed {
		row.Charged = cost
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ledger = append(c.ledger, entry{tag, row})
	if !replayed {
		c.spent += row.List
	}
	return nil
}

// Calls returns the ledger rows (the `calls[]` of result.json), only those made with tag unless it is empty.
func (c *Client) Calls(tag string) []Row {
	c.mu.Lock()
	defer c.mu.Unlock()
	rows := []Row{}
	for _, e := range c.ledger {
		if tag == "" || e.tag == tag {
			rows = append(rows, e.row)
		}
	}
	return rows
}

// Summary totals the ledger; replayed rows are counted but add nothing.
func (c *Client) Summary() Summary {
	c.mu.Lock()
	defer c.mu.Unlock()
	s := Summary{Calls: len(c.ledger)}
	for _, e := range c.ledger {
		if !e.row.Replayed {
			s.ListTotal += e.row.List
			s.ChargedTotal += e.row.Charged
		}
	}
	s.ListTotal, s.ChargedTotal = round6(s.ListTotal), round6(s.ChargedTotal)
	return s
}

// endregion

func (c *Client) reserve(worst float64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if m := c.maxCredits; m != nil && c.spent+c.reserved+worst > *m+1e-9 {
		return &Error{Code: "budget_exceeded", Message: fmt.Sprintf("call refused: up to %s credits would pass FOLDBUN_MAX_CREDITS=%s (spent %s)", g(worst), g(*m), g(c.spent))}
	}
	c.reserved += worst
	return nil
}

func (c *Client) release(worst float64) {
	c.mu.Lock()
	c.reserved -= worst
	c.mu.Unlock()
}

func (c *Client) take(models map[string]Model, model string) {
	info := models[model]
	if !c.throttle || info.PerMinute <= 0 {
		return
	}
	pool := info.Pool
	if pool == "" {
		pool = model
	}
	c.mu.Lock()
	b := c.buckets[pool]
	if b == nil {
		b = newBucket(info.PerMinute)
		c.buckets[pool] = b
	}
	c.mu.Unlock()
	b.take()
}

// bucket is a token bucket: a burst of perMinute, refilled at perMinute per minute.
type bucket struct {
	mu         sync.Mutex
	rate, cap_ float64
	tokens     float64
	last       time.Time
}

func newBucket(perMinute int) *bucket {
	return &bucket{rate: float64(perMinute) / 60, cap_: float64(perMinute), tokens: float64(perMinute), last: time.Now()}
}

func (b *bucket) take() {
	for {
		b.mu.Lock()
		now := time.Now()
		b.tokens = math.Min(b.cap_, b.tokens+now.Sub(b.last).Seconds()*b.rate)
		b.last = now
		if b.tokens >= 1 {
			b.tokens--
			b.mu.Unlock()
			return
		}
		wait := (1 - b.tokens) / b.rate
		b.mu.Unlock()
		time.Sleep(time.Duration(wait * float64(time.Second)))
	}
}

func round6(x float64) float64 { return math.Floor(x*1e6+0.5) / 1e6 }

// g formats like Python's `{x:g}`: six significant digits, no trailing zeros.
func g(x float64) string { return strconv.FormatFloat(x, 'g', 6, 64) }

func newUUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6], b[8] = b[6]&0x0f|0x40, b[8]&0x3f|0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

// Estimate is the worst-case list price of one call (the encoding fee is always counted).
func Estimate(models map[string]Model, path string, body any, model string) float64 {
	_, v, _ := encode(body)
	return estimate(models, path, v, model)
}

func estimate(models map[string]Model, path string, v view, model string) float64 {
	price := map[string]float64{}
	for _, p := range models[model].Prices {
		price[p.Unit] = p.Price
	}
	unit := func(u string, n int) float64 { return float64(price[u] * float64(n)) } // float64(): no fused multiply-add
	has := func(key string) int {
		n := 0
		for _, p := range v.Prompts {
			if _, ok := p[key]; ok {
				n++
			}
		}
		return n
	}
	switch path {
	case "/v1/segment":
		texts, boxes, points := has("text"), has("box"), has("point")
		if model == "sam-lite" {
			return round6(unit("encode_image", 1) + unit("box", boxes) + unit("point", points))
		}
		prompts := texts
		if prompts == 0 && boxes > 0 {
			prompts = 1
		}
		return round6(unit("encode_image", 1) + unit("prompt", prompts) + unit("point", points))
	case "/v1/refine":
		return round6(unit("encode_image", 1) + unit("refine", 1))
	case "/v1/embeddings":
		if v.Texts != nil {
			return round6(unit("text", len(v.Texts)))
		}
		return round6(unit("image", 1))
	case "/v1/documents/parse":
		return round6(unit("page", 1))
	}
	return round6(unit("image", 1))
}
