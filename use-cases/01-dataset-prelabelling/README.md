# Pre-label an image dataset from text prompts

English | [简体中文](README.zh-CN.md)

Segment a folder of images in batches with `sam3` from text prompts, export the confident labels as COCO and YOLO-seg, and put everything else in a review queue with crops. Runs can be resumed, repeated without paying twice, and every image has a cost row.

## Problem <!-- #problem -->

A new detection or segmentation dataset needs labels, and drawing them from scratch is the expensive part. A text prompt such as `dog` finds most instances for a few hundredths of a credit, but the answer is a guess with a score, and a pre-labelled dataset is only useful if three things hold:

- the confident labels can be used as they are, in the formats training code reads (COCO, YOLO-seg);
- the uncertain ones are not lost and not silently accepted: a person looks at them, quickly, with a crop;
- a run over thousands of images survives a dropped connection, an expired batch or a restart without paying for the same image twice.

This example is that workflow for the 60 images, 4 classes (Dog, Cat, Bird, Boat) of the dataset in `datasets/`. Four of the images are committed as the smoke set.

## Pipeline <!-- #pipeline -->

```
 images/ + manifest.json
        |  sha256 of every file
        v
 POST /v1/images           free; handle = img_<sha256>, so repeats cost nothing
        |
        v   batches of 100, up to 2 in flight, key = round + index + sha256 of the request body
 POST /v1/batch/segment    4 text prompts per image, filter.minScore 0.3, event stream
        |                  expired / retryable items -> next round, cut stream -> same key again
        v
 routing by score
   >= 0.6          accept  ----> coco.json, yolo/labels/*.txt + data.yaml
   0.3 .. 0.6      review  ----> coco-review.json, review/queue.csv + review/crops/*.jpg
   class expected, nothing found -> review (reason empty_but_expected)
        |
        v
 result.json (routing, calls)      costs.csv (one row per image)
```

1. Scan the manifest and compute the sha256 of every file; refuse a file that does not match the manifest.
2. Upload each distinct file once. This is free, and the handle is derived from the file contents, so uploading again is harmless.
3. Cut the images into batches of at most 100 and send each as one `POST /v1/batch/segment` event stream. A batch takes one rate-limit token; the images in it run one after another on the server.
4. Each image carries 4 text prompts (`dog`, `cat`, `bird`, `boat`). `filter.minScore` is the review threshold (0.3), so the API returns everything a reviewer might want and nothing below it.
5. Route every instance by score, and every expected-but-empty class by the manifest's `expectedClasses`.
6. Export what other tools read, and write the cost of every image.

Two more modes use the same pipeline. `--exemplar` is for a class that text does not describe: draw one box around an example in an image, and the model finds more like it in that image. `--boxes` with `--model sam-lite` turns the boxes of an existing detector into masks.

## Run it <!-- #run -->

```bash
export FOLDBUN_API_KEY="fbs_..."
cd use-cases/01-dataset-prelabelling/python
uv run main.py --manifest ../../../datasets/smoke/01-dataset-prelabelling/manifest.json --out out
```

The account needs a balance, because `sam3` is billed from it. The smoke set costs 2.8 credits at list price. Set `FOLDBUN_MAX_CREDITS` to make the client refuse any call that could pass your own limit.

| Option | Default | Meaning |
|---|---|---|
| `--concurrency N` | 2 | batches in flight at once; an account may have 1 to 4 open batches, and a `429 too_many_batches` is waited out |
| `--batch-size N` | 100 | images per batch request (the API allows 1,000 items and 600 s per event stream) |
| `--accept` / `--review` | 0.6 / 0.3 | accept from this score; between the two goes to review; below `--review` is not requested |
| `--rounds N` | 3 | how often expired or failed items are sent again |
| `--resume` | off | skip images finished by an earlier run in the same `--out` directory |
| `--limit N` | all | only the first N manifest items |
| `--masks` | off | also request masks; COCO segmentations become RLE and keep holes |
| `--exemplar FILE` | | exemplar mode, see below |
| `--boxes FILE --model sam-lite` | | box mode, see below |

Besides `id` and `image`, a manifest item may carry `prompts` (default: the prompts of the manifest's `classes`), `sha256` and `expectedClasses`, the image-level labels you already have (for example from folder names). An expected class with no detection becomes a review row.

Files written to `--out`:

| File | Content |
|---|---|
| `result.json` | per image: status, routing result, ledger rows (`contract/result.schema.json`) |
| `coco.json` | accepted labels, COCO instances format, one `score` per annotation |
| `coco-review.json` | the review detections in the same format, for tools that import pre-annotations |
| `yolo/labels/*.txt`, `yolo/data.yaml` | accepted labels as YOLO segmentation; link or copy the images to `yolo/images/` |
| `review/queue.csv`, `review/crops/` | one row and one JPEG crop per label to check |
| `costs.csv` | list price, charged credits, cache, timing and counts per image |
| `raw/` | the answer of every finished image; `--resume` reads it |

Exemplar mode takes a JSON file with one example per image. Images without an entry are not sent. Positive boxes are examples of the class, negative boxes are look-alikes to leave out; the boxes add no charge, and an image costs one prompt.

```json
{ "class": "Bird", "items": { "16774edaeacc5aed": { "positive": [[162, 121, 894, 685]], "negative": [] } } }
```

Box mode takes a CSV of boxes `x1,y1,x2,y2` in image pixels, with the class in `label`. Each box is one object prompt for `sam-lite`, at 0.01 credits per box plus the image encoding.

```bash
uv run main.py --manifest ... --out out-boxes --boxes detections.csv --model sam-lite
```

The full set is a release asset. Extract `01-dataset-prelabelling-full.tar.gz` into `assets/` (see `assets/full.json`) and pass `assets/full.json`; `evaluate.py` then scores the run against `assets/truth.json`.

### The Go version

`go/` produces the same `result.json`, exports and `costs.csv` from the same arguments, and is written the way a long-running batch job would be. The Python version keeps one file per image in `raw/`; the Go version differs in three ways:

- **Checkpoint file.** `checkpoint.jsonl` in `--out` gets one line per finished image, written and synced as soon as the image arrives rather than at the end of the batch. `--resume` reads it, skips images whose request is unchanged and reports their calls as replayed. A last line cut short by a crash is ignored.
- **Context cancellation.** The whole run takes a `context.Context`. Once it is cancelled no new batch or round starts, and waits between rounds end at once.
- **Stopping on SIGINT or SIGTERM.** The batches in flight finish and are checkpointed, the exports and `result.json` are written with what exists, and the program exits with 0. Images that were never sent have the error code `interrupted`; run again with `--resume` to finish them. A second signal ends the program immediately.

```bash
cd use-cases/01-dataset-prelabelling/go
go run . --manifest ../../../datasets/smoke/01-dataset-prelabelling/manifest.json --out out
```

The standard library is the only dependency, so review crops are written for JPEG and PNG images (EXIF orientation is applied); an image in another format is segmented as usual, and its review rows have an empty `crop`. `go test ./...` covers the routing thresholds, the half-pixel polygon shift, batch key derivation and the checkpoint round trip.

## Code <!-- #code -->

The files are small and meant to be copied: `main.py` (arguments, wiring, `result.json`), `batches.py` (scan, upload, batches, resume), `routing.py` (score bands), `exports.py` (COCO, YOLO, review queue, costs), plus the two client files. The Go version follows the same split (`main.go`, `batches.go`, `routing.go`, `exports.go`) and adds `crop.go` for the review crops.

The client reads the key and base URL from the environment and never prints the key:

<!-- code: client -->
<details><summary>Python (python/main.py)</summary>

```python
client = sc.StackClient(max_concurrency=cfg.concurrency)  # FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
client.models()  # free: checks the key
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
client, err := stackclient.New(stackclient.Config{MaxConcurrency: c.Concurrency}) // FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
if err != nil {
	return err
}
if _, err := client.Models(); err != nil { // free: checks the key
	return err
}
```

</details>
<!-- /code -->

Every file is hashed before anything is sent. The manifest's own `sha256`, when present, is checked, so a changed image fails early instead of producing labels for the wrong file:

<!-- code: scan -->
<details><summary>Python (python/batches.py)</summary>

```python
def scan(items, base):
    """Read every image once: sha256 (checked against the manifest when it has one), size and content type."""
    units = []
    for item in items:
        path = base / item["image"]
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if item.get("sha256") and item["sha256"] != digest:
            raise ValueError(f"{item['id']}: sha256 of {item['image']} does not match the manifest")
        units.append(Unit(item, path, digest, len(data)))
    return units
```

</details>

<details><summary>Go (go/batches.go)</summary>

```go
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
```

</details>
<!-- /code -->

Uploads are free, and the handle is `img_` plus the sha256 of the image. The same file uploaded twice is one handle:

<!-- code: upload -->
<details><summary>Python (python/batches.py)</summary>

```python
def upload_all(client, units, workers):
    """Upload every distinct file once (free). The handle is `img_<sha256>`, so repeated files share one upload."""
    by_sha = {}
    for u in units:
        by_sha.setdefault(u.sha256, []).append(u)

    def one(group):
        first = group[0]
        info = client.upload(first.path.read_bytes(), CONTENT_TYPES.get(first.path.suffix.lower(), "image/jpeg"))
        for u in group:
            u.handle, u.width, u.height = info["id"], info["width"], info["height"]

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, by_sha.values()))
```

</details>

<details><summary>Go (go/batches.go)</summary>

```go
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
```

</details>
<!-- /code -->

One image becomes one batch item. The prompts are the classes; `minScore` is the review threshold; overlap removal stays inside a class; polygons come without holes because COCO polygons cannot carry them:

<!-- code: batch-request -->
<details><summary>Python (python/main.py)</summary>

```python
def build_prompts(unit, cfg, by_prompt, extra):
    """The API prompts of one image, and the class each prompt stands for."""
    if cfg.mode == "exemplar":
        ex = extra["items"].get(unit.id)
        if not ex or not ex.get("positive"):
            return [], []
        boxes = [{"box": b, "positive": True} for b in ex["positive"]] + [{"box": b, "positive": False} for b in ex.get("negative", [])]
        return boxes, [extra["class"]] * len(boxes)
    if cfg.mode == "boxes":
        rows = extra.get(unit.id, [])
        return [{"box": r["box"]} for r in rows], [r["label"] for r in rows]
    texts = unit.item.get("prompts") or list(by_prompt)
    return [{"text": t} for t in texts], [by_prompt.get(t, t) for t in texts]


def build_body(unit, cfg):
    """One batch item: the body of POST /v1/segment without `model`. The image is an uploaded handle."""
    defaults = {"geometry": ["polygon", "mask"] if cfg.masks else ["polygon"], "polygon": {"tolerancePx": 1.0, "holes": False}}
    options = {"defaults": defaults}
    if cfg.mode != "boxes":  # filters and overlap removal belong to search prompts; box prompts are one object each
        defaults["filter"] = {"minScore": cfg.review}  # take everything from the review threshold up: low scores are routed, not lost
        options["nms"] = {"iou": 0.7, "metric": "mask", "scope": "group"}  # one object found twice under the same class
    if cfg.masks:
        options["maskSize"] = 4096  # the grid is never larger than the image, so masks come at image size
    return {"image": {"id": unit.handle}, "prompts": unit.prompts, "options": options}
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
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
```

</details>
<!-- /code -->

The idempotency key of a batch is made of the round, the index and the sha256 of the request body. The same manifest and options give the same keys, so a repeat replays what finished (`replayed: true`, not charged). Changing an option changes the body and the key, which is what keeps a changed item from colliding with an old one (`idempotency_conflict`):

<!-- code: batch-key -->
<details><summary>Python (python/batches.py)</summary>

```python
def batch_key(round_no, index, body):
    """Idempotency key of a batch: round, index and the sha256 of the request body (which holds image sha256 handles,
    prompts and options). The same manifest and options give the same keys, so a repeated run replays finished items."""
    digest = hashlib.sha256(canonical(body).encode()).hexdigest()
    return f"prelabel-r{round_no}-{index:04d}-{digest[:32]}"
```

</details>

<details><summary>Go (go/batches.go)</summary>

```go
// batchKey is the idempotency key of a batch: round, index and the sha256 of the request body (which holds image
// sha256 handles, prompts and options). The same manifest and options give the same keys, so a repeated run replays
// finished items.
func batchKey(round, index int, body any) string {
	return fmt.Sprintf("prelabel-r%d-%04d-%s", round, index, digestOf(body)[:32])
}
```

</details>
<!-- /code -->

The batch loop reads the event stream. A stream that ends without its `summary` was cut, so the same body is sent again under the same key. Each `ok` item gets the ledger row the client has just written; items that came back `expired` or `error` are recorded as such:

<!-- code: batch-run -->
<details><summary>Python (python/batches.py)</summary>

```python
for attempt in range(1, STREAM_ATTEMPTS + 1):
    summary = None
    try:
        for event, data in client.batch("segment", body, key, tag=tag):
            if event == "progress":
                log(f"  batch {tag}: {data['done']} done, {data['remaining']} remaining")
            elif event == "summary":
                summary = data
            elif event == "item":
                unit = units[data["index"]]
                cur = out[unit.id]
                if data["status"] == "ok":
                    row = client.calls(tag)[-1]  # the client records the item's ledger row just before it yields the event
                    cur["calls"].append(row)
                    if cur["result"] is None:  # a replay after a cut stream repeats items we already hold
                        cur.update(status="ok", result=data["result"], error=None)
                        on_ok(unit, cur)  # checkpoint the item now, not at the end of the batch
                elif cur["result"] is None:
                    if (data.get("error") or {}).get("code") == "insufficient_balance":
                        raise sc.StackError(402, "insufficient_balance", data["error"]["message"])
                    err = data.get("error") or {"code": "expired", "message": "the batch time limit passed before this item finished"}
                    cur.update(status=data["status"], error=local_error(err["code"], err["message"]))
    except sc.StackError as err:
        if err.code in ("model_locked", "budget_exceeded", "insufficient_balance") or not (err.retryable or err.code == "batch_timeout"):
            raise
        log(f"  batch {tag}: {err.code}, attempt {attempt} of {STREAM_ATTEMPTS}")
    if summary is not None:
        break
    time.sleep(min(2**attempt, 10))
```

</details>

<details><summary>Go (go/batches.go)</summary>

```go
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
```

</details>
<!-- /code -->

Rounds: images that expired or failed with a retryable code go into new batches (new body, new key). Non-retryable errors, such as `invalid_image`, stay as errors and are listed in `result.json`:

<!-- code: requeue -->
<details><summary>Python (python/batches.py)</summary>

```python
def run_all(client, units, cfg, raw_dir):
    """Run every unit to an outcome: checkpoint (with --resume), then batches, then new rounds for what expired or failed."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    outcomes = {}
    if cfg.resume:
        for u in units:
            done = load_checkpoint(raw_dir, u)
            if done:
                outcomes[u.id] = done
        log(f"resume: {len(outcomes)} of {len(units)} items are already finished")
    pending = [u for u in units if u.id not in outcomes]
    for round_no in range(1, cfg.rounds + 1):
        if not pending:
            break
        outcomes.update(run_round(client, cfg, pending, round_no, raw_dir))
        pending = [u for u in pending if retryable(outcomes[u.id])]  # expired or failed with a retryable code: next round
        if pending and round_no < cfg.rounds:
            time.sleep(min(cfg.retry_wait * round_no, 30))
    return outcomes
```

</details>

<details><summary>Go (go/batches.go)</summary>

```go
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
```

</details>
<!-- /code -->

Routing is a few lines. The thresholds are options because the right ones depend on the classes:

<!-- code: routing -->
<details><summary>Python (python/routing.py)</summary>

```python
def route(score, cfg):
    """The band of a score: "accept", "review", or None when it is below the review threshold."""
    if score >= cfg.accept:
        return "accept"
    if score >= cfg.review or cfg.mode == "boxes":  # a box the user supplied is never dropped, only checked
        return "review"
    return None


def detections(unit, result, cfg):
    """Detections of one item (sorted by score), and the expected classes for which nothing was found."""
    found = []
    for inst in result["instances"]:
        band = route(inst["score"], cfg)
        if band is None:
            continue
        idx = inst.get("prompt", 0)
        rings = [part["outer"] for part in inst.get("polygon", [])]
        found.append(Detection(unit.labels[idx], inst["score"], inst["bbox"], inst["area"], band, rings, inst.get("mask") if cfg.masks else None, idx))
    found.sort(key=lambda d: (-d.score, d.bbox))
    seen = {d.cls for d in found}
    missing = [c for c in unit.item.get("expectedClasses", []) if c not in seen]
    return found, missing
```

</details>

<details><summary>Go (go/routing.go)</summary>

```go
// route is the band of a score: "accept", "review", or "" when it is below the review threshold.
func route(score float64, t thresholds) string {
	if score >= t.Accept {
		return "accept"
	}
	if score >= t.Review || t.Boxes {
		return "review"
	}
	return ""
}

// detections returns the detections of one item (sorted by score), and the expected classes for which nothing was found.
func detections(u *Unit, res *segResult, t thresholds, masks bool) ([]Detection, []string) {
	found := []Detection{}
	for _, inst := range res.Instances {
		band := route(inst.Score, t)
		if band == "" {
			continue
		}
		d := Detection{Class: u.Labels[inst.Prompt], Score: inst.Score, BBox: inst.BBox, Area: inst.Area, Route: band}
		for _, part := range inst.Polygon {
			d.Polygon = append(d.Polygon, part.Outer)
		}
		if masks {
			d.Mask = inst.Mask
		}
		found = append(found, d)
	}
	sort.SliceStable(found, func(i, j int) bool {
		a, b := found[i], found[j]
		if a.Score != b.Score {
			return a.Score > b.Score
		}
		return lessBox(a.BBox, b.BBox)
	})
	seen := map[string]bool{}
	for _, d := range found {
		seen[d.Class] = true
	}
	missing := []string{}
	for _, c := range u.Item.ExpectedClasses {
		if !seen[c] {
			missing = append(missing, c)
		}
	}
	return found, missing
}
```

</details>
<!-- /code -->

The polygon vertices of the API are pixel centres, so every exported vertex gets +0.5 to land on the pixel grid that COCO and YOLO use. Boxes are already pixel edges. COCO `bbox` is `[x, y, width, height]`:

<!-- code: coco-export -->
<details><summary>Python (python/exports.py)</summary>

```python
def coco_annotation(ann_id, image_id, cat_id, det, width, height):
    x1, y1, x2, y2 = det.bbox
    if det.mask:  # --masks: a COCO RLE at the size of the image (the mask grid can be smaller)
        rle = det.mask if det.mask["size"] == [height, width] else coco_rle.resize(det.mask, height, width)
        segmentation, area = {"size": rle["size"], "counts": rle["counts"]}, coco_rle.area(rle)
    else:
        segmentation, area = [grid_ring(r) for r in det.polygon], det.area
    return {
        "id": ann_id,
        "image_id": image_id,
        "category_id": cat_id,
        "bbox": [x1, y1, x2 - x1, y2 - y1],  # [x, y, width, height]
        "area": area,
        "segmentation": segmentation,
        "iscrowd": 0,
        "score": round(det.score, 4),
    }


def write_coco(path, records, classes, band, description):
    """COCO instances file of the detections routed to `band`. Every image is listed, also those without annotations."""
    cats = {name: i + 1 for i, name in enumerate(classes)}
    doc = {
        "info": {"description": description, "version": "1.0", "date_created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
        "licenses": [],
        "images": [],
        "categories": [{"id": i, "name": name} for name, i in cats.items()],
        "annotations": [],
    }
    for n, rec in enumerate(records, 1):
        doc["images"].append({"id": n, "file_name": rec["file"], "width": rec["width"], "height": rec["height"]})
        for det in rec["detections"]:
            if det.route == band and (det.polygon or det.mask):
                doc["annotations"].append(coco_annotation(len(doc["annotations"]) + 1, n, cats[det.cls], det, rec["width"], rec["height"]))
    path.write_text(json.dumps(doc, indent=1) + "\n")
```

</details>

<details><summary>Go (go/exports.go)</summary>

```go
type cocoAnnotation struct {
	ID           int     `json:"id"`
	ImageID      int     `json:"image_id"`
	CategoryID   int     `json:"category_id"`
	BBox         [4]int  `json:"bbox"` // [x, y, width, height]
	Area         int     `json:"area"`
	Segmentation any     `json:"segmentation"`
	IsCrowd      int     `json:"iscrowd"`
	Score        float64 `json:"score"`
}

func cocoAnn(id, imageID, catID int, d Detection, width, height int) (cocoAnnotation, error) {
	x1, y1, x2, y2 := d.BBox[0], d.BBox[1], d.BBox[2], d.BBox[3]
	a := cocoAnnotation{ID: id, ImageID: imageID, CategoryID: catID, BBox: [4]int{x1, y1, x2 - x1, y2 - y1}, Score: round(d.Score, 4)}
	if d.Mask != nil { // --masks: a COCO RLE at the size of the image (the mask grid can be smaller)
		m, err := fitMask(*d.Mask, height, width)
		if err != nil {
			return a, err
		}
		area, err := rleArea(m)
		if err != nil {
			return a, err
		}
		a.Segmentation, a.Area = map[string]any{"size": m.Size, "counts": m.Counts}, area
	} else {
		rings := [][]float64{}
		for _, r := range d.Polygon {
			rings = append(rings, gridRing(r))
		}
		a.Segmentation, a.Area = rings, d.Area
	}
	return a, nil
}

// writeCOCO writes the COCO instances file of the detections routed to `band`. Every image is listed, also those
// without annotations.
func writeCOCO(path string, records []record, classes []string, band, description string) error {
	type image struct {
		ID       int    `json:"id"`
		FileName string `json:"file_name"`
		Width    int    `json:"width"`
		Height   int    `json:"height"`
	}
	type category struct {
		ID   int    `json:"id"`
		Name string `json:"name"`
	}
	doc := struct {
		Info        map[string]string `json:"info"`
		Licenses    []string          `json:"licenses"`
		Images      []image           `json:"images"`
		Categories  []category        `json:"categories"`
		Annotations []cocoAnnotation  `json:"annotations"`
	}{
		Info:        map[string]string{"description": description, "version": "1.0", "date_created": time.Now().UTC().Format("2006-01-02T15:04:05Z")},
		Licenses:    []string{},
		Images:      []image{},
		Categories:  []category{},
		Annotations: []cocoAnnotation{},
	}
	cats := map[string]int{}
	for i, name := range classes {
		cats[name] = i + 1
		doc.Categories = append(doc.Categories, category{i + 1, name})
	}
	for n, rec := range records {
		doc.Images = append(doc.Images, image{n + 1, rec.File, rec.Width, rec.Height})
		for _, d := range rec.Detections {
			if d.Route != band || (len(d.Polygon) == 0 && d.Mask == nil) {
				continue
			}
			a, err := cocoAnn(len(doc.Annotations)+1, n+1, cats[d.Class], d, rec.Width, rec.Height)
			if err != nil {
				return err
			}
			doc.Annotations = append(doc.Annotations, a)
		}
	}
	data, err := json.MarshalIndent(doc, "", " ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0o644)
}
```

</details>
<!-- /code -->

YOLO-seg writes one normalised polygon per line, one line per part. An image without labels gets an empty file, which YOLO reads as a background image:

<!-- code: yolo-export -->
<details><summary>Python (python/exports.py)</summary>

```python
def write_yolo(root, records, classes):
    """YOLO segmentation labels of the accepted detections: `labels/<image>.txt`, one line per polygon part:
    `class x1 y1 x2 y2 ...` normalised to 0..1, and `data.yaml`. An image without detections gets an empty file."""
    labels = root / "labels"
    labels.mkdir(parents=True, exist_ok=True)
    for rec in records:
        lines = []
        for det in rec["detections"]:
            if det.route != "accept":
                continue
            for ring in det.polygon:
                pts = grid_ring(ring)
                norm = [min(1.0, max(0.0, v / (rec["width"] if i % 2 == 0 else rec["height"]))) for i, v in enumerate(pts)]
                lines.append(f"{classes.index(det.cls)} " + " ".join(f"{v:.6f}" for v in norm))
        (labels / (Path(rec["file"]).stem + ".txt")).write_text("\n".join(lines) + ("\n" if lines else ""))
    names = "\n".join(f"  {i}: {json.dumps(name)}" for i, name in enumerate(classes))
    (root / "data.yaml").write_text(
        f"# Put or link the images in yolo/images; labels/ holds the matching .txt files.\npath: .\ntrain: images\nval: images\nnames:\n{names}\n"
    )
```

</details>

<details><summary>Go (go/exports.go)</summary>

```go
// writeYOLO writes the YOLO segmentation labels of the accepted detections: `labels/<image>.txt`, one line per polygon
// part: `class x1 y1 x2 y2 ...` normalised to 0..1, and `data.yaml`. An image without detections gets an empty file.
func writeYOLO(root string, records []record, classes []string) error {
	if err := os.MkdirAll(filepath.Join(root, "labels"), 0o755); err != nil {
		return err
	}
	index := map[string]int{}
	for i, name := range classes {
		index[name] = i
	}
	for _, rec := range records {
		var lines strings.Builder
		for _, d := range rec.Detections {
			if d.Route != "accept" {
				continue
			}
			for _, ring := range d.Polygon {
				fmt.Fprintf(&lines, "%d", index[d.Class])
				for i, v := range gridRing(ring) {
					size := rec.Height
					if i%2 == 0 {
						size = rec.Width
					}
					fmt.Fprintf(&lines, " %.6f", math.Min(1, math.Max(0, v/float64(size))))
				}
				lines.WriteString("\n")
			}
		}
		stem := strings.TrimSuffix(filepath.Base(rec.File), filepath.Ext(rec.File))
		if err := os.WriteFile(filepath.Join(root, "labels", stem+".txt"), []byte(lines.String()), 0o644); err != nil {
			return err
		}
	}
	var names strings.Builder
	for i, name := range classes {
		fmt.Fprintf(&names, "  %d: %s\n", i, jsonString(name))
	}
	yaml := "# Put or link the images in yolo/images; labels/ holds the matching .txt files.\npath: .\ntrain: images\nval: images\nnames:\n" + names.String()
	return os.WriteFile(filepath.Join(root, "data.yaml"), []byte(yaml), 0o644)
}
```

</details>
<!-- /code -->

The review queue is a CSV that a spreadsheet or an annotation tool can load, with a crop per row, and rows for images where an expected class found nothing:

<!-- code: review-queue -->
<details><summary>Python (python/exports.py)</summary>

```python
def write_review(root, records):
    """`queue.csv` plus one crop per row. Rows: detections between the review and accept thresholds (`low_score`), and
    expected classes with no detection (`empty_but_expected`, the crop is the whole image). Returns the number of rows."""
    (root / "crops").mkdir(parents=True, exist_ok=True)
    rows = []
    for rec in records:
        n = 0
        for det in rec["detections"]:
            if det.route == "review":
                n += 1
                rows.append((rec, f"{rec['id']}-{n:02d}", det.cls, "low_score", round(det.score, 4), det.bbox))
        for cls in rec["missing"]:
            n += 1
            rows.append((rec, f"{rec['id']}-{n:02d}", cls, "empty_but_expected", "", None))
    with (root / "queue.csv").open("w", newline="") as fh:
        out = csv.writer(fh)
        out.writerow(QUEUE_FIELDS)
        for rec, qid, cls, reason, score, box in rows:
            save_crop(rec["path"], box, root / "crops" / f"{qid}.jpg")
            out.writerow([qid, rec["id"], rec["file"], cls, reason, score, *(box or ["", "", "", ""]), f"crops/{qid}.jpg"])
    return len(rows)
```

</details>

<details><summary>Go (go/exports.go)</summary>

```go
// writeReview writes `queue.csv` plus one crop per row. Rows: detections between the review and accept thresholds
// (`low_score`), and expected classes with no detection (`empty_but_expected`, the crop is the whole image). It returns
// the number of rows.
func writeReview(root string, records []record) (int, error) {
	if err := os.MkdirAll(filepath.Join(root, "crops"), 0o755); err != nil {
		return 0, err
	}
	type row struct {
		rec                record
		qid, class, reason string
		score              string
		box                *[4]int // nil: the crop is the whole image
	}
	var rows []row
	for _, rec := range records {
		n := 0
		for _, d := range rec.Detections {
			if d.Route == "review" {
				n++
				rows = append(rows, row{rec, fmt.Sprintf("%s-%02d", rec.ID, n), d.Class, "low_score", num(round(d.Score, 4)), &d.BBox})
			}
		}
		for _, class := range rec.Missing {
			n++
			rows = append(rows, row{rec, fmt.Sprintf("%s-%02d", rec.ID, n), class, "empty_but_expected", "", nil})
		}
	}
	var buf bytes.Buffer
	w := csv.NewWriter(&buf)
	w.Write(queueFields)
	for _, r := range rows {
		crop, box := "crops/"+r.qid+".jpg", []string{"", "", "", ""}
		if err := saveCrop(r.rec.Path, r.box, filepath.Join(root, crop)); err != nil {
			logf("  review %s: no crop (%v)", r.qid, err)
			crop = ""
		}
		if r.box != nil {
			for i, v := range r.box {
				box[i] = strconv.Itoa(v)
			}
		}
		w.Write(append([]string{r.qid, r.rec.ID, r.rec.File, r.class, r.reason, r.score}, append(box, crop)...))
	}
	w.Flush()
	return len(rows), os.WriteFile(filepath.Join(root, "queue.csv"), buf.Bytes(), 0o644)
}
```

</details>
<!-- /code -->

`costs.csv` is built from the ledger rows of each image, so it also shows replays and cache hits:

<!-- code: costs -->
<details><summary>Python (python/exports.py)</summary>

```python
def write_costs(path, items):
    """One row per image: what the call cost at list price and what was charged (0 when replayed), timing and counts."""
    with path.open("w", newline="") as fh:
        out = csv.writer(fh)
        out.writerow(COST_FIELDS)
        for it in items:
            calls = it["calls"]
            out_ = it["output"] or {}
            counts = out_.get("counts", {})
            out.writerow(
                [
                    it["id"],
                    it["status"],
                    round(sum(c["list"] for c in calls if not c["replayed"]), 6),
                    round(sum(c["charged"] for c in calls), 6),
                    any(c["replayed"] for c in calls),
                    ",".join(sorted({c["cache"] for c in calls if c["cache"]})),
                    sum(c["queueMs"] for c in calls),
                    sum(c["processingMs"] for c in calls),
                    len(out_.get("instances", [])),
                    counts.get("accept", 0),
                    counts.get("review", 0),
                ]
            )
```

</details>

<details><summary>Go (go/exports.go)</summary>

```go
// writeCosts writes one row per image: what the call cost at list price and what was charged (0 when replayed),
// timing and counts.
func writeCosts(path string, items []itemResult) error {
	var buf bytes.Buffer
	w := csv.NewWriter(&buf)
	w.Write(costFields)
	for _, it := range items {
		var list, charged float64
		var queue, processing int
		replayed, caches := false, map[string]bool{}
		for _, c := range it.Calls {
			if !c.Replayed {
				list += c.List
			}
			charged += c.Charged
			replayed = replayed || c.Replayed
			if c.Cache != nil && *c.Cache != "" {
				caches[*c.Cache] = true
			}
			queue += c.QueueMs
			processing += c.ProcessingMs
		}
		names := []string{}
		for name := range caches {
			names = append(names, name)
		}
		sort.Strings(names)
		var instances, accepted, review int
		if it.Output != nil {
			instances, accepted, review = len(it.Output.Instances), it.Output.Counts.Accept, it.Output.Counts.Review
		}
		// "True" / "False" and plain decimals, so the file is the same in every language of this use case.
		w.Write([]string{it.ID, it.Status, num(round(list, 6)), num(round(charged, 6)), pyBool(replayed),
			strings.Join(names, ","), strconv.Itoa(queue), strconv.Itoa(processing), strconv.Itoa(instances), strconv.Itoa(accepted), strconv.Itoa(review)})
	}
	w.Flush()
	return os.WriteFile(path, buf.Bytes(), 0o644)
}
```

</details>
<!-- /code -->

### How the exports were checked

`tests/test_prelabelling.py` runs the program against the fake API and checks the files against their formats: COCO ids, categories, boxes inside the image, polygon rings of at least 3 vertices at half-pixel positions inside the box; with `--masks`, RLE segmentations that decode (`clients/python/coco_rle.py`, tested against pycocotools vectors) to the annotation's area and box; YOLO lines with an integer class, an even number of coordinates in 0 to 1 and a `data.yaml` whose names match. The tests also cover resume, expired items, a cut stream and both extra modes. The COCO files were not loaded into pycocotools or an annotation tool in this repository.

## Results <!-- #results -->

The numbers below are measured on the 60-image set with `sam3`; the block stays empty until a maintainer records a run. They vary a little between runs, because a cached image encoding can change a result slightly.

How the numbers are made (`evaluate.py`):

- Precision and recall per class at IoU 0.5, the Open Images way: a class counts only on images where it is verified present or verified absent. On images where the class is not verified, a detection is neither right nor wrong. `precision@0.5` and `recall@0.5` use accepted labels only; `*_with_review` also include the review band.
- `review_share` is the share of the review queue among all detections plus empty-but-expected rows. `review_true_positive_share` says how many review detections turned out to be right, which tells whether reviewing is worth the effort.
- `cost_per_1000_images` is the planning price per 1,000 images: the list price of the calls with the image encoding fee counted on every call. A call that finds the encoding of its image still cached is priced without that fee, and a cache hit is not promised, so plan with this number; `cost_per_1000_images_this_run` is what the recorded run was actually priced at, and `encoding_cache_hit_share` the share of its calls that hit the cache. Throughput is not published: it depends on the network path between the client and the API (`evaluate.py --timing` prints it for a run of your own).

<!-- metrics: expected/full-summary.json -->
| Metric | Value |
|---|---|
| `precision@0.5.Bird` | 0.8846 |
| `recall@0.5.Bird` | 0.7667 |
| `precision_with_review@0.5.Bird` | 0.8621 |
| `recall_with_review@0.5.Bird` | 0.8333 |
| `precision@0.5.Boat` | 0.6579 |
| `recall@0.5.Boat` | 0.8929 |
| `precision_with_review@0.5.Boat` | 0.4464 |
| `recall_with_review@0.5.Boat` | 0.8929 |
| `precision@0.5.Cat` | 0.96 |
| `recall@0.5.Cat` | 1 |
| `precision_with_review@0.5.Cat` | 0.8276 |
| `recall_with_review@0.5.Cat` | 1 |
| `precision@0.5.Dog` | 0.8889 |
| `recall@0.5.Dog` | 0.9231 |
| `precision_with_review@0.5.Dog` | 0.8276 |
| `recall_with_review@0.5.Dog` | 0.9231 |
| `precision@0.5.macro` | 0.8478 |
| `recall@0.5.macro` | 0.8957 |
| `precision_with_review@0.5.macro` | 0.7409 |
| `recall_with_review@0.5.macro` | 0.9123 |
| `review_share` | 0.1973 |
| `review_images_share` | 0.25 |
| `review_true_positive_share` | 0.0741 |
| `review_detections_evaluated` | 27 |
| `images` | 60 |
| `images_ok` | 60 |
| `cost_per_1000_images` | 700 |
| `cost_per_1000_images_this_run` | 225 |
| `encoding_cache_hit_share` | 0.95 |
<!-- /metrics -->

## Cost <!-- #cost -->

An image with 4 text prompts costs one image encoding (0.5) and 4 prompts (4 x 0.05): 0.70 credits at list price, so about 700 credits per 1,000 images. The encoding fee is waived when the same image was encoded shortly before, for example when a run is repeated with other prompts; do not plan on it. In exemplar mode an image costs 0.55 (one prompt, however many boxes). In box mode with `sam-lite` it costs 0.2 plus 0.01 per box. Replayed items are not charged. Uploads are free.

<!-- cost -->
| Run | Calls | List price (credits) | Charged (credits) |
|---|---|---|---|
| smoke | 4 | 2.8 | 2.8 |
| full | 60 | 13.5 | 13.5 |
<!-- /cost -->

## Limits and when not to use this <!-- #limits -->

- Text prompts work for common categories. Rare, fine-grained or specialist classes (medical, industrial defects) have lower recall: measure on a small sample before labelling thousands of images, and consider `--exemplar`.
- A box or mask is a proposal. The review queue exists because scores are not probabilities; it does not replace a final human check of the exported set.
- Very small objects (below about 1/50 of the short image side) are often missed, and crowds that overlap heavily can be merged into one instance.
- COCO polygons have no holes, so objects with holes are filled in `coco.json` and in YOLO files. Use `--masks` for RLE segmentations that keep them. The YOLO export always uses polygons.
- In box mode the score is the model's predicted mask IoU, not a detector confidence, and no box is dropped: anything below `--accept` goes to review.
- An exemplar box applies to the image it is drawn on. It finds more instances in that image; it does not teach the model a class for the rest of the dataset.
- Images in a batch run one after another: a batch of 100 images is minutes, not seconds, and at most 600 s fit in one event stream. Items that do not fit expire and are sent again in the next round.
- Idempotency keys are kept for 24 hours. `--resume` uses the local `raw/` copy, so it works later, but an item that finished on the server and was never received before a crash can be charged again.
- Review rows and crops are for people. Cropping uses Pillow, the only dependency.
- See [Segmentation with text prompts](https://stack.foldbun.com/docs/use-cases/concept-segmentation/) and [Post-processing](https://stack.foldbun.com/docs/post-processing/) in the documentation.

## Data <!-- #data -->

The 60 images are a sample of the Open Images Dataset V7 validation set. Annotations are CC BY 4.0 (Google LLC) and the images are CC BY 2.0 by their authors; the committed smoke images and their attribution are in `datasets/smoke/01-dataset-prelabelling/`, the rest in `datasets/attribution/ATTRIBUTION-01-dataset-prelabelling.csv`. The offline fixtures in `fixtures/` are synthetic: they were drawn by `fixtures/synthesize.py` from the ground-truth boxes and are not model output.
