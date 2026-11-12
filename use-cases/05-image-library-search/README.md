# Search an image library by image, by text, and for near-duplicates

English | [简体中文](README.zh-CN.md)

Embed a library of images with `dinov3` and `siglip2`, keep the vectors in a small local index that is updated incrementally, and answer three questions: which images look like this one, which images match this sentence, and which images are near-duplicates of each other. Both models are in the public pool, so a new account can run the smoke set.

**Built with DINOv3.** `dinov3` output is covered by the DINOv3 License; products built on it should show the tag "Built with DINOv3".

## Problem <!-- #problem -->

A photo library has a few hundred to a few tens of thousands of images, grows by a few images a day, and is searched by people who remember what a photo looked like or what was in it, not what it is called. Three things are wanted:

- find the images that look like a given image (retrieval by example);
- find the images that show what a sentence describes (text search);
- find copies of the same picture that were resized, cropped, re-compressed, flipped or watermarked (near-duplicates).

The constraints that shape the code are the ones around the call, not the model:

- **Cost.** An image costs 0.05 credits per model, so a library costs 0.10 credits per image once. Re-embedding the library on every change would multiply that, so the index must be rebuilt incrementally: only new or changed files are embedded, found by the sha256 of their content.
- **Allowance.** The public pool includes 100 calls per UTC day, and every image of a batch is a call. A library of a few hundred images does not fit in one day, so the run has to stop cleanly when the allowance is used up and continue the next day, not fail half-way.
- **Failure.** Batches can lose an item, expire, or lose the connection. A rerun must not embed or pay twice.
- **Scale.** At this size the vectors fit in memory and exact search is one matrix product. No vector database is needed, and none is added.

## Pipeline <!-- #pipeline -->

```
 folder of images
      |  sha256 of every file; compare with the catalogue in the index
      v
 new or changed images only
      |  POST /v1/images (free), then POST /v1/batch/embeddings
      |  chunks of 50, one model at a time, dinov3 then siglip2
      v
 index directory: index.json + vectors-dinov3-N.f32 + vectors-siglip2-N.f32
      |  committed after every chunk (atomic), stops cleanly at the daily allowance
      v
 queries, all local except the query text or the query image that is not in the library
   image  -> dinov3 vector  -> exact cosine top-k
   text   -> POST /v1/embeddings (siglip2 texts) -> exact cosine top-k over the siglip2 image vectors
   duplicates -> dinov3 vectors, pairs above a threshold, connected components (union-find)
```

1. `POST /v1/images`: upload each new file once and get a handle. Free.
2. `POST /v1/batch/embeddings` with `dinov3` and with `siglip2`: up to 1,000 items per request, 50 here. A batch takes one token of the per-minute limit, but its items run one after another on the server and each one counts as a call of the daily allowance.
3. `index.json` and the vector files are written after every chunk. Vectors are L2-normalised, so the dot product is the cosine similarity.
4. A text query is one `POST /v1/embeddings` call with `siglip2` (0.01 credits per text, up to 64 texts per call); it is ranked against the `siglip2` vectors of the images, because image and text share one space. An image query uses the stored `dinov3` vector when the image is in the library, and one call otherwise.
5. Near-duplicates need no call: the pairs above a threshold are read off the stored `dinov3` vectors.

## Run it <!-- #run -->

```bash
export FOLDBUN_API_KEY="fbs_..."
cd use-cases/05-image-library-search/python

uv run imgsearch.py index ~/photos                          # embed new and changed files; run it again to resume
uv run imgsearch.py image ~/photos/2026/beach.jpg           # images that look like this one
uv run imgsearch.py text "a photo of a dog on a beach"      # images that match a sentence
uv run imgsearch.py duplicates --threshold 0.90             # groups of near-duplicates
uv run imgsearch.py status                                  # what the index holds

uv run main.py                                              # the smoke set; writes out/result.json (what the harness runs)
```

`index` accepts `--batch-size`, `--free-only` (stop at the included daily calls instead of paying for more) and `--keep-free N` (leave N included calls for queries). When the daily allowance ends, it prints how many vectors are missing and when the calls come back (00:00 UTC), and exits with code 5; run the same command then. The exit codes are listed in the header of `imgsearch.py`.

The first query after a fresh `index` shows its cost on the last line, for example `cost: 0.01 credits at list price, 0 charged, in 1 calls`.

The same code, driven by a manifest, is what the harness runs (`main.py --manifest M --out DIR`). It also writes `DIR/index/` (the index), `DIR/stats.json` (timing, the allowance seen, the result of a second indexing pass that must make no call) and `DIR/result.json` in the format of `contract/result.schema.json`: one item per library image and one per text query, each with the ledger rows of its own calls.

The full set (400 images, 100 near-duplicate variants, 40 text queries, 73 MB) is a release asset of the repository (see `datasets/README.md`). Its manifest is committed as `assets/full.json`; the images and the ground truth are in the archive. Extract it into `assets/` and run the reference implementation on it from the repository root. The run is at most 51 credits at list price (500 images x 0.10 plus 40 texts x 0.01 is 50.4) in about 1,040 calls, which fits in one UTC day only on a funded account; on a public-pool account the daily allowance ends the run early and it must be repeated on the following days.

```bash
tar xzf 05-image-library-search-full.tar.gz -C use-cases/05-image-library-search/assets
export FOLDBUN_API_KEY="fbs_..."
uv run python -m harness live --size full --uc 05 --max-credits 51 --dry-run   # the plan, nothing is sent
uv run python -m harness refresh --size full --uc 05 --max-credits 51          # runs, scores, writes expected/full.json and expected/full-summary.json, then `uv run python -m harness snippets`
```

`harness live` runs the same and only checks the result against the floors in `usecase.toml` `[quality]`; the metrics are those of `evaluate.py --result out/result.json --truth assets/truth.json`.

The Go implementation is the same pipeline as one command: `cd use-cases/05-image-library-search/go && go run . --manifest ../assets/smoke/manifest.json --out out`. It uses only the standard library and reads and writes the same index format, so an index built by one language can be continued by the other with `--resume`. It has no counterpart of `imgsearch.py` (the folder commands are in Python only), and in Go `--limit 0` means all items.

## Code <!-- #code -->

The client file (`stack_client.py`, `stackclient/client.go`) is a copy of the one in `clients/` (retries, idempotency keys, the per-minute token bucket, the credit cap and the ledger). The code below is what this use case adds.

**The loop over the library.** The images that lack a vector are cut into chunks and embedded with each model in turn. The models alternate chunk by chunk, so that when the allowance ends the first part of the library has both vectors.

<!-- code: index-library -->
<details><summary>Python (python/embedder.py)</summary>

```python
def index_library(
    client, index: LibraryIndex, root: Path, models=("dinov3", "siglip2"), *, batch_size=50, free_only=False, keep_free=0, workers=4, log=print
) -> Report:
    """Embed every library image that is missing from the index, with every model, a chunk at a time.

    Chunks alternate between the models, so that when the daily allowance ends the first part of the library has
    both vectors instead of one model having used all of it.
    """
    report = Report()
    paths = {it["sha256"]: root / it["path"] for it in index.items}
    # sha256 of every image that lacks a vector of some model, in catalogue order
    pending = list(dict.fromkeys(sha for model in models for sha in index.missing(model)))
    log(f"{len(index.items)} images in the library, {len(pending)} to embed")
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        for model in models:
            need = {sha: paths[sha] for sha in chunk if not index.has(model, sha)}
            if need and not report.stopped:
                embed_chunk(client, index, model, need, report, free_only=free_only, keep_free=keep_free, workers=workers)
        log(f"  {min(start + batch_size, len(pending))}/{len(pending)}")
        if report.stopped:
            break
    return report
```

</details>

<details><summary>Go (go/embedder.go)</summary>

```go
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
```

</details>
<!-- /code -->

**One batch.** Each item of the stream is handled as it arrives. The ledger row of an item is the last row of the batch's tag. An item that failed for a reason that may pass (`image_not_found`, `model_busy`, an expired item) goes into the next attempt; a stream that ends without a summary sends the unfinished items again; the allowance ending, or the credit cap, stops the run. The idempotency key of a batch is derived from its content, so rerunning an interrupted chunk replays the items that finished without charging them again.

<!-- code: embed-chunk -->
<details><summary>Python (python/embedder.py)</summary>

```python
def run_batch(client, model, shas, handles, attempt, report, *, free_only, keep_free):
    """One batch request. Returns ([(sha256, vector)] that succeeded, [sha256] to send again)."""
    tag = f"{model}:{shas[0][:12]}:{attempt}"
    body = {"model": model, "items": [{"image": sc.image_handle(h)} for h in handles]}
    done, retry = [], []
    try:
        with closing(client.batch("embeddings", body, _key(model, handles, attempt), tag=tag)) as events:
            for event, data in events:
                if event != "item":
                    continue  # progress heartbeats; the summary ends the stream
                sha = shas[data["index"]]
                if data["status"] == "ok":
                    done.append((sha, data["result"]["embeddings"][0]["vector"]))
                    report.rows.setdefault(sha, []).append(client.calls(tag)[-1])  # the ledger row of the item that just arrived
                    billing = data["result"].get("billing") or {}
                    if billing.get("allowance"):
                        report.allowance[model] = {"pool": billing["pool"], **billing["allowance"]}
                        used, limit = billing["allowance"]["used"], billing["allowance"]["limit"]
                        # On the public pool the next call would be refused. With `free_only` a funded account stops
                        # here too, instead of paying for the calls that come after the included ones.
                        if (billing["pool"] == "public" or free_only) and limit - used <= keep_free:
                            report.stopped = {"code": "daily_limit_reached", "message": f"{used} of {limit} included calls used today", "model": model}
                            break  # closing the stream stops the batch; the items that already finished are kept
                elif data["status"] == "expired" or data["error"]["code"] in RETRY_CODES:
                    retry.append(sha)  # not billed; goes into the next attempt (an evicted handle is uploaded again)
                elif data["error"]["code"] in STOP_CODES:
                    report.stopped = {**data["error"], "model": model}
                    break
                else:
                    report.failed[sha] = data["error"]  # for example an image the API cannot read: no retry helps
    except sc.StackError as err:
        if err.code in STOP_CODES:  # refused before the stream started, or by the credit cap
            report.stopped = {"code": err.code, "message": err.message, "model": model}
        elif not err.retryable:
            raise
    if not report.stopped:
        # A stream can end early (a dropped connection, a timeout) without a summary event: whatever never got an
        # outcome is sent again. The finished items are kept, so nothing is embedded or paid twice.
        seen = {s for s, _ in done} | set(retry) | set(report.failed)
        retry += [s for s in shas if s not in seen]
    return done, retry


def embed_chunk(client, index: LibraryIndex, model: str, paths: dict[str, Path], report: Report, *, free_only=False, keep_free=0, workers=4) -> None:
    """Embed the images `paths` (sha256 -> file) with `model`, commit them, and record the outcome in `report`."""
    todo = list(paths)
    for attempt in range(ATTEMPTS):
        if not todo or report.stopped:
            break
        with ThreadPoolExecutor(workers) as pool:
            handles = list(pool.map(lambda sha: _upload(client, paths[sha]), todo))  # free; also re-uploads evicted images
        done, retry = run_batch(client, model, todo, handles, attempt, report, free_only=free_only, keep_free=keep_free)
        if done:
            index.add(model, [s for s, _ in done], [v for _, v in done])
            index.commit()
            report.embedded += len(done)
        todo = retry
    for sha in [] if report.stopped else todo:
        report.failed.setdefault(sha, {"code": "gave_up", "message": f"not embedded after {ATTEMPTS} attempts"})
```

</details>

<details><summary>Go (go/embedder.go)</summary>

```go
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
```

</details>
<!-- /code -->

**The index on disk.** Vector files are never modified in place. A commit writes new files, then replaces `index.json` atomically, then deletes the files nothing refers to. A crash leaves the old index or the new one.

<!-- code: commit -->
<details><summary>Python (python/index.py)</summary>

```python
def commit(self) -> None:
    """Write what changed. Call it after every batch, so that a stop or a crash loses at most one batch."""
    self.dir.mkdir(parents=True, exist_ok=True)
    keep = {it["sha256"] for it in self.items}
    self.generation += 1
    for model in sorted(self._dirty):
        rows = self._rows[model]
        order = [sha for sha, _ in sorted(rows.items(), key=lambda kv: kv[1]) if sha in keep]
        matrix = self._vectors[model][[rows[sha] for sha in order]] if order else self._vectors[model][:0]
        self._vectors[model], self._rows[model] = matrix, {sha: i for i, sha in enumerate(order)}
        name = f"vectors-{model}-{self.generation}.f32"
        atomic_write(self.dir / name, np.ascontiguousarray(matrix, dtype="<f4").tobytes())
        self.models[model].update(file=name, sha256s=order)
    self._dirty.clear()
    meta = {"format": FORMAT, "generation": self.generation, "items": self.items, "models": self.models}
    atomic_write(self.dir / "index.json", json.dumps(meta, indent=1).encode())
    live = {info["file"] for info in self.models.values()}
    for stale in self.dir.glob("vectors-*.f32"):
        if stale.name not in live:
            stale.unlink()
```

</details>

<details><summary>Go (go/index.go)</summary>

```go
// Commit writes what changed. Call it after every batch, so that a stop or a crash loses at most one batch.
func (x *LibraryIndex) Commit() error {
	if err := os.MkdirAll(x.Dir, 0o755); err != nil {
		return err
	}
	keep := map[string]bool{}
	for _, it := range x.Items {
		keep[it.Sha256] = true
	}
	x.Generation++
	dirty := make([]string, 0, len(x.dirty))
	for model := range x.dirty {
		dirty = append(dirty, model)
	}
	sort.Strings(dirty)
	for _, model := range dirty {
		rows, dims := x.rows[model], x.Models[model].Dimensions
		order := make([]string, 0, len(rows))
		for sha := range rows {
			if keep[sha] {
				order = append(order, sha)
			}
		}
		sort.Slice(order, func(i, j int) bool { return rows[order[i]] < rows[order[j]] })
		old, fresh := x.vectors[model], make(map[string]int, len(order))
		kept, file := make([]float32, 0, len(order)*dims), make([]byte, 0, 4*len(order)*dims)
		for i, sha := range order {
			row := old[rows[sha]*dims : (rows[sha]+1)*dims]
			kept = append(kept, row...)
			for _, v := range row {
				file = binary.LittleEndian.AppendUint32(file, math.Float32bits(v))
			}
			fresh[sha] = i
		}
		x.vectors[model], x.rows[model] = kept, fresh
		name := fmt.Sprintf("vectors-%s-%d.f32", model, x.Generation)
		if err := atomicWrite(filepath.Join(x.Dir, name), file); err != nil {
			return err
		}
		x.Models[model].File, x.Models[model].Sha256s = name, order
	}
	x.dirty = map[string]bool{}
	items := x.Items
	if items == nil {
		items = []Item{}
	}
	meta, err := json.MarshalIndent(indexMeta{Format: indexFormat, Generation: x.Generation, Items: items, Models: x.Models}, "", " ")
	if err != nil {
		return err
	}
	if err := atomicWrite(filepath.Join(x.Dir, "index.json"), meta); err != nil {
		return err
	}
	live := map[string]bool{}
	for _, info := range x.Models {
		live[info.File] = true
	}
	stale, _ := filepath.Glob(filepath.Join(x.Dir, "vectors-*.f32"))
	for _, path := range stale {
		if !live[filepath.Base(path)] {
			os.Remove(path)
		}
	}
	return nil
}
```

</details>
<!-- /code -->

**Exact search.** The vectors are L2-normalised, so one matrix-vector product gives every cosine similarity.

<!-- code: top-k -->
<details><summary>Python (python/search.py)</summary>

```python
def top_k(matrix: np.ndarray, query, k: int, exclude: int | None = None) -> list[tuple[int, float]]:
    """The `k` rows of `matrix` most similar to `query`, as (row, score), best first. `exclude` skips one row (the query itself)."""
    if len(matrix) == 0:
        return []
    scores = matrix @ np.asarray(query, dtype=np.float32)
    if exclude is not None:
        scores[exclude] = -np.inf
    k = min(k, len(scores) - (exclude is not None))
    if k <= 0:
        return []
    best = np.argpartition(-scores, k - 1)[:k] if k < len(scores) else np.arange(len(scores))
    best = best[np.lexsort((best, -scores[best]))]  # by score, ties by row, so the order is stable
    return [(int(i), float(scores[i])) for i in best]
```

</details>

<details><summary>Go (go/search.go)</summary>

```go
// TopK returns the k rows of m most similar to query (which has m.Dim values), as (row, score), best first. exclude
// skips one row (the query itself); pass -1 to skip none. Equal scores are ordered by row, so the order is stable.
func TopK(m Matrix, query []float32, k int, exclude int) []Hit {
	if m.N == 0 {
		return []Hit{}
	}
	hits := make([]Hit, 0, m.N)
	for i := 0; i < m.N; i++ {
		if i != exclude {
			hits = append(hits, Hit{i, dot(m.Row(i), query)})
		}
	}
	sort.Slice(hits, func(a, b int) bool {
		if hits[a].Score != hits[b].Score {
			return hits[a].Score > hits[b].Score
		}
		return hits[a].Row < hits[b].Row
	})
	if k < 0 {
		k = 0
	}
	return hits[:min(k, len(hits))]
}
```

</details>
<!-- /code -->

**Near-duplicates.** Pairs above the threshold are found block by block (memory stays bounded) and merged with union-find.

<!-- code: duplicates -->
<details><summary>Python (python/search.py)</summary>

```python
class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]  # path halving
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        self.parent[self.find(j)] = self.find(i)


def similar_pairs(matrix: np.ndarray, threshold: float, block: int = 1024):
    """Yield (i, j, score) for every pair i < j with similarity >= threshold, one block of rows at a time (memory stays O(block x n))."""
    n = len(matrix)
    for start in range(0, n, block):
        scores = matrix[start : start + block] @ matrix.T
        rows, cols = np.nonzero(scores >= threshold)
        for r, c in zip(rows, cols):
            if start + r < c:
                yield int(start + r), int(c), float(scores[r, c])


def duplicate_groups(matrix: np.ndarray, threshold: float) -> list[list[int]]:
    """Groups of rows that are near-duplicates of each other, found as connected components of the 'similar enough' graph.

    Connected components are transitive: if A resembles B and B resembles C, all three end up in one group even when A and
    C are below the threshold. A high threshold keeps chains short; the groups are what a person reviews, not a verdict.
    """
    uf = UnionFind(len(matrix))
    for i, j, _ in similar_pairs(matrix, threshold):
        uf.union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(len(matrix)):
        groups.setdefault(uf.find(i), []).append(i)
    return sorted((g for g in groups.values() if len(g) > 1), key=lambda g: g[0])
```

</details>

<details><summary>Go (go/search.go)</summary>

```go
// UnionFind groups elements into connected components.
type UnionFind struct{ parent []int }

func NewUnionFind(n int) *UnionFind {
	u := &UnionFind{parent: make([]int, n)}
	for i := range u.parent {
		u.parent[i] = i
	}
	return u
}

func (u *UnionFind) Find(i int) int {
	for u.parent[i] != i {
		u.parent[i] = u.parent[u.parent[i]] // path halving
		i = u.parent[i]
	}
	return i
}

func (u *UnionFind) Union(i, j int) { u.parent[u.Find(j)] = u.Find(i) }

// SimilarPairs returns every pair (i, j) with i < j whose similarity is at least threshold, ordered by i then j. Rows are
// compared in parallel, in blocks.
func SimilarPairs(m Matrix, threshold float64) [][2]int {
	const block = 64
	limit := float32(threshold) // the scores are float32: compare in float32
	blocks := (m.N + block - 1) / block
	found := make([][][2]int, blocks)
	var wg sync.WaitGroup
	next := make(chan int)
	for w := 0; w < runtime.NumCPU(); w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for b := range next {
				for i := b * block; i < min((b+1)*block, m.N); i++ {
					for j := i + 1; j < m.N; j++ {
						if dot(m.Row(i), m.Row(j)) >= limit {
							found[b] = append(found[b], [2]int{i, j})
						}
					}
				}
			}
		}()
	}
	for b := 0; b < blocks; b++ {
		next <- b
	}
	close(next)
	wg.Wait()
	var pairs [][2]int
	for _, f := range found {
		pairs = append(pairs, f...)
	}
	return pairs
}

// DuplicateGroups returns the groups of rows that are near-duplicates of each other, found as connected components of
// the "similar enough" graph. Rows inside a group, and the groups themselves, are in row order.
//
// Connected components are transitive: if A resembles B and B resembles C, all three end up in one group even when A and
// C are below the threshold. A high threshold keeps chains short; the groups are what a person reviews, not a verdict.
func DuplicateGroups(m Matrix, threshold float64) [][]int {
	uf := NewUnionFind(m.N)
	for _, p := range SimilarPairs(m, threshold) {
		uf.Union(p[0], p[1])
	}
	byRoot, groups := map[int][]int{}, [][]int{}
	for i := 0; i < m.N; i++ {
		root := uf.Find(i)
		byRoot[root] = append(byRoot[root], i)
	}
	for _, g := range byRoot {
		if len(g) > 1 {
			groups = append(groups, g)
		}
	}
	sort.Slice(groups, func(a, b int) bool { return groups[a][0] < groups[b][0] })
	return groups
}
```

</details>
<!-- /code -->

**Incremental re-indexing is checked on every run.** After indexing, `main.py` indexes again. Nothing is missing, so the second pass must make no API call; the number is recorded in `stats.json`, and a call makes the run fail.

<!-- code: reindex-check -->
<details><summary>Python (python/main.py)</summary>

```python
calls_before = len(client.ledger)
again = embedder.index_library(client, index, base, MODELS, batch_size=args.batch_size, workers=args.concurrency, log=lambda *_: None)
stats["reindex"] = {"apiCalls": len(client.ledger) - calls_before, "embedded": again.embedded}  # nothing is missing, so nothing is sent
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
callsBefore := len(client.Calls(""))
var again *Report
again, err = indexLibrary(client, index, base, models, *batchSize, *freeOnly, 0, *concurrency, func(string) {})
if err == nil {
	st.Reindex.APICalls, st.Reindex.Embedded = len(client.Calls(""))-callsBefore, again.Embedded // nothing is missing, so nothing is sent
}
```

</details>
<!-- /code -->

**Text queries.** One call per query keeps the cost and the latency of each query in its own item. Texts can be sent together (up to 64 per call) to spend fewer calls of the daily allowance; the price is per text either way.

<!-- code: text-queries -->
<details><summary>Python (python/main.py)</summary>

```python
text_items, text_ms = [], []
siglip_items, siglip_matrix = index.matrix("siglip2")
for q in manifest.get("queries", []):
    entry = {"id": q["id"], "calls": [], "output": None}
    try:
        vector, row = embedder.embed_text(client, q["text"], tag=q["id"])
        t = time.monotonic()
        ranked = search.top_k(siglip_matrix, vector, TEXT_RESULTS)
        text_ms.append((time.monotonic() - t) * 1000)
        entry["status"], entry["calls"] = "ok", [row]
        entry["output"] = {"type": "query", "text": q["text"], "ranking": [siglip_items[i]["id"] for i, _ in ranked], "scores": [r4(s) for _, s in ranked]}
    except sc.StackError as err:
        print(f"  {q['id']}: {err.status} {err.code}: {err.message}", file=sys.stderr)
        if err.code == "model_locked":
            return EXIT_TIER
        entry["status"] = "expired" if err.code in embedder.STOP_CODES else "error"
        entry["error"] = {"status": err.status, "code": err.code, "message": err.message}
        if err.code == "budget_exceeded":
            code = EXIT_BUDGET
    text_items.append(entry)
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
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
```

</details>
<!-- /code -->

## Search service <!-- #service -->

The Go program has a second mode that serves searches over an index that an earlier run wrote, in Python or in Go (the format is shared): `go run . serve --index out/index`. It listens on `127.0.0.1:8080` by default (`--addr` changes it), is read-only (nothing is indexed over HTTP), answers JSON and reports every error as `{"error": {"code": "...", "message": "..."}}`.

```bash
curl "127.0.0.1:8080/search?q=a+photo+of+a+dog&k=10"   # images that match a text (siglip2)
curl "127.0.0.1:8080/similar?id=5177b735e298df6c&k=10"  # images that look like a library image (dinov3)
curl "127.0.0.1:8080/duplicates?threshold=0.90"         # groups of near-duplicates (dinov3)
curl "127.0.0.1:8080/healthz"
```

`/search` embeds the text with `siglip2` through the client, so every query is one billed call (0.01 credits at list price; it counts as one of the included daily calls while those last); it needs `FOLDBUN_API_KEY` when the service starts, and without it only `/search` is off. `/similar` and `/duplicates` read the stored vectors and make no API call. `k` defaults to 10 and is lowered to 100 when larger; a bad `k`, an empty or over-long `q` and a threshold outside (0, 1] answer 400, an unknown image id 404, any method but GET 405, and an API failure 502 (429 when the daily allowance, the balance or the credit cap is used up). SIGINT and SIGTERM stop the service after the running requests finish.

The handler table and the server settings (timeouts, header limit, graceful shutdown) are all of it:

<!-- code: serve-setup@go -->
<details><summary>Go (go/serve.go)</summary>

```go
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
```

</details>
<!-- /code -->

The search is a brute-force pass over the vectors, which is fine to a few tens of thousands of images; see the limits below for what to use beyond that.

## Results <!-- #results -->

What is measured, with `evaluate.py` (standard library only; it reads `result.json`, `stats.json` and the ground truth):

- **Image to image.** Each near-duplicate variant is a query; `i2i_recall_at_k` is the share of variants whose original is among the k nearest images.
- **Text to image.** 40 queries of the form "a photo of a <class>", scored against the human-verified Open Images image-level labels of the originals: `text_precision_at_5`, `text_precision_at_10`, `text_recall_at_10`, `text_map`. Variants are not relevant by definition and are removed from the rankings before scoring.
- **Near-duplicates.** Precision, recall and F1 of "similarity at or above the threshold" on the candidate pairs. The threshold is chosen on split a of the data (the lowest threshold that keeps precision at 0.99 or better) and applied unchanged to split b, where the numbers are reported.
- **Speed and cost.** The local search time (no network involved), the number of calls a second indexing pass makes (it must be none), and the list price and the charge per 1,000 images. Indexing throughput depends on the reader's network path to the API, so it is not published.

The numbers below are generated from `expected/full-summary.json`, which a maintainer records from a live run on the full set (about 500 images). Until then the block says so. The offline runs of `harness check` use the fake API, whose vectors are deterministic but carry no meaning: they verify the plumbing (the index is built, a second pass sends nothing, queries return ranked results of the right shape) and not the quality, so no quality bound applies to them.

<!-- metrics: expected/full-summary.json -->
| Metric | Value |
|---|---|
| `i2i_recall_at_1` | 1 |
| `i2i_recall_at_5` | 1 |
| `i2i_recall_at_10` | 1 |
| `text_precision_at_5` | 0.905 |
| `text_precision_at_10` | 0.8075 |
| `text_recall_at_10` | 0.7963 |
| `text_map` | 0.8438 |
| `text_queries` | 40 |
| `dup_threshold` | 0.9352 |
| `dup_precision` | 1 |
| `dup_recall` | 0.9149 |
| `dup_f1` | 0.9556 |
| `dup_pairs_a` | 53 |
| `dup_pairs_b` | 47 |
| `images` | 500 |
| `images_embedded` | 500 |
| `cost_per_1000_images_list` | 100 |
| `cost_per_1000_images_charged` | 90 |
| `cost_per_text_query_list` | 0.01 |
| `search_ms_text` | 0.389 |
| `search_ms_image` | 0.003 |
| `second_pass_api_calls` | 0 |
<!-- /metrics -->

## Cost <!-- #cost -->

An image costs 0.05 credits with `dinov3` and 0.05 with `siglip2`; a text costs 0.01 with `siglip2`. The smoke set (25 images, 5 queries) is 25 x 0.10 + 5 x 0.01 = 2.55 credits at list price, in 55 calls: 25 for `dinov3`, 25 for `siglip2` and 5 text queries. That is inside the 100 included calls of one UTC day, so a new account is charged nothing. The full set (about 500 images, 40 queries) is 50.4 credits at list price, in about 1,040 calls: a funded account's free pool covers the first 1,000 calls per UTC day of each pool model; a public-pool account would need about 11 days of 100 calls, and the indexing resumes after each stop.

Each run records its own list price and charge:

<!-- cost -->
| Run | Calls | List price (credits) | Charged (credits) |
|---|---|---|---|
| smoke | 55 | 2.55 | 0 |
| full | 1040 | 45.4 | 45.4 |
<!-- /cost -->

Per 1,000 images the library costs 100 credits at list price, once; after that only new and changed images are charged. Queries cost 0.01 credits for a text and nothing for an image that is already in the library.

## Limits and when not to use this <!-- #limits -->

- **Exact search is for this size.** One query is one pass over all vectors: about 3 MB per 1,000 images (768 values of 4 bytes), a few milliseconds for 10,000 images. Past roughly 50,000 to 100,000 images, or when queries must answer in a few milliseconds under load, move to an approximate index (FAISS, hnswlib, or pgvector in a database). Keep the interface: `top_k(matrix, query, k)` becomes a call to the index, the vector files stay the source of truth, and nothing else in the pipeline changes. All-pairs duplicate search grows with the square of the library; at that size use the index's range search or group by a coarse hash first.
- **Duplicates are a threshold on a similarity.** The default (0.90) is a starting point. Choose it on a labelled sample of the library, as `evaluate.py` does, and expect crops and heavy re-compression to score lower than resizes. Connected components chain: A resembles B and B resembles C puts all three in one group.
- **Image-level meaning only.** Both models describe a whole image; there is no object location. `siglip2` takes a 224 px input, so small objects are lost. Text queries work best in English, and the scores are small numbers: only the ranking carries meaning, so no fixed cut-off is used.
- **The model is part of the index.** Vectors of different models are not comparable, and a new version of a model would make old vectors inconsistent with new ones. The index stores the vector length of each model and refuses a different one; rebuild it when a model changes.
- **Daily allowance.** The public pool includes 100 calls per UTC day. A shared pool may use one allowance for both models; the code reacts to what each response reports instead of assuming. Items of a batch run one after another, so a batch of N takes about N times the processing time of one image; keep chunks well inside the 600 s limit of a batch.
- **Uploads are kept by the client.** The client file keeps the bytes of uploaded images in memory to upload again when a handle is evicted. For a very large library, index it in several runs.
- Reference: [Embeddings and search](https://stack.foldbun.com/docs/use-cases/embeddings/), [DINOv3](https://stack.foldbun.com/docs/models/dinov3/), [SigLIP 2](https://stack.foldbun.com/docs/models/siglip2/) and [Rate limits and pools](https://stack.foldbun.com/docs/rate-limits/) in the documentation. `dinov3` output is covered by the DINOv3 License; products built on it should show the tag "Built with DINOv3".

## Data <!-- #data -->

The smoke set is 20 original photos, 5 near-duplicate variants and 5 text queries from `datasets/smoke/05-image-library-search/` (`assets/smoke/` links to it). The photos come from the Open Images V7 validation split (images CC BY 2.0 by their authors, labels CC BY 4.0); the variants are derived from them with recorded transforms. Authors and licences are in `assets/smoke/ATTRIBUTION.csv`. The full set (400 images, 100 variants, 40 queries) is a release asset described in `datasets/README.md`.
