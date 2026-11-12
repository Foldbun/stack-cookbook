package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"stack-cookbook/01-dataset-prelabelling/stackclient"
)

// Scan, upload and batch execution: the part that makes a long run resumable and safe to repeat.
//
// A run is a list of units (one image plus its prompts). Units are cut into batches of at most `--batch-size` and sent
// as `POST /v1/batch/segment` event streams. Every step can be repeated without paying twice:
//
//   - uploads are free and the handle is `img_` + the sha256 of the image, so uploading again returns the same handle;
//   - a batch has an idempotency key derived from its round, its index and the sha256 of its request body, so sending
//     the same batch again replays the items that finished (`replayed: true`, no charge) and runs the rest;
//   - items that came back `expired`, or failed with a retryable error, go into a new batch in the next round;
//   - every finished item is appended to `<out>/checkpoint.jsonl`, so `--resume` does not even ask the API again.
//
// Shutdown. The run takes a context. When it is cancelled (SIGINT or SIGTERM) no new batch is started, the batches
// in flight finish and are checkpointed, and the program writes its result with what it has.

const (
	streamAttempts = 3
	maxPrompts     = 50 // per segment item
)

var retryableItemErrors = map[string]bool{"model_busy": true, "model_unavailable": true, "upstream_error": true, "upstream_timeout": true,
	"internal_error": true, "unavailable": true, "rate_limited": true, "no_result": true}

var contentTypes = map[string]string{".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

// manifestItem is one entry of manifest.json.
type manifestItem struct {
	ID              string   `json:"id"`
	Image           string   `json:"image"`
	Prompts         []string `json:"prompts"`
	SHA256          string   `json:"sha256"`
	ExpectedClasses []string `json:"expectedClasses"`
}

// Unit is one image to segment: the manifest entry, its file, and what the API needs and returns for it.
type Unit struct {
	Item          manifestItem
	Path          string
	SHA256        string
	Handle        string
	Width, Height int
	Prompts       []map[string]any // API prompt objects
	Labels        []string         // class name per prompt, same order
	Body          map[string]any   // the SegmentItem sent in the batch
	Digest        string           // sha256 of Body: identifies the request in the checkpoint
}

func (u *Unit) ID() string { return u.Item.ID }

// itemError is the `error` of an item in result.json.
type itemError struct {
	Status  int    `json:"status"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

// outcome is what a unit ended with. Status is "ok", "error", "expired" or "no_result" (the stream ended before the item).
type outcome struct {
	Status string
	Result *segResult
	Err    *itemError
	Calls  []stackclient.Row
}

func logf(format string, args ...any) { fmt.Fprintf(os.Stderr, format+"\n", args...) }

// region scan
// scan reads every image once: sha256 (checked against the manifest when it has one) and the path.
func scan(items []manifestItem, base string) ([]*Unit, error) {
	units := []*Unit{}
	for _, item := range items {
		path := filepath.Join(base, item.Image)
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, err
		}
		sum := sha256.Sum256(data)
		digest := hex.EncodeToString(sum[:])
		if item.SHA256 != "" && item.SHA256 != digest {
			return nil, fmt.Errorf("%s: sha256 of %s does not match the manifest", item.ID, item.Image)
		}
		units = append(units, &Unit{Item: item, Path: path, SHA256: digest})
	}
	return units, nil
}

// endregion

// region upload
// uploadAll uploads every distinct file once (free). The handle is `img_<sha256>`, so repeated files share one upload.
func uploadAll(client *stackclient.Client, units []*Unit, workers int) error {
	groups, order := map[string][]*Unit{}, []string{}
	for _, u := range units {
		if _, ok := groups[u.SHA256]; !ok {
			order = append(order, u.SHA256)
		}
		groups[u.SHA256] = append(groups[u.SHA256], u)
	}
	var wg sync.WaitGroup
	var mu sync.Mutex
	var firstErr error
	slots := make(chan struct{}, workers)
	for _, sum := range order {
		group := groups[sum]
		slots <- struct{}{}
		wg.Add(1)
		go func() {
			defer func() { <-slots; wg.Done() }()
			data, err := os.ReadFile(group[0].Path)
			var info stackclient.ImageInfo
			if err == nil {
				ct := contentTypes[strings.ToLower(filepath.Ext(group[0].Path))]
				info, err = client.Upload(data, ct)
			}
			mu.Lock()
			defer mu.Unlock()
			if err != nil && firstErr == nil {
				firstErr = err
			}
			for _, u := range group {
				u.Handle, u.Width, u.Height = info.ID, info.Width, info.Height
			}
		}()
	}
	wg.Wait()
	return firstErr
}

// endregion

// canonical is the JSON of a request body with sorted keys and no insignificant whitespace (maps are sorted by encoding/json).
func canonical(v any) []byte {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		panic(err) // request bodies are built from maps, strings, numbers and slices
	}
	return bytes.TrimRight(buf.Bytes(), "\n")
}

func digestOf(v any) string {
	sum := sha256.Sum256(canonical(v))
	return hex.EncodeToString(sum[:])
}

// region batch-key
// batchKey is the idempotency key of a batch: round, index and the sha256 of the request body (which holds image
// sha256 handles, prompts and options). The same manifest and options give the same keys, so a repeated run replays
// finished items.
func batchKey(round, index int, body any) string {
	return fmt.Sprintf("prelabel-r%d-%04d-%s", round, index, digestOf(body)[:32])
}

// endregion

func localError(code, message string) *itemError { return &itemError{Code: code, Message: message} }

// batchEvent is the payload of an `item` event; progress events carry done and remaining.
type batchEvent struct {
	Index     int             `json:"index"`
	Status    string          `json:"status"`
	Result    json.RawMessage `json:"result"`
	Error     *itemError      `json:"error"`
	Done      int             `json:"done"`
	Remaining int             `json:"remaining"`
}

// runBatch sends one batch and collects an outcome per unit. A stream that ends without its `summary` event was cut:
// the same body is sent again under the same key, which replays what finished and runs the rest.
func runBatch(client *stackclient.Client, model string, units []*Unit, key, tag string, onOK func(*Unit, *outcome) error) (map[string]*outcome, error) {
	items := make([]any, len(units))
	for i, u := range units {
		items[i] = u.Body
	}
	body := map[string]any{"model": model, "items": items}
	out := map[string]*outcome{}
	for _, u := range units {
		out[u.ID()] = &outcome{Status: "no_result", Err: localError("no_result", "the stream ended before this item"), Calls: []stackclient.Row{}}
	}
	// region batch-run
	for attempt := 1; attempt <= streamAttempts; attempt++ {
		summary := false
		err := client.Batch("segment", body, stackclient.Opts{Key: key, Tag: tag}, func(ev stackclient.Event) error {
			var data batchEvent
			if err := json.Unmarshal(ev.Data, &data); err != nil {
				return err
			}
			switch ev.Name {
			case "progress":
				logf("  batch %s: %d done, %d remaining", tag, data.Done, data.Remaining)
			case "summary":
				summary = true
			case "item":
				unit := units[data.Index]
				cur := out[unit.ID()]
				if data.Status == "ok" {
					rows := client.Calls(tag) // the client records the item's ledger row just before it hands us the event
					cur.Calls = append(cur.Calls, rows[len(rows)-1])
					if cur.Result == nil { // a replay after a cut stream repeats items we already hold
						var res segResult
						if err := json.Unmarshal(data.Result, &res); err != nil {
							return err
						}
						cur.Status, cur.Result, cur.Err = "ok", &res, nil
						return onOK(unit, cur) // checkpoint the item now, not at the end of the batch
					}
				} else if cur.Result == nil {
					if data.Error != nil && data.Error.Code == "insufficient_balance" {
						return &stackclient.Error{Status: 402, Code: "insufficient_balance", Message: data.Error.Message}
					}
					e := data.Error
					if e == nil {
						e = localError("expired", "the batch time limit passed before this item finished")
					}
					cur.Status, cur.Err = data.Status, localError(e.Code, e.Message)
				}
			}
			return nil
		})
		var apiErr *stackclient.Error
		if errors.As(err, &apiErr) {
			switch {
			case apiErr.Code == "model_locked" || apiErr.Code == "budget_exceeded" || apiErr.Code == "insufficient_balance":
				return nil, err
			case !(apiErr.Retryable || apiErr.Code == "batch_timeout"):
				return nil, err
			}
			logf("  batch %s: %s, attempt %d of %d", tag, apiErr.Code, attempt, streamAttempts)
		} else if err != nil {
			return nil, err
		}
		if summary {
			break
		}
		if attempt < streamAttempts {
			time.Sleep(min(time.Duration(1<<attempt)*time.Second, 10*time.Second))
		}
	}
	// endregion
	return out, nil
}

func retryable(o *outcome) bool {
	return o.Status == "expired" || o.Status == "no_result" || (o.Status == "error" && retryableItemErrors[o.Err.Code])
}

// checkpoint is an append-only file with one JSON line per finished item. The last line of an item wins, and a line cut
// short by a crash is ignored (and cut off again) when the file is opened with resume.
type checkpoint struct {
	mu   sync.Mutex
	f    *os.File
	done map[string]checkpointLine
}

type checkpointLine struct {
	ID     string            `json:"id"`
	Digest string            `json:"digest"` // of the request body: an item whose request changed is asked again
	Result *segResult        `json:"result"`
	Calls  []stackclient.Row `json:"calls"`
}

// openCheckpoint opens `path`. Without resume the file starts empty.
func openCheckpoint(path string, resume bool) (*checkpoint, error) {
	c := &checkpoint{done: map[string]checkpointLine{}}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	flags, valid := os.O_CREATE|os.O_WRONLY|os.O_APPEND, int64(0)
	if resume {
		if data, err := os.ReadFile(path); err == nil {
			r := bufio.NewReader(bytes.NewReader(data))
			for {
				line, err := r.ReadBytes('\n')
				var l checkpointLine
				if err != nil || json.Unmarshal(line, &l) != nil || l.Result == nil {
					break // end of file, or a line that was cut short
				}
				c.done[l.ID] = l
				valid += int64(len(line))
			}
			if err := os.Truncate(path, valid); err != nil {
				return nil, err
			}
		}
	} else {
		flags |= os.O_TRUNC
	}
	f, err := os.OpenFile(path, flags, 0o644)
	c.f = f
	return c, err
}

func (c *checkpoint) Close() error { return c.f.Close() }

// lookup returns a finished item of an earlier run, when its request is unchanged. Its calls are reported as replayed.
func (c *checkpoint) lookup(u *Unit) *outcome {
	c.mu.Lock()
	defer c.mu.Unlock()
	l, ok := c.done[u.ID()]
	if !ok || l.Digest != u.Digest {
		return nil
	}
	calls := make([]stackclient.Row, len(l.Calls))
	for i, row := range l.Calls {
		row.Charged, row.Replayed = 0, true
		calls[i] = row
	}
	return &outcome{Status: "ok", Result: l.Result, Calls: calls}
}

// save appends the finished item and syncs the file, so a kill right after it loses nothing.
func (c *checkpoint) save(u *Unit, o *outcome) error {
	line, err := json.Marshal(checkpointLine{u.ID(), u.Digest, o.Result, o.Calls})
	if err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, err := c.f.Write(append(line, '\n')); err != nil {
		return err
	}
	return c.f.Sync()
}

// runRound sends `pending` as batches, up to cfg.Concurrency at a time. A cancelled context stops new batches from
// starting; a fatal error (tier, budget, balance) stops them too and is returned.
func runRound(ctx context.Context, client *stackclient.Client, cfg *config, pending []*Unit, round int, ckpt *checkpoint) (map[string]*outcome, error) {
	var chunks [][]*Unit
	for i := 0; i < len(pending); i += cfg.BatchSize {
		chunks = append(chunks, pending[i:min(i+cfg.BatchSize, len(pending))])
	}
	logf("round %d: %d items in %d batches (up to %d at once)", round, len(pending), len(chunks), cfg.Concurrency)
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var mu sync.Mutex
	var wg sync.WaitGroup
	var firstErr error
	outcomes := map[string]*outcome{}
	jobs := make(chan int)
	for w := 0; w < cfg.Concurrency; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for index := range jobs {
				if ctx.Err() != nil {
					continue // shutting down: leave the batch unsent, resume will pick it up
				}
				items := make([]any, len(chunks[index]))
				for i, u := range chunks[index] {
					items[i] = u.Body
				}
				key := batchKey(round, index, map[string]any{"model": cfg.Model, "items": items})
				got, err := runBatch(client, cfg.Model, chunks[index], key, fmt.Sprintf("r%db%d", round, index), ckpt.save)
				mu.Lock()
				if err != nil {
					if firstErr == nil {
						firstErr = err
					}
					cancel()
				}
				for id, o := range got {
					outcomes[id] = o
				}
				mu.Unlock()
			}
		}()
	}
	for index := range chunks {
		jobs <- index
	}
	close(jobs)
	wg.Wait()
	return outcomes, firstErr
}

// sleep waits for d or until ctx is cancelled; it reports whether the full time passed.
func sleep(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

// region requeue
// runAll runs every unit to an outcome: checkpoint (with --resume), then batches, then new rounds for what expired or
// failed. After a cancel the units that were never sent have no outcome in the result.
func runAll(ctx context.Context, client *stackclient.Client, units []*Unit, cfg *config, ckpt *checkpoint) (map[string]*outcome, error) {
	outcomes := map[string]*outcome{}
	if cfg.Resume {
		for _, u := range units {
			if done := ckpt.lookup(u); done != nil {
				outcomes[u.ID()] = done
			}
		}
		logf("resume: %d of %d items are already finished", len(outcomes), len(units))
	}
	pending := []*Unit{}
	for _, u := range units {
		if outcomes[u.ID()] == nil {
			pending = append(pending, u)
		}
	}
	for round := 1; round <= cfg.Rounds && len(pending) > 0 && ctx.Err() == nil; round++ {
		got, err := runRound(ctx, client, cfg, pending, round, ckpt)
		for id, o := range got {
			outcomes[id] = o
		}
		if err != nil {
			return outcomes, err
		}
		next := []*Unit{}
		for _, u := range pending { // expired or failed with a retryable code: next round
			if o := outcomes[u.ID()]; o == nil || retryable(o) {
				next = append(next, u)
			}
		}
		pending = next
		if len(pending) > 0 && round < cfg.Rounds {
			sleep(ctx, min(time.Duration(cfg.RetryWait*float64(round)*float64(time.Second)), 30*time.Second))
		}
	}
	return outcomes, nil
}

// endregion
