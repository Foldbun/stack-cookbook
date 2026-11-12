// Index the images of a manifest, then run its text queries; write result.json for the harness and the evaluator.
//
// This is the entry point the cookbook harness runs (--manifest, --out). Next to result.json it writes:
//
//	index/        the vector index (format described in index.go; the Python implementation reads and writes the same one)
//	stats.json    timing, what was embedded, what the second (no-op) indexing pass did, the daily allowance
//
// `serve` starts the search service over such an index instead: see serve.go.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"time"

	"stack-cookbook/05-image-library-search/stackclient"
)

const (
	exitFailed = 1
	exitNoKey  = 2
	exitTier   = 3
	exitBudget = 4

	neighbors   = 10  // nearest images kept per image: enough to evaluate duplicates and retrieval by example
	textResults = 100 // ranked images kept per text query
)

var models = []string{"dinov3", "siglip2"}

type manifest struct {
	Usecase string `json:"usecase"`
	Items   []struct {
		ID     string `json:"id"`
		Image  string `json:"image"`
		Sha256 string `json:"sha256"`
	} `json:"items"`
	Queries []struct {
		ID   string `json:"id"`
		Text string `json:"text"`
	} `json:"queries"`
}

// imageOutput and queryOutput are the two shapes of items[].output (output.schema.json).
type imageOutput struct {
	Type           string         `json:"type"`
	Sha256         string         `json:"sha256"`
	Dimensions     map[string]int `json:"dimensions"`
	Neighbors      []string       `json:"neighbors"`
	NeighborScores []float64      `json:"neighborScores"`
	DuplicateGroup *string        `json:"duplicateGroup"`
	GroupSize      int            `json:"groupSize"`
}

type queryOutput struct {
	Type    string    `json:"type"`
	Text    string    `json:"text"`
	Ranking []string  `json:"ranking"`
	Scores  []float64 `json:"scores"`
}

type errOutput struct {
	Status  int    `json:"status,omitempty"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

type resultItem struct {
	ID     string            `json:"id"`
	Status string            `json:"status"`
	Calls  []stackclient.Row `json:"calls"`
	Output any               `json:"output"`
	Error  *errOutput        `json:"error,omitempty"`
}

type result struct {
	Usecase   string              `json:"usecase"`
	Language  string              `json:"language"`
	Client    string              `json:"client"`
	StartedAt string              `json:"startedAt"`
	ElapsedMs int64               `json:"elapsedMs"`
	Items     []resultItem        `json:"items"`
	Ledger    stackclient.Summary `json:"ledger"`
}

type stats struct {
	IndexSeconds float64 `json:"indexSeconds"`
	Reindex      struct {
		APICalls int `json:"apiCalls"`
		Embedded int `json:"embedded"`
	} `json:"reindex"`
	ImageSearchMs float64  `json:"imageSearchMs"`
	TextSearchMs  *float64 `json:"textSearchMs"`
	Library       struct {
		Images          int `json:"images"`
		UniqueImages    int `json:"uniqueImages"`
		VectorsEmbedded int `json:"vectorsEmbedded"`
	} `json:"library"`
	Stopped   *Stopped                  `json:"stopped"`
	Allowance map[string]map[string]any `json:"allowance"`
	Failed    int                       `json:"failed"`
}

func r4(scores []float64) []float64 {
	out := make([]float64, len(scores))
	for i, s := range scores {
		out[i] = round4(s)
	}
	return out
}

// catalogue lists the library items of the manifest: id, path, sha256 (checked against the manifest when it has one).
func catalogue(m manifest, base string, limit int) ([]Item, error) {
	n := len(m.Items)
	if limit > 0 {
		n = min(n, limit)
	}
	items := make([]Item, 0, n)
	for _, it := range m.Items[:n] {
		path := filepath.Join(base, it.Image)
		sha, err := fileSha256(path)
		if err != nil {
			return nil, err
		}
		if it.Sha256 != "" && it.Sha256 != sha {
			return nil, fmt.Errorf("%s: sha256 differs from the manifest", it.Image)
		}
		st, err := os.Stat(path)
		if err != nil {
			return nil, err
		}
		items = append(items, Item{ID: it.ID, Path: it.Image, Sha256: sha, Bytes: st.Size()})
	}
	return items, nil
}

func errorOf(err *ErrInfo, defaultCode string) *errOutput {
	if err == nil {
		return &errOutput{Code: defaultCode, Message: "not embedded in this run"}
	}
	return &errOutput{Code: err.Code, Message: err.Message}
}

// region image-outputs

// imageOutputs gives, per image: dimensions, its nearest neighbours by dinov3, and its near-duplicate group at the default threshold.
func imageOutputs(index *LibraryIndex, ids []string) map[string]*imageOutput {
	items, matrix := index.Matrix("dinov3")
	rowOf, groupOf := map[string]int{}, map[string][]int{}
	for i, it := range items {
		rowOf[it.ID] = i
	}
	for _, group := range DuplicateGroups(matrix, DefaultThreshold) {
		for _, i := range group {
			groupOf[items[i].ID] = group
		}
	}
	byID, out := map[string]Item{}, map[string]*imageOutput{}
	for _, it := range index.Items {
		byID[it.ID] = it
	}
	for _, id := range ids {
		it := byID[id]
		var hits []Hit
		if row, ok := rowOf[id]; ok {
			hits = TopK(matrix, matrix.Row(row), neighbors, row)
		}
		o := &imageOutput{Type: "image", Sha256: it.Sha256, Dimensions: map[string]int{}, Neighbors: []string{}, NeighborScores: []float64{}, GroupSize: 1}
		for _, m := range models {
			if index.Has(m, it.Sha256) {
				o.Dimensions[m] = index.Dimensions(m)
			}
		}
		scores := []float64{}
		for _, h := range hits {
			o.Neighbors = append(o.Neighbors, items[h.Row].ID)
			scores = append(scores, float64(h.Score))
		}
		o.NeighborScores = r4(scores)
		if group := groupOf[id]; group != nil {
			first := items[group[0]].ID
			o.DuplicateGroup, o.GroupSize = &first, len(group)
		}
		out[id] = o
	}
	return out
}

// endregion

func median(xs []float64) float64 {
	sort.Float64s(xs)
	if n := len(xs); n%2 == 1 {
		return xs[n/2]
	} else {
		return (xs[n/2-1] + xs[n/2]) / 2
	}
}

func round3(x float64) float64 {
	f, _ := strconv.ParseFloat(strconv.FormatFloat(x, 'f', 3, 64), 64)
	return f
}

func writeJSON(path string, v any) error {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return err
	}
	return os.WriteFile(path, buf.Bytes(), 0o644)
}

func fail(err error) int {
	fmt.Fprintln(os.Stderr, err)
	return exitFailed
}

func run() int {
	manifestPath := flag.String("manifest", "../assets/smoke/manifest.json", "manifest.json of the run")
	out := flag.String("out", "out", "directory for result.json")
	concurrency := flag.Int("concurrency", 4, "requests in flight")
	limit := flag.Int("limit", 0, "only the first N items (0: all)")
	resume := flag.Bool("resume", false, "continue from the index in <out>/index (without it the index is rebuilt from scratch)")
	batchSize := flag.Int("batch-size", 50, "images per batch request")
	freeOnly := flag.Bool("free-only", false, "stop when the included daily calls are used up instead of paying for more")
	flag.Parse()
	raw, err := os.ReadFile(*manifestPath)
	if err != nil {
		return fail(err)
	}
	var mf manifest
	if err := json.Unmarshal(raw, &mf); err != nil {
		return fail(fmt.Errorf("manifest: %w", err))
	}
	base := filepath.Dir(*manifestPath) // not resolved: the images are found next to it, also behind a symlink
	started, t0 := time.Now().UTC(), time.Now()
	client, err := stackclient.New(stackclient.Config{MaxConcurrency: *concurrency}) // FOLDBUN_API_KEY, FOLDBUN_API_BASE
	if stackclient.Code(err) == "missing_api_key" {
		fmt.Fprintln(os.Stderr, stackclient.MissingKey)
		return exitNoKey
	}
	if err != nil {
		return fail(err)
	}
	if _, err := client.Models(); err != nil { // free: checks the key
		return fail(err)
	}

	indexDir := filepath.Join(*out, "index")
	if !*resume {
		os.RemoveAll(indexDir)
	}
	index, err := LoadIndex(indexDir)
	if err != nil {
		return fail(err)
	}
	library, err := catalogue(mf, base, *limit)
	if err != nil {
		return fail(err)
	}
	index.SetItems(library)
	if err := index.Commit(); err != nil {
		return fail(err)
	}
	ids, firstWith := []string{}, map[string]string{} // an exact copy shares its twin's vector, and its calls
	for _, it := range index.Items {
		ids = append(ids, it.ID)
		if _, ok := firstWith[it.Sha256]; !ok {
			firstWith[it.Sha256] = it.ID
		}
	}

	code, st := 0, stats{}
	var report *Report
	{
		// region run-index
		tIndex := time.Now()
		report, err = indexLibrary(client, index, base, models, *batchSize, *freeOnly, 0, *concurrency, func(s string) { fmt.Println(s) })
		st.IndexSeconds = round3(time.Since(tIndex).Seconds())
		// endregion
		if err == nil {
			// region reindex-check
			callsBefore := len(client.Calls(""))
			var again *Report
			again, err = indexLibrary(client, index, base, models, *batchSize, *freeOnly, 0, *concurrency, func(string) {})
			if err == nil {
				st.Reindex.APICalls, st.Reindex.Embedded = len(client.Calls(""))-callsBefore, again.Embedded // nothing is missing, so nothing is sent
			}
			// endregion
			if err == nil && report.Stopped == nil && len(report.Failed) == 0 && st.Reindex.APICalls > 0 {
				fmt.Fprintln(os.Stderr, "second indexing pass made API calls: incremental indexing is broken")
				code = exitFailed
			}
		}
	}
	if err != nil {
		var apiErr *stackclient.Error
		if !errors.As(err, &apiErr) { // not an API error: the index or the disk
			return fail(err)
		}
		fmt.Fprintln(os.Stderr, " ", apiErr) // model_locked, or a failure no retry helps
		if apiErr.Code == "model_locked" {
			return exitTier
		}
		return exitFailed
	}

	outputs := imageOutputs(index, ids)
	byID, items := map[string]Item{}, []resultItem{}
	for _, it := range index.Items {
		byID[it.ID] = it
	}
	tSearch := time.Now()
	for _, id := range ids {
		sha := byID[id].Sha256
		entry := resultItem{ID: id, Calls: []stackclient.Row{}}
		if index.Has("dinov3", sha) && index.Has("siglip2", sha) {
			entry.Status, entry.Output = "ok", outputs[id]
		} else if failure, ok := report.Failed[sha]; ok {
			entry.Status, entry.Error = "error", errorOf(&failure, "failed")
		} else {
			var why *ErrInfo
			if report.Stopped != nil {
				why = &ErrInfo{report.Stopped.Code, report.Stopped.Message}
			}
			entry.Status, entry.Error = "expired", errorOf(why, "stopped")
		}
		if firstWith[sha] == id && report.Rows[sha] != nil {
			entry.Calls = report.Rows[sha]
		}
		items = append(items, entry)
	}
	st.ImageSearchMs = round3(float64(time.Since(tSearch).Microseconds()) / 1000 / float64(max(len(ids), 1)))

	// region text-queries
	var textMs []float64
	siglipItems, siglipMatrix := index.Matrix("siglip2")
	for _, q := range mf.Queries {
		entry := resultItem{ID: q.ID, Calls: []stackclient.Row{}}
		vector, row, err := embedText(client, q.Text, "siglip2", q.ID)
		if err == nil && siglipMatrix.N > 0 && len(vector) != siglipMatrix.Dim {
			err = fmt.Errorf("the query vector has %d values, the index %d", len(vector), siglipMatrix.Dim)
		}
		var apiErr *stackclient.Error
		switch {
		case err == nil:
			t := time.Now()
			hits := TopK(siglipMatrix, vector, textResults, -1)
			textMs = append(textMs, float64(time.Since(t).Microseconds())/1000)
			ranking, scores := []string{}, []float64{}
			for _, h := range hits {
				ranking, scores = append(ranking, siglipItems[h.Row].ID), append(scores, float64(h.Score))
			}
			entry.Status, entry.Calls = "ok", []stackclient.Row{row}
			entry.Output = queryOutput{Type: "query", Text: q.Text, Ranking: ranking, Scores: r4(scores)}
		case errors.As(err, &apiErr):
			fmt.Fprintf(os.Stderr, "  %s: %v\n", q.ID, apiErr)
			if apiErr.Code == "model_locked" {
				return exitTier
			}
			entry.Status = "error"
			if stopCodes[apiErr.Code] {
				entry.Status = "expired"
			}
			entry.Error = &errOutput{Status: apiErr.Status, Code: apiErr.Code, Message: apiErr.Message}
			if apiErr.Code == "budget_exceeded" {
				code = exitBudget
			}
		default:
			return fail(err)
		}
		items = append(items, entry)
	}
	// endregion
	if len(textMs) > 0 {
		m := round3(median(textMs))
		st.TextSearchMs = &m
	}

	unique := map[string]bool{}
	for _, it := range index.Items {
		unique[it.Sha256] = true
	}
	st.Library.Images, st.Library.UniqueImages, st.Library.VectorsEmbedded = len(index.Items), len(unique), report.Embedded
	st.Stopped, st.Allowance, st.Failed = report.Stopped, report.Allowance, len(report.Failed)

	res := result{
		Usecase:   mf.Usecase,
		Language:  "go",
		Client:    "stackclient " + stackclient.Version,
		StartedAt: started.Format("2006-01-02T15:04:05Z"),
		ElapsedMs: time.Since(t0).Milliseconds(),
		Items:     items,
		Ledger:    client.Summary(),
	}
	if err := os.MkdirAll(*out, 0o755); err != nil {
		return fail(err)
	}
	if err := writeJSON(filepath.Join(*out, "result.json"), res); err != nil {
		return fail(err)
	}
	if err := writeJSON(filepath.Join(*out, "stats.json"), st); err != nil {
		return fail(err)
	}
	if report.Stopped != nil {
		fmt.Fprintf(os.Stderr, "stopped early: %s; run again with --resume after 00:00 UTC\n", report.Stopped.Message)
	}
	return code
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "serve" { // go run . serve --index DIR: see serve.go
		os.Exit(runServe(os.Args[2:]))
	}
	os.Exit(run())
}
