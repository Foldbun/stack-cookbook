package main

// `serve` mode: a read-only HTTP search service over an index that an earlier run wrote (this program's or the Python
// one's: the format is shared). Nothing is indexed over HTTP.
//
//	GET /search?q=<text>&k=10         images that match a text; embeds the text with siglip2: one billed call per query
//	GET /similar?id=<image id>&k=10   the images that look most like a library image (dinov3); no API call
//	GET /duplicates?threshold=0.90    groups of near-duplicate images (dinov3); no API call
//	GET /healthz                      what the service holds
//
// Responses are JSON. An error is always {"error": {"code": "...", "message": "..."}}.

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"stack-cookbook/05-image-library-search/stackclient"
)

const (
	defaultK     = 10
	maxK         = 100  // larger values of k are lowered to this
	maxQueryLen  = 1000 // bytes of a text query
	maxCachedCut = 32   // thresholds whose duplicate groups are kept
)

// Result is one ranked image.
type Result struct {
	ID    string  `json:"id"`
	Score float64 `json:"score"`
}

type service struct {
	images, texts       []Item // library items that have a dinov3 / a siglip2 vector, in matrix row order
	imageVecs, textVecs Matrix
	rowOf               map[string]int                       // image id -> row of imageVecs
	embed               func(text string) ([]float32, error) // nil: text search is off
	mu                  sync.Mutex
	groups              map[float64][][]string // threshold -> duplicate groups as image ids
}

func newService(index *LibraryIndex, embed func(string) ([]float32, error)) *service {
	s := &service{embed: embed, rowOf: map[string]int{}, groups: map[float64][][]string{}}
	s.images, s.imageVecs = index.Matrix("dinov3")
	s.texts, s.textVecs = index.Matrix("siglip2")
	for i, it := range s.images {
		s.rowOf[it.ID] = i
	}
	return s
}

// embedQuery returns the function that embeds a query text with siglip2 through the API client.
func embedQuery(client *stackclient.Client) func(string) ([]float32, error) {
	return func(text string) ([]float32, error) {
		raw, err := client.Post("/v1/embeddings", map[string]any{"model": "siglip2", "texts": []string{text}}, stackclient.Opts{})
		if err != nil {
			return nil, err
		}
		var result struct {
			Embeddings []struct {
				Vector []float32 `json:"vector"`
			} `json:"embeddings"`
		}
		if err := json.Unmarshal(raw, &result); err != nil || len(result.Embeddings) == 0 {
			return nil, errors.New("embeddings response without a vector")
		}
		return result.Embeddings[0].Vector, nil
	}
}

func respond(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func apiError(w http.ResponseWriter, status int, code, message string) {
	respond(w, status, map[string]any{"error": map[string]string{"code": code, "message": message}})
}

// parseK reads ?k=: absent means defaultK, anything but a positive integer is refused, and large values are lowered to maxK.
func parseK(r *http.Request) (int, error) {
	raw := r.URL.Query().Get("k")
	if raw == "" {
		return defaultK, nil
	}
	k, err := strconv.Atoi(raw)
	if err != nil || k < 1 {
		return 0, fmt.Errorf("k must be a positive integer, got %q", raw)
	}
	return min(k, maxK), nil
}

func results(items []Item, hits []Hit) []Result {
	out := make([]Result, 0, len(hits))
	for _, h := range hits {
		out = append(out, Result{items[h.Row].ID, round4(float64(h.Score))})
	}
	return out
}

func (s *service) search(w http.ResponseWriter, r *http.Request) {
	text := r.URL.Query().Get("q")
	k, err := parseK(r)
	if err != nil {
		apiError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	if text == "" || len(text) > maxQueryLen {
		apiError(w, http.StatusBadRequest, "bad_request", fmt.Sprintf("q must be a text of 1 to %d bytes", maxQueryLen))
		return
	}
	if s.embed == nil {
		apiError(w, http.StatusServiceUnavailable, "text_search_off", "text search needs FOLDBUN_API_KEY, which was not set when the service started")
		return
	}
	vector, err := s.embed(text)
	var apiErr *stackclient.Error
	switch {
	case errors.As(err, &apiErr):
		status := http.StatusBadGateway
		if stopCodes[apiErr.Code] {
			status = http.StatusTooManyRequests // the daily allowance, the balance or the credit cap is used up
		}
		apiError(w, status, apiErr.Code, apiErr.Message)
		return
	case err != nil:
		apiError(w, http.StatusBadGateway, "upstream_error", err.Error())
		return
	case s.textVecs.N > 0 && len(vector) != s.textVecs.Dim:
		apiError(w, http.StatusBadGateway, "dimension_mismatch", fmt.Sprintf("the model returned %d values, the index holds %d", len(vector), s.textVecs.Dim))
		return
	}
	respond(w, http.StatusOK, map[string]any{"query": text, "model": "siglip2", "results": results(s.texts, TopK(s.textVecs, vector, k, -1))})
}

func (s *service) similar(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("id")
	k, err := parseK(r)
	if err != nil {
		apiError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	if id == "" {
		apiError(w, http.StatusBadRequest, "bad_request", "id is required")
		return
	}
	row, ok := s.rowOf[id]
	if !ok {
		apiError(w, http.StatusNotFound, "not_found", "no image with this id has a dinov3 vector in the index")
		return
	}
	respond(w, http.StatusOK, map[string]any{"id": id, "model": "dinov3", "results": results(s.images, TopK(s.imageVecs, s.imageVecs.Row(row), k, row))})
}

func (s *service) duplicates(w http.ResponseWriter, r *http.Request) {
	threshold := DefaultThreshold
	if raw := r.URL.Query().Get("threshold"); raw != "" {
		var err error
		if threshold, err = strconv.ParseFloat(raw, 64); err != nil || !(threshold > 0 && threshold <= 1) {
			apiError(w, http.StatusBadRequest, "bad_request", fmt.Sprintf("threshold must be a number above 0 and at most 1, got %q", raw))
			return
		}
	}
	s.mu.Lock()
	groups, cached := s.groups[threshold]
	s.mu.Unlock()
	if !cached {
		groups = [][]string{}
		for _, g := range DuplicateGroups(s.imageVecs, threshold) {
			ids := make([]string, len(g))
			for i, row := range g {
				ids[i] = s.images[row].ID
			}
			groups = append(groups, ids)
		}
		s.mu.Lock()
		if len(s.groups) < maxCachedCut {
			s.groups[threshold] = groups
		}
		s.mu.Unlock()
	}
	respond(w, http.StatusOK, map[string]any{"threshold": threshold, "model": "dinov3", "groups": groups})
}

func (s *service) healthz(w http.ResponseWriter, r *http.Request) {
	respond(w, http.StatusOK, map[string]any{"status": "ok", "images": len(s.images), "texts": len(s.texts), "textSearch": s.embed != nil})
}

// region serve-setup

// handler routes the four read-only endpoints; any other method or path gets the standard error shape.
func (s *service) handler() http.Handler {
	routes := map[string]http.HandlerFunc{"/search": s.search, "/similar": s.similar, "/duplicates": s.duplicates, "/healthz": s.healthz}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h, ok := routes[r.URL.Path]
		switch {
		case !ok:
			apiError(w, http.StatusNotFound, "not_found", "unknown path")
		case r.Method != http.MethodGet && r.Method != http.MethodHead:
			w.Header().Set("Allow", "GET, HEAD")
			apiError(w, http.StatusMethodNotAllowed, "method_not_allowed", "the service is read-only: use GET")
		default:
			h(w, r)
		}
	})
}

// listenAndServe serves until ctx ends (SIGINT or SIGTERM in main), then lets running requests finish for up to 10 s.
func listenAndServe(ctx context.Context, ln net.Listener, h http.Handler) error {
	srv := &http.Server{
		Handler:           h,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      2 * time.Minute, // a text query waits for the API, which may retry
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}
	failed := make(chan error, 1)
	go func() { failed <- srv.Serve(ln) }()
	select {
	case err := <-failed:
		return err
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return srv.Shutdown(shutdown)
}

// endregion

func runServe(args []string) int {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	dir := fs.String("index", "out/index", "index directory written by an earlier run (either language)")
	addr := fs.String("addr", "127.0.0.1:8080", "listen address; the default is reachable from this machine only")
	if err := fs.Parse(args); err != nil {
		return exitFailed
	}
	index, err := LoadIndex(*dir)
	if err != nil {
		return fail(err)
	}
	var embed func(string) ([]float32, error)
	if client, err := stackclient.New(stackclient.Config{}); err == nil {
		embed = embedQuery(client)
	} else if stackclient.Code(err) == "missing_api_key" {
		fmt.Fprintln(os.Stderr, "FOLDBUN_API_KEY is not set: /search is off, /similar and /duplicates work")
	} else {
		return fail(err)
	}
	ln, err := net.Listen("tcp", *addr)
	if err != nil {
		return fail(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	svc := newService(index, embed)
	fmt.Printf("serving %d images on http://%s\n", len(svc.images), ln.Addr())
	if err := listenAndServe(ctx, ln, svc.handler()); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return fail(err)
	}
	return 0
}
