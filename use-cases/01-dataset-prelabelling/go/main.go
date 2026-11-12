// Pre-label an image dataset from text prompts with batch segmentation, route by score, export COCO and YOLO-seg.
//
//	go run . --manifest manifest.json --out out [--concurrency N] [--limit N] [--resume]
//	go run . ... --exemplar boxes.json                       find objects that are hard to name from one example box
//	go run . ... --boxes detections.csv --model sam-lite     turn detector boxes into masks
//
// Written the way a long-running batch job would be: progress is checkpointed after every finished image, and on
// SIGINT or SIGTERM the batches in flight finish, no new ones start, and the program writes what it has and exits
// with 0. `--resume` continues from there.
package main

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"stack-cookbook/01-dataset-prelabelling/stackclient"
)

const (
	exitFailed = 1
	exitNoKey  = 2
	exitTier   = 3
	exitBudget = 4
	exitUsage  = 2 // bad arguments; the Python version exits with 2 for these as well (argparse)
)

// config holds the command line.
type config struct {
	Manifest, Out      string
	Concurrency, Limit int
	Resume             bool
	BatchSize          int
	Accept, Review     float64
	Rounds             int
	RetryWait          float64
	Masks              bool
	Exemplar, Boxes    string
	Model, Mode        string // Mode: "text", "exemplar" or "boxes"
}

func (c *config) thresholds() thresholds {
	return thresholds{Accept: c.Accept, Review: c.Review, Boxes: c.Mode == "boxes"}
}

func parseArgs(args []string) (*config, error) {
	c := &config{}
	fs := flag.NewFlagSet("prelabel", flag.ContinueOnError)
	fs.StringVar(&c.Manifest, "manifest", "", "manifest.json of the run (required)")
	fs.StringVar(&c.Out, "out", "out", "output directory")
	fs.IntVar(&c.Concurrency, "concurrency", 2, "batches in flight at once; accounts may have 1 to 4 open batches")
	fs.IntVar(&c.Limit, "limit", 0, "only the first N manifest items (0: all)")
	fs.BoolVar(&c.Resume, "resume", false, "skip items finished in an earlier run of the same out directory")
	fs.IntVar(&c.BatchSize, "batch-size", 100, "images per batch request (at most 1000)")
	fs.Float64Var(&c.Accept, "accept", 0.6, "score from which a label is exported without review")
	fs.Float64Var(&c.Review, "review", 0.3, "lowest score kept; between this and --accept goes to the review queue")
	fs.IntVar(&c.Rounds, "rounds", 3, "how often expired or failed items are sent again")
	fs.Float64Var(&c.RetryWait, "retry-wait", 5, "seconds to wait before sending expired or failed items again")
	fs.BoolVar(&c.Masks, "masks", false, "also request pixel masks and export COCO RLE segmentations (keeps holes)")
	fs.StringVar(&c.Exemplar, "exemplar", "", `JSON {"class": "Bolt", "items": {"<id>": {"positive": [[x1,y1,x2,y2]], "negative": []}}}`)
	fs.StringVar(&c.Boxes, "boxes", "", "CSV with image_id,x1,y1,x2,y2[,label]: segment these boxes instead of searching by text")
	fs.StringVar(&c.Model, "model", "", "sam3 (default) or sam-lite (boxes only; the default with --boxes)")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	c.Mode = "text"
	if c.Exemplar != "" {
		c.Mode = "exemplar"
	} else if c.Boxes != "" {
		c.Mode = "boxes"
	}
	if c.Model == "" {
		c.Model = "sam3"
		if c.Mode == "boxes" {
			c.Model = "sam-lite"
		}
	}
	switch {
	case c.Manifest == "":
		return nil, errors.New("--manifest is required")
	case c.Exemplar != "" && c.Boxes != "":
		return nil, errors.New("--exemplar and --boxes exclude each other")
	case c.Model != "sam3" && c.Model != "sam-lite":
		return nil, errors.New("--model is sam3 or sam-lite")
	case c.Model == "sam-lite" && c.Mode != "boxes":
		return nil, errors.New("sam-lite takes boxes and points only: use it with --boxes")
	case c.Mode == "boxes" && c.Model != "sam-lite":
		return nil, errors.New("--boxes needs --model sam-lite")
	case !(0 <= c.Review && c.Review <= c.Accept && c.Accept <= 1) || c.BatchSize < 1 || c.BatchSize > 1000 || c.Concurrency < 1:
		return nil, errors.New("need 0 <= --review <= --accept <= 1, 1 <= --batch-size <= 1000 and --concurrency >= 1")
	}
	return c, nil
}

type manifest struct {
	Usecase string `json:"usecase"`
	Classes []struct {
		Name   string `json:"name"`
		Prompt string `json:"prompt"`
	} `json:"classes"`
	Items []manifestItem `json:"items"`
}

// classIndex maps prompts to class names, in a stable order.
type classIndex struct {
	ByPrompt map[string]string
	Prompts  []string // every prompt, in the order they were first seen
	Names    []string
}

// classNames lists the manifest's `classes`, then any prompt an item uses that is not listed.
func classNames(m manifest, items []manifestItem) classIndex {
	idx := classIndex{ByPrompt: map[string]string{}}
	for _, c := range m.Classes {
		idx.ByPrompt[c.Prompt] = c.Name
		idx.Prompts = append(idx.Prompts, c.Prompt)
		idx.Names = append(idx.Names, c.Name)
	}
	for _, it := range items {
		for _, p := range it.Prompts {
			if _, ok := idx.ByPrompt[p]; !ok {
				idx.ByPrompt[p] = p
				idx.Prompts = append(idx.Prompts, p)
				idx.Names = append(idx.Names, p)
			}
		}
	}
	return idx
}

// extra holds the input of the exemplar and boxes modes.
type extra struct {
	Class string `json:"class"`
	Items map[string]struct {
		Positive [][4]int `json:"positive"`
		Negative [][4]int `json:"negative"`
	} `json:"items"`
	Boxes map[string][]boxRow `json:"-"`
}

type boxRow struct {
	Box   [4]int
	Label string
}

func readExtra(c *config) (*extra, error) {
	ex := &extra{}
	switch c.Mode {
	case "exemplar":
		data, err := os.ReadFile(c.Exemplar)
		if err != nil {
			return nil, err
		}
		return ex, json.Unmarshal(data, ex)
	case "boxes":
		f, err := os.Open(c.Boxes)
		if err != nil {
			return nil, err
		}
		defer f.Close()
		r := csv.NewReader(f)
		head, err := r.Read()
		if err != nil {
			return nil, err
		}
		col := map[string]int{}
		for i, h := range head {
			col[h] = i
		}
		ex.Boxes = map[string][]boxRow{}
		for {
			rec, err := r.Read()
			if err == io.EOF {
				return ex, nil
			}
			if err != nil {
				return nil, err
			}
			row := boxRow{}
			for i, k := range []string{"x1", "y1", "x2", "y2"} {
				v, err := strconv.ParseFloat(rec[col[k]], 64)
				if err != nil {
					return nil, fmt.Errorf("%s: %w", c.Boxes, err)
				}
				row.Box[i] = int(v)
			}
			if i, ok := col["label"]; ok && i < len(rec) {
				row.Label = rec[i]
			}
			ex.Boxes[rec[col["image_id"]]] = append(ex.Boxes[rec[col["image_id"]]], row)
		}
	}
	return ex, nil
}

// region batch-request
// buildPrompts returns the API prompts of one image, and the class each prompt stands for.
func buildPrompts(u *Unit, c *config, classes classIndex, ex *extra) ([]map[string]any, []string) {
	prompts, labels := []map[string]any{}, []string{}
	switch c.Mode {
	case "exemplar":
		e, ok := ex.Items[u.ID()]
		if !ok || len(e.Positive) == 0 {
			return prompts, labels
		}
		for _, b := range e.Positive {
			prompts, labels = append(prompts, map[string]any{"box": b, "positive": true}), append(labels, ex.Class)
		}
		for _, b := range e.Negative {
			prompts, labels = append(prompts, map[string]any{"box": b, "positive": false}), append(labels, ex.Class)
		}
	case "boxes":
		for _, r := range ex.Boxes[u.ID()] {
			prompts, labels = append(prompts, map[string]any{"box": r.Box}), append(labels, r.Label)
		}
	default:
		texts := u.Item.Prompts
		if len(texts) == 0 {
			texts = classes.Prompts
		}
		for _, t := range texts {
			name, ok := classes.ByPrompt[t]
			if !ok {
				name = t
			}
			prompts, labels = append(prompts, map[string]any{"text": t}), append(labels, name)
		}
	}
	return prompts, labels
}

// buildBody is one batch item: the body of POST /v1/segment without `model`. The image is an uploaded handle.
func buildBody(u *Unit, c *config) map[string]any {
	geometry := []string{"polygon"}
	if c.Masks {
		geometry = append(geometry, "mask")
	}
	defaults := map[string]any{"geometry": geometry, "polygon": map[string]any{"tolerancePx": json.Number("1.0"), "holes": false}}
	options := map[string]any{"defaults": defaults}
	if c.Mode != "boxes" { // filters and overlap removal belong to search prompts; box prompts are one object each
		defaults["filter"] = map[string]any{"minScore": c.Review}                       // take everything from the review threshold up: low scores are routed, not lost
		options["nms"] = map[string]any{"iou": 0.7, "metric": "mask", "scope": "group"} // one object found twice under the same class
	}
	if c.Masks {
		options["maskSize"] = 4096 // the grid is never larger than the image, so masks come at image size
	}
	return map[string]any{"image": map[string]any{"id": u.Handle}, "prompts": u.Prompts, "options": options}
}

// endregion

// Result types: the shape of result.json.

type itemOutput struct {
	Image struct {
		Width  int    `json:"width"`
		Height int    `json:"height"`
		SHA256 string `json:"sha256"`
	} `json:"image"`
	Instances []outInstance `json:"instances"`
	Counts    struct {
		Accept int `json:"accept"`
		Review int `json:"review"`
	} `json:"counts"`
	Missing []string `json:"missing"`
}

type outInstance struct {
	Class string  `json:"class"`
	Score float64 `json:"score"`
	BBox  [4]int  `json:"bbox"`
	Area  int     `json:"area"`
	Route string  `json:"route"`
}

type itemResult struct {
	ID     string            `json:"id"`
	Status string            `json:"status"`
	Calls  []stackclient.Row `json:"calls"`
	Output *itemOutput       `json:"output"`
	Error  *itemError        `json:"error,omitempty"`
}

type runResult struct {
	Usecase   string              `json:"usecase"`
	Language  string              `json:"language"`
	Client    string              `json:"client"`
	StartedAt string              `json:"startedAt"`
	ElapsedMs int64               `json:"elapsedMs"`
	Items     []itemResult        `json:"items"`
	Ledger    stackclient.Summary `json:"ledger"`
}

// itemOutputOf is the `output` of result.json: what routing decided, without geometry (that goes to the export files).
func itemOutputOf(u *Unit, found []Detection, missing []string) *itemOutput {
	o := &itemOutput{Instances: []outInstance{}, Missing: missing}
	o.Image.Width, o.Image.Height, o.Image.SHA256 = u.Width, u.Height, u.SHA256
	for _, d := range found {
		o.Instances = append(o.Instances, outInstance{d.Class, round(d.Score, 4), d.BBox, d.Area, d.Route})
		if d.Route == "accept" {
			o.Counts.Accept++
		} else {
			o.Counts.Review++
		}
	}
	return o
}

func run(ctx context.Context, c *config) error {
	started := time.Now().UTC()
	raw, err := os.ReadFile(c.Manifest)
	if err != nil {
		return err
	}
	var m manifest
	if err := json.Unmarshal(raw, &m); err != nil {
		return fmt.Errorf("manifest: %w", err)
	}
	items := m.Items
	if c.Limit > 0 && c.Limit < len(items) {
		items = items[:c.Limit]
	}
	classes := classNames(m, items)
	ex, err := readExtra(c)
	if err != nil {
		return err
	}
	if c.Mode == "exemplar" && !contains(classes.Names, ex.Class) {
		classes.Names = append(classes.Names, ex.Class)
	}
	if c.Mode == "boxes" { // a box without a label belongs to the first class
		for id, rows := range ex.Boxes {
			for i := range rows {
				if rows[i].Label == "" && len(classes.Names) > 0 {
					rows[i].Label = classes.Names[0]
				}
			}
			ex.Boxes[id] = rows
		}
	}
	// region client
	client, err := stackclient.New(stackclient.Config{MaxConcurrency: c.Concurrency}) // FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
	if err != nil {
		return err
	}
	if _, err := client.Models(); err != nil { // free: checks the key
		return err
	}
	// endregion
	units, err := scan(items, filepath.Dir(c.Manifest))
	if err != nil {
		return err
	}
	if err := uploadAll(client, units, c.Concurrency); err != nil {
		return err
	}
	results, runnable := map[string]*outcome{}, []*Unit{}
	for _, u := range units {
		u.Prompts, u.Labels = buildPrompts(u, c, classes, ex)
		switch {
		case len(u.Prompts) == 0:
			results[u.ID()] = &outcome{Status: "error", Err: localError("no_prompts", "no prompts for this image in this mode"), Calls: []stackclient.Row{}}
		case len(u.Prompts) > maxPrompts:
			results[u.ID()] = &outcome{Status: "error", Err: localError("too_many_prompts", fmt.Sprintf("%d prompts; the API takes at most 50", len(u.Prompts))), Calls: []stackclient.Row{}}
		default:
			u.Body = buildBody(u, c)
			u.Digest = digestOf(u.Body)
			runnable = append(runnable, u)
		}
	}
	ckpt, err := openCheckpoint(filepath.Join(c.Out, "checkpoint.jsonl"), c.Resume)
	if err != nil {
		return err
	}
	defer ckpt.Close()
	done, err := runAll(ctx, client, runnable, c, ckpt)
	if err != nil {
		return err
	}
	for id, o := range done {
		results[id] = o
	}
	interrupted := 0

	entries, records := []itemResult{}, []record{}
	for _, u := range units {
		o := results[u.ID()]
		if o == nil { // never sent: the run was cancelled first
			o = &outcome{Status: "error", Err: localError("interrupted", "the run was stopped before this image was sent; run again with --resume"), Calls: []stackclient.Row{}}
			interrupted++
		}
		entry := itemResult{ID: u.ID(), Status: o.Status, Calls: o.Calls}
		if o.Status != "ok" && o.Status != "expired" {
			entry.Status = "error"
		}
		if o.Status == "ok" {
			found, missing := detections(u, o.Result, c.thresholds(), c.Masks)
			entry.Output = itemOutputOf(u, found, missing)
			records = append(records, record{u.ID(), u.Item.Image, u.Path, u.Width, u.Height, found, missing})
		} else if o.Err != nil {
			entry.Error = o.Err
		} else {
			entry.Error = localError("no_result", "no result")
		}
		entries = append(entries, entry)
	}

	// region export
	if err := os.MkdirAll(c.Out, 0o755); err != nil {
		return err
	}
	note := fmt.Sprintf("Pre-labels from text prompts, accepted at score >= %g", c.Accept)
	queued := 0
	for _, step := range []func() error{
		func() error {
			return writeCOCO(filepath.Join(c.Out, "coco.json"), records, classes.Names, "accept", note)
		},
		func() error {
			return writeCOCO(filepath.Join(c.Out, "coco-review.json"), records, classes.Names, "review", fmt.Sprintf("Detections for review, score %g to %g", c.Review, c.Accept))
		},
		func() error { return writeYOLO(filepath.Join(c.Out, "yolo"), records, classes.Names) },
		func() (err error) { queued, err = writeReview(filepath.Join(c.Out, "review"), records); return },
		func() error { return writeCosts(filepath.Join(c.Out, "costs.csv"), entries) },
	} {
		if err := step(); err != nil {
			return err
		}
	}
	// endregion

	elapsed := time.Since(started)
	ledger := ledgerOf(entries)
	result := runResult{
		Usecase:   m.Usecase,
		Language:  "go",
		Client:    "stackclient " + stackclient.Version,
		StartedAt: started.Format("2006-01-02T15:04:05Z"),
		ElapsedMs: elapsed.Milliseconds(),
		Items:     entries,
		Ledger:    ledger,
	}
	if err := writeJSON(filepath.Join(c.Out, "result.json"), result); err != nil {
		return err
	}
	ok, accepted := 0, 0
	for _, e := range entries {
		if e.Status == "ok" {
			ok++
			accepted += e.Output.Counts.Accept
		}
	}
	fmt.Printf("%d of %d images done in %.1f s (%.0f images/min)\n", ok, len(entries), elapsed.Seconds(), float64(ok)/math.Max(elapsed.Seconds(), 1e-9)*60)
	fmt.Printf("%d labels accepted, %d rows in the review queue, list price %g credits, charged %g\n", accepted, queued, ledger.ListTotal, ledger.ChargedTotal)
	if interrupted > 0 {
		fmt.Printf("stopped early: %d images were not sent; run again with --resume to finish them\n", interrupted)
	}
	return nil
}

// ledgerOf totals the calls of all items, resumed ones included; replayed rows are counted but add nothing.
func ledgerOf(entries []itemResult) stackclient.Summary {
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
	s.ListTotal, s.ChargedTotal = round(s.ListTotal, 6), round(s.ChargedTotal, 6)
	return s
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
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

func main() { os.Exit(realMain(os.Args[1:])) }

func realMain(args []string) int {
	c, err := parseArgs(args)
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitUsage
	}
	// The first SIGINT or SIGTERM cancels ctx: the batches in flight finish, nothing new starts. A second one kills the
	// program the usual way.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		sig := <-sigs
		signal.Stop(sigs)
		logf("%v: finishing the batches in flight, nothing new will start (send it again to quit at once)", sig)
		cancel()
	}()
	err = run(ctx, c)
	switch code := stackclient.Code(err); {
	case err == nil:
		return 0
	case code == "missing_api_key":
		fmt.Fprintln(os.Stderr, stackclient.MissingKey)
		return exitNoKey
	case code == "model_locked":
		fmt.Fprintln(os.Stderr, err)
		return exitTier
	case code == "budget_exceeded":
		fmt.Fprintln(os.Stderr, err)
		return exitBudget
	default:
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
}
