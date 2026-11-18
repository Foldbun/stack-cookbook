package main

// Embed a library through the API: batches or single calls, retries, and a ledger row per embedded image.
//
// Two modes, chosen by the client (client.Free(), true when there is no API key):
//
//   - With an API key an image is uploaded once and embedded by a batch request that refers to its handle, charged at
//     list price. Items of a batch run one after another on the server, so a batch is cut into chunks (50 by default):
//     a chunk finishes well inside the 600 s limit of a batch, and the index is committed after every chunk.
//   - Without a key (the free tier) there are no uploads, handles or batches: each image is sent inline as base64 in a
//     single /v1/embeddings call. The free tier is meant for trying the recipe, so a library of more than
//     freeMaxImages images needs a key. The client waits and retries on 429 by itself, so this code does not pace anything.
//
//   - With -async (a key is needed) nobody waits for the answer: the images are uploaded as before, then each model gets
//     one async job (at most jobItems images each) at a tenth of the price. The job runs on idle capacity with no promise
//     of when it ends; the code polls it, reads its results and commits them. An image whose item ended as an error,
//     expired or cancelled stays missing and is embedded by the next run, like a failed image of a batch.
//
// Either way the run can stop cleanly (the credit cap, or the free tier's daily limit): what was embedded is committed,
// the rest stays missing, and the same command run again continues where it stopped, because only missing images are
// embedded.

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
	"time"

	"stack-cookbook/05-image-library-search/stackclient"
)

// stopCodes mean there is no point in trying the next image.
var stopCodes = map[string]bool{"daily_limit_reached": true, "insufficient_balance": true, "budget_exceeded": true}

// retryCodes are item errors that may pass: the item goes into the next attempt.
var retryCodes = map[string]bool{"image_not_found": true, "model_busy": true, "model_unavailable": true, "unavailable": true, "upstream_error": true, "upstream_timeout": true, "internal_error": true, "rate_limited": true, "free_busy": true}

const attempts = 3 // a chunk is sent at most this many times; only the images that failed are sent again

// The library of the smoke run has 25 images, so 25 is also what the free tier accepts: 25 images x 2 models = 50 light-class
// calls. A bigger library is real work and needs a key (it would also run into the daily limit of the free tier).
const freeMaxImages = 25

const (
	jobItems    = 10000 // items one async job may hold
	pollSeconds = 30    // the pace the API documents for polling a job: every 30 seconds to a few minutes
)

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
	Embedded int
	Rows     map[string][]stackclient.Row
	Failed   map[string]ErrInfo // sha256 -> why
	Stopped  *Stopped           // nil unless the run stopped before the end
}

func newReport() *Report {
	return &Report{Rows: map[string][]stackclient.Row{}, Failed: map[string]ErrInfo{}}
}

// uploadFile uploads one file (free of charge) and returns its handle. The same bytes always give the same handle.
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
	} `json:"result"`
}

// region embed-chunk

// runBatch sends one batch request (needs a key). It returns the embeddings that succeeded and the sha256 to send again.
func runBatch(client *stackclient.Client, model string, shas, handles []string, attempt int, report *Report) ([]embedding, []string, error) {
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
		case data.Status == "expired" || (data.Error != nil && retryCodes[data.Error.Code]):
			retry = append(retry, sha) // not billed; goes into the next attempt (an evicted handle is uploaded again)
		case data.Error != nil && stopCodes[data.Error.Code]:
			report.Stopped = &Stopped{data.Error.Code, data.Error.Message, model}
			return stackclient.ErrStop // ending the stream stops the batch; the items that already finished are kept
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

// runSingles is the free tier: one /v1/embeddings call per image, the bytes inline as base64 (at most 4 MiB), at most
// workers calls at a time. It returns what runBatch returns.
func runSingles(client *stackclient.Client, model string, shas []string, paths map[string]string, report *Report, workers int) ([]embedding, []string, error) {
	type outcome struct {
		kind   string // ok, retry, stop or fail
		vector []float32
		row    stackclient.Row
		info   ErrInfo
		err    error // anything but an API error, or an API error that ends the run
	}
	outcomes, stopped := make([]outcome, len(shas)), make(chan struct{})
	var once sync.Once
	slots, wg := make(chan struct{}, max(workers, 1)), sync.WaitGroup{}
	for i, sha := range shas {
		wg.Add(1)
		slots <- struct{}{}
		go func() {
			defer func() { <-slots; wg.Done() }()
			select {
			case <-stopped:
				outcomes[i].kind = "retry" // another call already hit a limit: send nothing more
				return
			default:
			}
			data, err := os.ReadFile(paths[sha])
			if err != nil {
				outcomes[i].err = err
				return
			}
			tag := model + ":" + sha[:12]
			body := map[string]any{"model": model, "image": stackclient.ImageBase64(data)}
			raw, err := client.Post("/v1/embeddings", body, stackclient.Opts{Tag: tag}) // the client waits and retries 429 by itself
			var apiErr *stackclient.Error
			switch {
			case errors.As(err, &apiErr) && apiErr.Code != "model_locked" && apiErr.Code != "missing_api_key":
				kind := "fail"
				if stopCodes[apiErr.Code] {
					kind = "stop"
					once.Do(func() { close(stopped) })
				} else if retryCodes[apiErr.Code] {
					kind = "retry"
				}
				outcomes[i].kind, outcomes[i].info = kind, ErrInfo{apiErr.Code, apiErr.Message}
			case err != nil:
				outcomes[i].err = err
			default:
				var result struct {
					Embeddings []struct {
						Vector []float32 `json:"vector"`
					} `json:"embeddings"`
				}
				if err := json.Unmarshal(raw, &result); err != nil || len(result.Embeddings) == 0 {
					outcomes[i].err = errors.New("embeddings response without a vector")
					return
				}
				rows := client.Calls(tag)
				outcomes[i].kind, outcomes[i].vector, outcomes[i].row = "ok", result.Embeddings[0].Vector, rows[len(rows)-1]
			}
		}()
	}
	wg.Wait()
	var done []embedding
	var retry []string
	for i, sha := range shas {
		o := outcomes[i]
		switch {
		case o.err != nil:
			return nil, nil, o.err
		case o.kind == "ok":
			done = append(done, embedding{sha, o.vector})
			report.Rows[sha] = append(report.Rows[sha], o.row)
		case o.kind == "retry":
			retry = append(retry, sha)
		case o.kind == "stop":
			if report.Stopped == nil {
				report.Stopped = &Stopped{o.info.Code, o.info.Message, model}
			}
		default:
			report.Failed[sha] = o.info
		}
	}
	return done, retry, nil
}

// embedChunk embeds the images paths (sha256 -> file; order gives the batch order) with model, commits them, and
// records the outcome in report.
func embedChunk(client *stackclient.Client, index *LibraryIndex, model string, order []string, paths map[string]string, report *Report, workers int) error {
	todo := order
	for attempt := 0; attempt < attempts; attempt++ {
		if len(todo) == 0 || report.Stopped != nil {
			break
		}
		var done []embedding
		var retry []string
		var err error
		if client.Free() {
			done, retry, err = runSingles(client, model, todo, paths, report, workers)
		} else {
			var handles []string
			if handles, err = uploadAll(client, todo, paths, workers); err == nil { // free of charge; also uploads again what was evicted
				done, retry, err = runBatch(client, model, todo, handles, attempt, report)
			}
		}
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

// costLine is what the calls of this run cost at list price, and what was charged (nothing on the free tier).
func costLine(client *stackclient.Client) string {
	s := client.Summary()
	if client.Free() {
		return fmt.Sprintf("list price %g credits (free tier, not charged), in %d calls", s.ListTotal, s.Calls)
	}
	return fmt.Sprintf("cost: %g credits at list price, %g charged, in %d calls", s.ListTotal, s.ChargedTotal, s.Calls)
}

// checkFreeSize is for the free mode, which is meant for trying the recipe: a library of more than freeMaxImages
// images needs an API key. It returns an error with the code missing_api_key (exit code 2).
func checkFreeSize(client *stackclient.Client, images int) error {
	if client.Free() && images > freeMaxImages {
		return client.RequireKey(fmt.Sprintf("the free tier is meant for trying a recipe on a small library of up to %d images, and this one has %d", freeMaxImages, images))
	}
	return nil
}

// region async-jobs

// jobKey is the idempotency key of a job, derived from its content: running the same command again within 24 hours finds the same job.
func jobKey(model string, handles []string) string {
	sum := sha256.Sum256([]byte(strings.Join(append([]string{model}, handles...), "|")))
	return "job-" + hex.EncodeToString(sum[:])[:40]
}

type submittedJob struct {
	model string
	shas  []string
	job   stackclient.Job
}

// indexLibraryAsync embeds what is missing with one async job per model (more when a model has over jobItems images
// missing): upload, submit them all so they run side by side, wait for each, read the results by cursor and commit
// what arrived. It never stops a job early: it ends when every job has a final status.
func indexLibraryAsync(client *stackclient.Client, index *LibraryIndex, root string, models []string, workers int, poll time.Duration, log func(string)) (*Report, error) {
	if err := client.RequireKey("async jobs are not part of the free tier"); err != nil {
		return nil, err
	}
	report := newReport()
	paths := map[string]string{}
	for _, it := range index.Items {
		paths[it.Sha256] = filepath.Join(root, it.Path)
	}
	var submitted []submittedJob
submit:
	for _, model := range models {
		pending := index.Missing(model)
		log(fmt.Sprintf("%d images in the library, %d to embed with %s", len(index.Items), len(pending), model))
		for start := 0; start < len(pending); start += jobItems {
			shas := pending[start:min(start+jobItems, len(pending))]
			handles, err := uploadAll(client, shas, paths, workers) // free of charge
			if err != nil {
				return report, err
			}
			items := make([]map[string]any, len(handles))
			for i, h := range handles {
				items[i] = map[string]any{"image": stackclient.ImageHandle(h)}
			}
			job, err := client.CreateJob(model, items, map[string]string{"source": "cookbook-05"}, stackclient.Opts{Key: jobKey(model, handles)})
			var apiErr *stackclient.Error
			if errors.As(err, &apiErr) && (stopCodes[apiErr.Code] || apiErr.Retryable) {
				// not enough balance or credit cap, or too many jobs: keep the jobs that were submitted
				report.Stopped = &Stopped{apiErr.Code, apiErr.Message, model}
				break submit
			}
			if err != nil {
				return report, err
			}
			log(fmt.Sprintf("  submitted %s: %d images with %s, at most %g credits", job.ID, len(shas), model, job.Estimate))
			submitted = append(submitted, submittedJob{model, shas, job})
		}
	}
	for _, s := range submitted {
		job := s.job
		for !job.Final() {
			time.Sleep(poll)
			var err error
			if job, err = client.GetJob(job.ID); err != nil {
				return report, err
			}
			c := job.Counts
			log(fmt.Sprintf("  %s %s: %d/%d done, %d failed, cost %g", job.ID, job.Status, c.OK, c.Total, c.Error, job.Cost))
		}
		tag := s.model + ":" + job.ID
		var done []embedding
		var okShas []string
		err := client.JobResults(job.ID, stackclient.Opts{Tag: tag}, func(item stackclient.JobItem) error {
			sha := s.shas[item.Index]
			switch {
			case item.Status == "ok":
				var r struct {
					Embeddings []struct {
						Vector []float32 `json:"vector"`
					} `json:"embeddings"`
				}
				if err := json.Unmarshal(item.Result, &r); err != nil {
					return err
				}
				if len(r.Embeddings) == 0 {
					return errors.New("job item without an embedding")
				}
				done = append(done, embedding{sha, r.Embeddings[0].Vector})
				okShas = append(okShas, sha)
			case item.Status == "error" && item.Error != nil && stopCodes[item.Error.Code]:
				if report.Stopped == nil {
					report.Stopped = &Stopped{item.Error.Code, item.Error.Message, s.model}
				}
			case item.Error != nil: // not charged, and still missing, so the next run embeds it
				report.Failed[sha] = ErrInfo{item.Error.Code, item.Error.Message}
			default: // expired, cancelled or deleted
				report.Failed[sha] = ErrInfo{item.Status, "the item ended as " + item.Status}
			}
			return nil
		})
		if err != nil {
			return report, err
		}
		if rows := client.Calls(tag); len(rows) == len(okShas) {
			for i, sha := range okShas {
				report.Rows[sha] = append(report.Rows[sha], rows[i])
			}
		}
		if len(done) > 0 {
			shas, vectors := make([]string, len(done)), make([][]float32, len(done))
			for i, d := range done {
				shas[i], vectors[i] = d.sha, d.vector
			}
			if err := index.Add(s.model, shas, vectors); err != nil {
				return report, err
			}
			if err := index.Commit(); err != nil {
				return report, err
			}
			report.Embedded += len(done)
		}
		log(fmt.Sprintf("  %s: %d vectors from %s (%s)", s.model, len(done), job.ID, job.Status))
	}
	return report, nil
}

// endregion

// region index-library

// indexLibrary embeds every library image that is missing from the index, with every model, a chunk at a time.
func indexLibrary(client *stackclient.Client, index *LibraryIndex, root string, models []string, batchSize, workers int, log func(string)) (*Report, error) {
	if err := checkFreeSize(client, len(index.Items)); err != nil {
		return nil, err
	}
	report := newReport()
	paths := map[string]string{}
	for _, it := range index.Items {
		paths[it.Sha256] = filepath.Join(root, it.Path)
	}
	for _, model := range models {
		pending := index.Missing(model) // sha256 of the images that lack this model's vector, in catalogue order
		log(fmt.Sprintf("%d images in the library, %d to embed with %s", len(index.Items), len(pending), model))
		for start := 0; start < len(pending); start += batchSize {
			if report.Stopped != nil {
				return report, nil
			}
			chunk := pending[start:min(start+batchSize, len(pending))]
			if err := embedChunk(client, index, model, chunk, paths, report, workers); err != nil {
				return report, err
			}
			log(fmt.Sprintf("  %d/%d", min(start+batchSize, len(pending)), len(pending)))
		}
	}
	return report, nil
}

// endregion

// embedText embeds one query text. Up to 64 texts fit in one call; one per call keeps the cost per query.
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
