package main

// Embed a library through the API: batches, retries, the daily allowance, and a ledger row per embedded image.
//
// Two rules shape this file:
//
//   - Items of a batch run one after another on the server, so a batch is cut into chunks (50 by default). A chunk is
//     short enough to finish well inside the 600 s limit of a batch, and the index is committed after every chunk.
//   - The public pool includes 100 calls per UTC day, and every item of a batch is a call. When the allowance is used up
//     the run stops cleanly: what was embedded is committed, the rest stays missing, and the same command run after
//     00:00 UTC continues where it stopped, because only missing images are embedded.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"stack-cookbook/05-image-library-search/stackclient"
)

// stopCodes mean there is no point in trying the next image.
var stopCodes = map[string]bool{"daily_limit_reached": true, "insufficient_balance": true, "budget_exceeded": true}

// retryCodes are item errors that may pass: the item goes into the next attempt.
var retryCodes = map[string]bool{"image_not_found": true, "model_busy": true, "model_unavailable": true, "unavailable": true, "upstream_error": true, "upstream_timeout": true, "internal_error": true}

const attempts = 3 // a chunk is sent at most this many times; only the images that failed are sent again

// ErrInfo is the code and message of an item that failed, or of the reason a run stopped.
type ErrInfo struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Stopped is why a run ended before the end.
type Stopped struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Model   string `json:"model"`
}

// Report is what one indexing run did. Rows[sha256] holds the ledger rows of the calls that embedded that image.
type Report struct {
	Embedded  int
	Rows      map[string][]stackclient.Row
	Failed    map[string]ErrInfo        // sha256 -> why
	Stopped   *Stopped                  // nil unless the run stopped before the end
	Allowance map[string]map[string]any // model -> pool, used, limit as last seen in a response
}

func newReport() *Report {
	return &Report{Rows: map[string][]stackclient.Row{}, Failed: map[string]ErrInfo{}, Allowance: map[string]map[string]any{}}
}

// uploadFile uploads one file (free) and returns its handle. The same bytes always give the same handle.
func uploadFile(client *stackclient.Client, path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	ct := contentTypes[strings.ToLower(filepath.Ext(path))]
	if ct == "" {
		ct = "image/jpeg"
	}
	info, err := client.Upload(data, ct)
	return info.ID, err
}

// batchKey is the idempotency key of a batch, derived from its content: a rerun of an interrupted chunk replays what finished.
func batchKey(model string, handles []string, attempt int) string {
	sum := sha256.Sum256([]byte(strings.Join(append([]string{model, strconv.Itoa(attempt)}, handles...), "|")))
	return hex.EncodeToString(sum[:])[:40]
}

type embedding struct {
	sha    string
	vector []float32
}

// batchItem is the part of a batch `item` event that this program reads.
type batchItem struct {
	Index  int      `json:"index"`
	Status string   `json:"status"`
	Error  *ErrInfo `json:"error"`
	Result struct {
		Embeddings []struct {
			Vector []float32 `json:"vector"`
		} `json:"embeddings"`
		Billing *struct {
			Pool      string         `json:"pool"`
			Allowance map[string]any `json:"allowance"`
		} `json:"billing"`
	} `json:"result"`
}

// region embed-chunk

// runBatch sends one batch request. It returns the embeddings that succeeded and the sha256 to send again.
func runBatch(client *stackclient.Client, model string, shas, handles []string, attempt int, report *Report, freeOnly bool, keepFree float64) ([]embedding, []string, error) {
	tag := fmt.Sprintf("%s:%s:%d", model, shas[0][:12], attempt)
	items := make([]map[string]any, len(handles))
	for i, h := range handles {
		items[i] = map[string]any{"image": stackclient.ImageHandle(h)}
	}
	body := map[string]any{"model": model, "items": items}
	var done []embedding
	var retry []string
	err := client.Batch("embeddings", body, stackclient.Opts{Key: batchKey(model, handles, attempt), Tag: tag}, func(ev stackclient.Event) error {
		if ev.Name != "item" {
			return nil // progress heartbeats; the summary ends the stream
		}
		var data batchItem
		if err := json.Unmarshal(ev.Data, &data); err != nil {
			return err
		}
		sha := shas[data.Index]
		switch {
		case data.Status == "ok":
			if len(data.Result.Embeddings) == 0 {
				return errors.New("batch item without an embedding")
			}
			done = append(done, embedding{sha, data.Result.Embeddings[0].Vector})
			rows := client.Calls(tag)
			report.Rows[sha] = append(report.Rows[sha], rows[len(rows)-1]) // the ledger row of the item that just arrived
			if b := data.Result.Billing; b != nil && b.Allowance != nil {
				seen := map[string]any{"pool": b.Pool}
				for k, v := range b.Allowance {
					seen[k] = v
				}
				report.Allowance[model] = seen
				used, _ := b.Allowance["used"].(float64)
				limit, _ := b.Allowance["limit"].(float64)
				// On the public pool the next call would be refused. With freeOnly a funded account stops here too,
				// instead of paying for the calls that come after the included ones.
				if (b.Pool == "public" || freeOnly) && limit-used <= keepFree {
					report.Stopped = &Stopped{"daily_limit_reached", fmt.Sprintf("%g of %g included calls used today", used, limit), model}
					return stackclient.ErrStop // ending the stream stops the batch; the items that already finished are kept
				}
			}
		case data.Status == "expired" || (data.Error != nil && retryCodes[data.Error.Code]):
			retry = append(retry, sha) // not billed; goes into the next attempt (an evicted handle is uploaded again)
		case data.Error != nil && stopCodes[data.Error.Code]:
			report.Stopped = &Stopped{data.Error.Code, data.Error.Message, model}
			return stackclient.ErrStop
		case data.Error != nil:
			report.Failed[sha] = *data.Error // for example an image the API cannot read: no retry helps
		default:
			report.Failed[sha] = ErrInfo{"failed", "batch item " + data.Status}
		}
		return nil
	})
	var apiErr *stackclient.Error
	if errors.As(err, &apiErr) {
		if stopCodes[apiErr.Code] { // refused before the stream started, or by the credit cap
			report.Stopped = &Stopped{apiErr.Code, apiErr.Message, model}
		} else if !apiErr.Retryable {
			return nil, nil, err
		}
	} else if err != nil {
		return nil, nil, err
	}
	if report.Stopped == nil {
		// A stream can end early (a dropped connection, a timeout) without a summary event: whatever never got an
		// outcome is sent again. The finished items are kept, so nothing is embedded or paid twice.
		seen := map[string]bool{}
		for _, d := range done {
			seen[d.sha] = true
		}
		for _, s := range retry {
			seen[s] = true
		}
		for s := range report.Failed {
			seen[s] = true
		}
		for _, s := range shas {
			if !seen[s] {
				retry = append(retry, s)
			}
		}
	}
	return done, retry, nil
}

// embedChunk embeds the images paths (sha256 -> file; order gives the batch order) with model, commits them, and
// records the outcome in report.
func embedChunk(client *stackclient.Client, index *LibraryIndex, model string, order []string, paths map[string]string, report *Report, freeOnly bool, keepFree float64, workers int) error {
	todo := order
	for attempt := 0; attempt < attempts; attempt++ {
		if len(todo) == 0 || report.Stopped != nil {
			break
		}
		handles, err := uploadAll(client, todo, paths, workers) // free; also uploads again what was evicted
		if err != nil {
			return err
		}
		done, retry, err := runBatch(client, model, todo, handles, attempt, report, freeOnly, keepFree)
		if err != nil {
			return err
		}
		if len(done) > 0 {
			shas, vectors := make([]string, len(done)), make([][]float32, len(done))
			for i, d := range done {
				shas[i], vectors[i] = d.sha, d.vector
			}
			if err := index.Add(model, shas, vectors); err != nil {
				return err
			}
			if err := index.Commit(); err != nil {
				return err
			}
			report.Embedded += len(done)
		}
		todo = retry
	}
	if report.Stopped == nil {
		for _, sha := range todo {
			if _, ok := report.Failed[sha]; !ok {
				report.Failed[sha] = ErrInfo{"gave_up", fmt.Sprintf("not embedded after %d attempts", attempts)}
			}
		}
	}
	return nil
}

// uploadAll uploads the files of shas with at most workers uploads at a time and returns the handles in the same order.
func uploadAll(client *stackclient.Client, shas []string, paths map[string]string, workers int) ([]string, error) {
	handles, errs := make([]string, len(shas)), make([]error, len(shas))
	slots, wg := make(chan struct{}, max(workers, 1)), sync.WaitGroup{}
	for i, sha := range shas {
		wg.Add(1)
		slots <- struct{}{}
		go func() {
			defer func() { <-slots; wg.Done() }()
			handles[i], errs[i] = uploadFile(client, paths[sha])
		}()
	}
	wg.Wait()
	return handles, errors.Join(errs...)
}

// endregion

// region index-library

// indexLibrary embeds every library image that is missing from the index, with every model, a chunk at a time.
//
// Chunks alternate between the models, so that when the daily allowance ends the first part of the library has both
// vectors instead of one model having used all of it.
func indexLibrary(client *stackclient.Client, index *LibraryIndex, root string, models []string, batchSize int, freeOnly bool, keepFree float64, workers int, log func(string)) (*Report, error) {
	report := newReport()
	paths := map[string]string{}
	for _, it := range index.Items {
		paths[it.Sha256] = filepath.Join(root, it.Path)
	}
	// sha256 of every image that lacks a vector of some model, in catalogue order
	var pending []string
	queued := map[string]bool{}
	for _, model := range models {
		for _, sha := range index.Missing(model) {
			if !queued[sha] {
				queued[sha] = true
				pending = append(pending, sha)
			}
		}
	}
	log(fmt.Sprintf("%d images in the library, %d to embed", len(index.Items), len(pending)))
	for start := 0; start < len(pending); start += batchSize {
		chunk := pending[start:min(start+batchSize, len(pending))]
		for _, model := range models {
			var need []string
			for _, sha := range chunk {
				if !index.Has(model, sha) {
					need = append(need, sha)
				}
			}
			if len(need) > 0 && report.Stopped == nil {
				if err := embedChunk(client, index, model, need, paths, report, freeOnly, keepFree, workers); err != nil {
					return report, err
				}
			}
		}
		log(fmt.Sprintf("  %d/%d", min(start+batchSize, len(pending)), len(pending)))
		if report.Stopped != nil {
			break
		}
	}
	return report, nil
}

// endregion

// embedText embeds one query text. Up to 64 texts fit in one call (and count as one call of the daily allowance); one
// per call keeps the cost per query.
func embedText(client *stackclient.Client, text, model, tag string) ([]float32, stackclient.Row, error) {
	raw, err := client.Post("/v1/embeddings", map[string]any{"model": model, "texts": []string{text}}, stackclient.Opts{Tag: tag})
	if err != nil {
		return nil, stackclient.Row{}, err
	}
	var result struct {
		Embeddings []struct {
			Vector []float32 `json:"vector"`
		} `json:"embeddings"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return nil, stackclient.Row{}, err
	}
	if len(result.Embeddings) == 0 {
		return nil, stackclient.Row{}, errors.New("embeddings response without a vector")
	}
	rows := client.Calls(tag)
	return result.Embeddings[0].Vector, rows[len(rows)-1], nil
}
