// Receipts and invoices to validated JSON: read with ppocr-v6, extract fields by rules, validate, fall back to paddleocr-vl.
//
// Writes <out>/result.json (contract/result.schema.json), <out>/receipts.jsonl (validated receipts) and
// <out>/needs_review.jsonl (items that no model could make pass validation). See README.md.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"stack-cookbook/03-receipts-to-json/stackclient"
)

const (
	exitFailed = 1
	exitNoKey  = 2
	exitTier   = 3
	exitBudget = 4

	ocrModel      = "ppocr-v6"
	fallbackModel = "paddleocr-vl"
	// pipeline changes when extraction or validation rules change; a resumed run only reuses items of the same version.
	pipeline = "2"
)

// region idempotency

// idempotencyKey gives the same key for the same file and model (at most 128 printable ASCII characters). Within 24 hours
// the API replays the stored answer for a repeated key without running or charging the call, so a restarted run is free.
// The key names this program ("go"): the API compares the bytes of the request, and another program that writes the
// same JSON with different spacing would get `409 idempotency_conflict` under a shared key.
func idempotencyKey(imageSHA256, model string) string {
	return "r03-go-" + imageSHA256 + "-" + model
}

// endregion

func contentType(name string) string {
	switch strings.ToLower(filepath.Ext(name)) {
	case ".png":
		return "image/png"
	case ".webp":
		return "image/webp"
	}
	return "image/jpeg"
}

type manifestItem struct {
	ID    string `json:"id"`
	Image string `json:"image"`
}

// region attempt

// Attempt is what one model read from one image, with the checks of that reading.
type Attempt struct {
	Model   string
	Receipt *Receipt
	Passed  bool
	Checks  []Check
	Note    string
}

// AttemptSummary is what an attempt keeps in the output: the receipt as that model read it and the names of the checks it failed.
type AttemptSummary struct {
	Model        string   `json:"model"`
	Passed       bool     `json:"passed"`
	FailedChecks []string `json:"failedChecks"`
	Receipt      *Receipt `json:"receipt"`
	Note         string   `json:"note,omitempty"`
}

func (a Attempt) summary() AttemptSummary {
	failed := []string{}
	for _, c := range a.Checks {
		if !c.Passed {
			failed = append(failed, c.Name)
		}
	}
	return AttemptSummary{a.Model, a.Passed, failed, a.Receipt, a.Note}
}

func attempt(model string, receipt Receipt, today calendarDate) Attempt {
	checks := validate(receipt, today)
	return Attempt{Model: model, Receipt: &receipt, Passed: passed(checks), Checks: checks}
}

// endregion

// Output is the `output` of one item in result.json (output.schema.json).
type Output struct {
	Outcome  string           `json:"outcome"`
	Model    string           `json:"model"`
	Receipt  *Receipt         `json:"receipt"`
	Checks   []Check          `json:"checks"`
	Attempts []AttemptSummary `json:"attempts"`
}

type itemError struct {
	Status  int    `json:"status"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Entry is one item of result.json. Output is kept as JSON so that an item finished by an earlier run is reused as it was written.
type Entry struct {
	ID     string            `json:"id"`
	Status string            `json:"status"`
	Calls  []stackclient.Row `json:"calls"`
	Output json.RawMessage   `json:"output"`
	Error  *itemError        `json:"error,omitempty"`
}

// processItem runs one image through the whole pipeline. It returns the item entry of result.json and the sha256 of the image.
func processItem(client *stackclient.Client, item manifestItem, base string, useFallback bool, today calendarDate) (Entry, string, error) {
	data, err := os.ReadFile(filepath.Join(base, item.Image))
	if err != nil {
		return Entry{}, "", err
	}
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	uploaded, err := client.Upload(data, contentType(item.Image)) // free; both models read the same handle
	if err != nil {
		return Entry{}, "", err
	}
	handle := uploaded.ID
	var attempts []Attempt

	// 1. ppocr-v6: 0.25 credits, inside the public pool's daily allowance.
	// region ocr-call
	raw, err := client.Post("/v1/ocr",
		map[string]any{"model": ocrModel, "image": stackclient.ImageHandle(handle), "options": map[string]any{"words": false}},
		stackclient.Opts{Key: idempotencyKey(sha, ocrModel), Tag: item.ID})
	if err != nil {
		return Entry{}, "", err
	}
	var ocr struct {
		Lines []ocrLine `json:"lines"`
		Image struct {
			Width int `json:"width"`
		} `json:"image"`
	}
	if err := json.Unmarshal(raw, &ocr); err != nil {
		return Entry{}, "", err
	}
	attempts = append(attempts, attempt(ocrModel, extractReceipt(rowsFromOCR(ocr.Lines), ocrModel, ocr.Image.Width), today))
	// endregion

	// 2. Only an item that failed validation is read again, by paddleocr-vl on the same handle: 2 credits, paid models only.
	// region fallback-call
	if useFallback && !attempts[0].Passed {
		raw, err := client.Post("/v1/documents/parse",
			map[string]any{"model": fallbackModel, "image": stackclient.ImageHandle(handle), "options": map[string]any{"blocks": true}},
			stackclient.Opts{Key: idempotencyKey(sha, fallbackModel), Tag: item.ID})
		var apiErr *stackclient.Error
		switch {
		case errors.As(err, &apiErr) && apiErr.Code != "budget_exceeded":
			// An account without the paid pool (model_locked) or a failed call: the item stays with what ppocr-v6 read.
			attempts = append(attempts, Attempt{Model: fallbackModel, Note: apiErr.Code})
		case err != nil:
			return Entry{}, "", err
		default:
			var page struct {
				Blocks []block `json:"blocks"`
				Image  struct {
					Width int `json:"width"`
				} `json:"image"`
			}
			if err := json.Unmarshal(raw, &page); err != nil {
				return Entry{}, "", err
			}
			attempts = append(attempts, attempt(fallbackModel, extractReceipt(rowsFromBlocks(page.Blocks), fallbackModel, page.Image.Width), today))
		}
	}
	// endregion

	// 3. The result is the first attempt that passed; otherwise the attempt with the most passed checks goes to review.
	var good, best *Attempt
	bestScore := -1
	for i := range attempts {
		a := &attempts[i]
		if a.Passed && good == nil {
			good = a
		}
		if a.Receipt != nil {
			score := 0
			for _, c := range a.Checks {
				if c.Passed {
					score++
				}
			}
			if score >= bestScore { // on a tie the later attempt wins
				best, bestScore = a, score
			}
		}
	}
	outcome := "needs_review"
	if good != nil {
		outcome, best = "validated", good
	}
	out := Output{Outcome: outcome, Model: best.Model, Receipt: best.Receipt, Checks: best.Checks, Attempts: []AttemptSummary{}}
	for _, a := range attempts {
		out.Attempts = append(out.Attempts, a.summary())
	}
	encoded, err := json.Marshal(out)
	if err != nil {
		return Entry{}, "", err
	}
	return Entry{ID: item.ID, Status: "ok", Calls: client.Calls(item.ID), Output: encoded}, sha, nil
}

// region resume
func itemFile(out, itemID string) string { return filepath.Join(out, "items", itemID+".json") }

type savedItem struct {
	Key   []any `json:"key"`
	Entry Entry `json:"entry"`
}

func resumeKey(sha string, useFallback bool) []any { return []any{sha, pipeline, useFallback} }

// loadDone returns the saved entry of an item finished by an earlier run of this directory, when the file, the rules and the options are unchanged.
func loadDone(out string, item manifestItem, base string, useFallback bool) *Entry {
	raw, err := os.ReadFile(itemFile(out, item.ID))
	if err != nil {
		return nil
	}
	var saved savedItem
	if json.Unmarshal(raw, &saved) != nil {
		return nil
	}
	data, err := os.ReadFile(filepath.Join(base, item.Image))
	if err != nil {
		return nil
	}
	sum := sha256.Sum256(data)
	want, _ := json.Marshal(resumeKey(hex.EncodeToString(sum[:]), useFallback))
	have, _ := json.Marshal(saved.Key)
	if string(want) != string(have) || saved.Entry.Status != "ok" {
		return nil
	}
	return &saved.Entry
}

// saveDone writes one small file per finished item, atomically, so a killed run loses at most the items in flight.
func saveDone(out string, entry Entry, sha string, useFallback bool) error {
	path := itemFile(out, entry.ID)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	raw, err := json.Marshal(savedItem{resumeKey(sha, useFallback), entry})
	if err != nil {
		return err
	}
	tmp := strings.TrimSuffix(path, ".json") + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// endregion

func round6(x float64) float64 { return math.Round((x+1e-12)*1e6) / 1e6 }

// ledgerOf totals the calls of the entries; replayed calls are counted but add nothing.
func ledgerOf(entries []Entry) stackclient.Summary {
	var s stackclient.Summary
	for _, e := range entries {
		for _, c := range e.Calls {
			s.Calls++
			if !c.Replayed {
				s.ListTotal += c.List
				s.ChargedTotal += c.Charged
			}
		}
	}
	s.ListTotal, s.ChargedTotal = round6(s.ListTotal), round6(s.ChargedTotal)
	return s
}

func writeJSON(path string, v any) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

func run() int {
	manifestPath := flag.String("manifest", "../assets/smoke/manifest.json", "manifest.json of the run")
	out := flag.String("out", "out", "directory for result.json and the other files")
	concurrency := flag.Int("concurrency", 4, "requests in flight")
	limit := flag.Int("limit", 0, "only the first N items (0: all)")
	resume := flag.Bool("resume", false, "reuse the items already finished in --out instead of reading them again")
	noFallback := flag.Bool("no-fallback", false, "OCR only: items that fail validation go straight to needs_review.jsonl")
	flag.Parse()
	useFallback := !*noFallback

	raw, err := os.ReadFile(*manifestPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	var manifest struct {
		Usecase string         `json:"usecase"`
		Items   []manifestItem `json:"items"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		fmt.Fprintln(os.Stderr, "manifest:", err)
		return exitFailed
	}
	base, now := filepath.Dir(*manifestPath), time.Now()
	today := calendarDate{now.Year(), int(now.Month()), now.Day()}
	started := now.UTC()
	client, err := stackclient.New(stackclient.Config{MaxConcurrency: *concurrency}) // FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
	if stackclient.Code(err) == "missing_api_key" {
		fmt.Fprintln(os.Stderr, stackclient.MissingKey)
		return exitNoKey
	} else if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	if _, err := client.Models(); err != nil { // free: checks the key and loads prices and the per-minute limits the client paces itself by
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}

	items := manifest.Items
	if *limit > 0 && *limit < len(items) {
		items = items[:*limit]
	}
	entries := map[string]Entry{}
	var todo []manifestItem
	for _, item := range items {
		if *resume {
			if done := loadDone(*out, item, base, useFallback); done != nil {
				entries[item.ID] = *done
				fmt.Printf("%s: resumed\n", item.ID)
				continue
			}
		}
		todo = append(todo, item)
	}

	// region concurrency
	// The client allows `--concurrency` requests in flight and spaces calls by the per-minute limit of the model's pool,
	// so the worker goroutines here only keep it busy. One item is one goroutine: its calls are sequential (OCR, then maybe parse).
	var (
		mu     sync.Mutex
		wg     sync.WaitGroup
		halt   atomic.Bool
		code   int
		failed error
	)
	jobs := make(chan manifestItem)
	for w := 0; w < max(*concurrency, 1); w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for item := range jobs {
				if halt.Load() {
					continue
				}
				entry, sha, err := processItem(client, item, base, useFallback, today)
				var apiErr *stackclient.Error
				mu.Lock()
				switch {
				case errors.As(err, &apiErr):
					fmt.Fprintf(os.Stderr, "%s: %s\n", item.ID, apiErr)
					if apiErr.Code == "model_locked" || apiErr.Code == "budget_exceeded" {
						code = exitTier
						if apiErr.Code == "budget_exceeded" {
							code = exitBudget
						}
						halt.Store(true)
						break
					}
					entries[item.ID] = Entry{ID: item.ID, Status: "error", Calls: client.Calls(item.ID), Output: json.RawMessage("null"),
						Error: &itemError{apiErr.Status, apiErr.Code, apiErr.Message}}
				case err != nil:
					fmt.Fprintf(os.Stderr, "%s: %v\n", item.ID, err)
					failed = err
					halt.Store(true)
				default:
					entries[item.ID] = entry
					if err := saveDone(*out, entry, sha, useFallback); err != nil {
						fmt.Fprintln(os.Stderr, err)
					}
					var o Output
					_ = json.Unmarshal(entry.Output, &o)
					fmt.Printf("%s: %s (%s)\n", item.ID, o.Outcome, o.Model)
				}
				mu.Unlock()
			}
		}()
	}
	for _, item := range todo {
		jobs <- item
	}
	close(jobs)
	wg.Wait()
	// endregion
	if failed != nil {
		return exitFailed
	}

	done := []Entry{}
	for _, item := range items {
		if e, ok := entries[item.ID]; ok {
			done = append(done, e)
		}
	}
	if err := os.MkdirAll(*out, 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	if err := writeReviewFiles(*out, done); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	result := struct {
		Usecase   string              `json:"usecase"`
		Language  string              `json:"language"`
		Client    string              `json:"client"`
		StartedAt string              `json:"startedAt"`
		ElapsedMs int64               `json:"elapsedMs"`
		Items     []Entry             `json:"items"`
		Ledger    stackclient.Summary `json:"ledger"`
	}{manifest.Usecase, "go", "stackclient " + stackclient.Version, started.Format("2006-01-02T15:04:05Z"),
		time.Since(now).Milliseconds(), done, ledgerOf(done)}
	if err := writeJSON(filepath.Join(*out, "result.json"), result); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	return code
}

// writeReviewFiles splits the finished items into receipts.jsonl and needs_review.jsonl.
func writeReviewFiles(out string, done []Entry) error {
	// region needs-review
	// What no model could make pass goes to a person, with the best receipt as a starting point and the reasons.
	review, err := os.Create(filepath.Join(out, "needs_review.jsonl"))
	if err != nil {
		return err
	}
	defer review.Close()
	good, err := os.Create(filepath.Join(out, "receipts.jsonl"))
	if err != nil {
		return err
	}
	defer good.Close()
	for _, entry := range done {
		var o struct {
			Outcome string          `json:"outcome"`
			Model   string          `json:"model"`
			Receipt json.RawMessage `json:"receipt"`
			Checks  []Check         `json:"checks"`
		}
		if len(entry.Output) == 0 || string(entry.Output) == "null" || json.Unmarshal(entry.Output, &o) != nil {
			continue
		}
		var line any
		if o.Outcome == "validated" {
			line = struct {
				ID      string          `json:"id"`
				Model   string          `json:"model"`
				Receipt json.RawMessage `json:"receipt"`
			}{entry.ID, o.Model, o.Receipt}
		} else {
			reasons := []string{}
			for _, c := range o.Checks {
				if !c.Passed {
					reasons = append(reasons, c.Detail)
				}
			}
			line = struct {
				ID      string          `json:"id"`
				Reasons []string        `json:"reasons"`
				Model   string          `json:"model"`
				Receipt json.RawMessage `json:"receipt"`
			}{entry.ID, reasons, o.Model, o.Receipt}
		}
		w := review
		if o.Outcome == "validated" {
			w = good
		}
		enc := json.NewEncoder(w)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(line); err != nil {
			return err
		}
	}
	// endregion
	return nil
}

func main() { os.Exit(run()) }
