<!-- en-sha: 67114e70a5e3 -->
# 按图、按文字搜索图库，并找出近重复图片

[English](README.md) | 简体中文

用 `dinov3` 和 `siglip2` 为图库生成向量，存进可增量更新的本地小索引，回答三个问题：哪些图片与这张图相似，哪些图片符合这句描述，哪些图片互为近重复。两个模型都在公共池内，新账户即可运行 smoke 数据集。

**Built with DINOv3.** `dinov3` 的输出受 DINOv3 License 约束；基于它构建的产品应显示标签 "Built with DINOv3"。

## 问题 <!-- #problem -->

一个照片库有几百到几万张图片，每天增加少量新图，使用者记得的是照片的样子或内容，而不是文件名。需要三种能力：

- 找出与给定图片相似的图片（以图搜图）；
- 找出与一句描述相符的图片（文字搜图）；
- 找出同一张图的副本：缩放、裁剪、重新压缩、翻转或加水印后的版本（近重复）。

决定代码形状的是调用周围的约束，而不是模型本身：

- **成本。** 每个模型每张图 0.05 credits，建库一次每张图 0.10 credits。每次变动都重新嵌入整个图库会成倍放大这笔费用，因此索引必须增量重建：只嵌入新增或改动的文件，按文件内容的 sha256 判断。
- **额度。** 公共池每个 UTC 日包含 100 次调用，批处理中的每张图算一次调用。几百张图的图库一天内放不下，所以额度用完时必须干净地停下，次日接着做，而不是做到一半失败。
- **失败。** 批处理可能丢失某一项、过期或断开连接。重跑不得重复嵌入，也不得重复计费。
- **规模。** 这个量级下向量放得进内存，精确检索只是一次矩阵乘法。不需要向量数据库，也没有引入。

## 流程 <!-- #pipeline -->

```
 图片文件夹
      |  计算每个文件的 sha256，与索引中的目录比对
      v
 仅新增或改动的图片
      |  POST /v1/images（免费），再 POST /v1/batch/embeddings
      |  每块 50 张，一次一个模型，先 dinov3 后 siglip2
      v
 索引目录：index.json + vectors-dinov3-N.f32 + vectors-siglip2-N.f32
      |  每块之后提交（原子写入），到每日额度用完时干净地停止
      v
 查询：除查询文字或不在图库中的查询图片外，全部在本地完成
   图片   -> dinov3 向量  -> 精确余弦 top-k
   文字   -> POST /v1/embeddings（siglip2 文本）-> 对 siglip2 图片向量做精确余弦 top-k
   近重复 -> dinov3 向量，取超过阈值的图片对，连通分量（并查集）
```

1. `POST /v1/images`：每个新文件上传一次，得到句柄。免费。
2. 分别用 `dinov3` 和 `siglip2` 调用 `POST /v1/batch/embeddings`：每个请求最多 1,000 项，此处用 50。一个批处理占用一个每分钟限速令牌，但服务端逐项依次执行，每一项都计入每日额度的一次调用。
3. 每一块之后写入 `index.json` 和向量文件。向量已做 L2 归一化，点积就是余弦相似度。
4. 文字查询是一次 `siglip2` 的 `POST /v1/embeddings` 调用（每条文本 0.01 credits，每次最多 64 条），与图片的 `siglip2` 向量比较，因为图片与文字共用一个空间。图片查询在图片已在图库中时使用已存的 `dinov3` 向量，否则调用一次。
5. 近重复不需要调用：直接从已存的 `dinov3` 向量中取出超过阈值的图片对。

## 运行 <!-- #run -->

```bash
export FOLDBUN_API_KEY="fbs_..."
cd use-cases/05-image-library-search/python

uv run imgsearch.py index ~/photos                          # 嵌入新增和改动的文件；再运行一次即可续做
uv run imgsearch.py image ~/photos/2026/beach.jpg           # 与这张图相似的图片
uv run imgsearch.py text "a photo of a dog on a beach"      # 符合一句描述的图片
uv run imgsearch.py duplicates --threshold 0.90             # 近重复分组
uv run imgsearch.py status                                  # 索引中有什么

uv run main.py                                              # smoke 数据集，写出 out/result.json（harness 运行的就是它）
```

`index` 支持 `--batch-size`、`--free-only`（到达包含的每日调用数时停止，不再付费）和 `--keep-free N`（为查询保留 N 次包含的调用）。每日额度用完时，它会打印还缺多少向量、调用数何时恢复（00:00 UTC），并以退出码 5 结束；到时重新运行同一条命令即可。退出码列在 `imgsearch.py` 的文件头。

新建索引后的第一次查询会在最后一行显示费用，例如 `cost: 0.01 credits at list price, 0 charged, in 1 calls`。

同一套代码由清单驱动时就是 harness 运行的程序（`main.py --manifest M --out DIR`）。它还会写出 `DIR/index/`（索引）、`DIR/stats.json`（耗时、观察到的额度、第二次建库的结果，该次必须不发出任何调用）以及符合 `contract/result.schema.json` 的 `DIR/result.json`：每张图库图片一项，每条文字查询一项，各自带有自己那几次调用的账本行。

完整数据集（400 张图片、100 个近重复变体、40 条文字查询，73 MB）是仓库的发布附件（见 `datasets/README.md`）。其清单已提交为 `assets/full.json`，图片和标准答案在压缩包里。把压缩包解压到 `assets/`，再在仓库根目录对参考实现运行完整数据集。一次运行按标价最多 51 credits（500 张图片 x 0.10 加 40 条文本 x 0.01，合计 50.4），约 1,040 次调用；只有已充值账户能在一个 UTC 日内完成，公共池账户会因每日额度用尽而提前停止，需要在之后的日子重复运行。

```bash
tar xzf 05-image-library-search-full.tar.gz -C use-cases/05-image-library-search/assets
export FOLDBUN_API_KEY="fbs_..."
uv run python -m harness live --size full --uc 05 --max-credits 51 --dry-run   # 只打印计划，不发送请求
uv run python -m harness refresh --size full --uc 05 --max-credits 51          # 运行、打分，写入 expected/full.json 与 expected/full-summary.json，之后执行 `uv run python -m harness snippets`
```

`harness live` 运行方式相同，只按 `usecase.toml` 的 `[quality]` 下限检查结果；指标即 `evaluate.py --result out/result.json --truth assets/truth.json` 的输出。

Go 实现是同一条管线的单个命令：`cd use-cases/05-image-library-search/go && go run . --manifest ../assets/smoke/manifest.json --out out`。它只用标准库，读写的索引格式与 Python 相同，因此一种语言建好的索引可以用 `--resume` 交给另一种语言继续。Go 没有对应 `imgsearch.py` 的部分（针对文件夹的命令只有 Python 版），并且在 Go 中 `--limit 0` 表示全部条目。

## 代码 <!-- #code -->

客户端文件（`stack_client.py`、`stackclient/client.go`）是 `clients/` 中对应文件的副本（重试、幂等键、每分钟令牌桶、额度上限和账本）。以下是本用例新增的部分。

**遍历图库的循环。** 缺少向量的图片被切成块，依次交给每个模型嵌入。两个模型逐块交替，这样额度用完时，图库的前一部分两种向量都已齐备。

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

**一个批处理。** 流中的每一项到达时即处理。一项的账本行就是该批处理标签下的最后一行。因可能恢复的原因失败的项（`image_not_found`、`model_busy`、过期项）进入下一次尝试；没有 summary 就结束的流，会把未完成的项重新发送；额度用完或达到额度上限则停止运行。批处理的幂等键由内容派生，因此重跑被中断的一块时，已完成的项会被重放，不再计费。

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

**磁盘上的索引。** 向量文件从不原地修改。提交时先写新文件，再原子地替换 `index.json`，最后删除不再被引用的文件。崩溃后留下的是旧索引或新索引。

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

**精确检索。** 向量已做 L2 归一化，一次矩阵与向量的乘法即得全部余弦相似度。

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

**近重复。** 超过阈值的图片对按块查找（内存有界），再用并查集合并。

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

**每次运行都检查增量重建。** 建库之后，`main.py` 再建一次。此时没有缺失项，第二次必须不发出任何 API 调用；调用数记入 `stats.json`，发出调用则运行失败。

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

**文字查询。** 每条查询一次调用，使每条查询的费用和延迟落在各自的项里。文本也可以合并发送（每次最多 64 条），以少用每日额度的调用次数；两种方式的价格都按文本条数计。

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

## 检索服务 <!-- #service -->

Go 程序还有第二种模式：对先前运行写出的索引（Python 或 Go 写的都可以，格式相同）提供检索服务：`go run . serve --index out/index`。默认监听 `127.0.0.1:8080`（可用 `--addr` 修改），只读（不通过 HTTP 建库），返回 JSON，所有错误统一为 `{"error": {"code": "...", "message": "..."}}`。

```bash
curl "127.0.0.1:8080/search?q=a+photo+of+a+dog&k=10"   # 符合一段文字的图片（siglip2）
curl "127.0.0.1:8080/similar?id=5177b735e298df6c&k=10"  # 与某张图库图片相似的图片（dinov3）
curl "127.0.0.1:8080/duplicates?threshold=0.90"         # 近重复分组（dinov3）
curl "127.0.0.1:8080/healthz"
```

`/search` 通过客户端用 `siglip2` 嵌入文字，因此每次查询是一次计费调用（标价 0.01 credits；在包含的每日调用数用完之前，它计为其中一次）；服务启动时需要 `FOLDBUN_API_KEY`，缺少时只有 `/search` 关闭。`/similar` 和 `/duplicates` 读取已存的向量，不发出 API 调用。`k` 缺省为 10，超过 100 时按 100 处理；`k` 不合法、`q` 为空或过长、阈值不在 (0, 1] 内时返回 400，图片 id 不存在返回 404，GET 以外的方法返回 405，API 失败返回 502（每日额度、余额或额度上限用尽时为 429）。收到 SIGINT 或 SIGTERM 后，服务在处理中的请求结束后退出。

路由表和服务器设置（超时、请求头上限、平滑停止）就是服务的全部：

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

检索是对全部向量的暴力遍历，几万张以内没有问题；更大的规模见下文的局限。

## 结果 <!-- #results -->

`evaluate.py`（仅用标准库，读取 `result.json`、`stats.json` 和真值）度量的内容：

- **以图搜图。** 每个近重复变体作为一次查询；`i2i_recall_at_k` 是原图出现在最近 k 张图片中的变体所占比例。
- **文字搜图。** 40 条形如 "a photo of a <class>" 的查询，对照 Open Images 对原图的人工图像级标签评分：`text_precision_at_5`、`text_precision_at_10`、`text_recall_at_10`、`text_map`。变体按定义不相关，评分前先从排序中去掉。
- **近重复。** “相似度不低于阈值”这一判断在候选图片对上的精确率、召回率和 F1。阈值在数据的 a 半上选定（精确率保持在 0.99 以上的最低阈值），原样用于 b 半，并报告 b 半上的数字。
- **速度与成本。** 本地检索耗时（不涉及网络）、第二次建库发出的调用数（应为零），以及每 1,000 张图片的标价和实扣。建库吞吐取决于读者到 API 的网络路径，因此不予发布。

下方数字由 `expected/full-summary.json` 生成，维护者在完整数据集（约 500 张图片）上做一次真实运行后记录。在此之前该区块如实标明。`harness check` 的离线运行使用假 API，其向量是确定性的但没有语义：离线运行只验证流程（建出索引、第二次建库不发送任何请求、查询返回形状正确的排序结果），不验证质量，因此质量下限不适用于离线运行。

<!-- metrics: expected/full-summary.json -->
| 指标 | 数值 |
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

## 成本 <!-- #cost -->

`dinov3` 每张图 0.05 credits，`siglip2` 每张图 0.05 credits，每条文本 0.01 credits。smoke 数据集（25 张图片、5 条查询）按标价为 25 x 0.10 + 5 x 0.01 = 2.55 credits，共 55 次调用：`dinov3` 25 次、`siglip2` 25 次、文字查询 5 次。这在一个 UTC 日包含的 100 次调用之内，新账户不扣费。完整数据集（约 500 张图片、40 条查询）按标价为 50.4 credits，约 1,040 次调用：已充值账户的免费池按每个池内模型每个 UTC 日前 1,000 次调用免费；公共池账户需要约 11 天、每天 100 次，每次停止后建库可继续。

每次运行记录各自的标价和实扣：

<!-- cost -->
| 运行 | 调用次数 | 标价（credits） | 实扣（credits） |
|---|---|---|---|
| smoke | 55 | 2.55 | 0 |
| full | 1040 | 45.4 | 45.4 |
<!-- /cost -->

每 1,000 张图片的建库标价为 100 credits，仅一次；之后只对新增和改动的图片计费。文字查询每条 0.01 credits，图库内图片的查询不计费。

## 局限与不适用的情形 <!-- #limits -->

- **精确检索适用于这个量级。** 一次查询遍历全部向量：每 1,000 张图片约 3 MB（768 个值，每个 4 字节），1 万张图片需要几毫秒。超过大约 5 万到 10 万张，或需要在负载下几毫秒内响应时，应改用近似索引（FAISS、hnswlib，或数据库里的 pgvector）。接口保持不变：`top_k(matrix, query, k)` 换成对索引的调用，向量文件仍是数据的来源，流程的其余部分不变。全量两两比较的近重复查找随图库规模平方增长；到那个规模时，使用索引的范围检索，或先按粗粒度哈希分组。
- **近重复是对相似度设的阈值。** 默认值（0.90）只是起点。应在图库自己的带标注样本上选定，做法与 `evaluate.py` 相同；裁剪和重度重新压缩的得分通常低于缩放。连通分量会成链：A 与 B 相似、B 与 C 相似，三者就进入同一组。
- **只有图像级语义。** 两个模型描述的是整张图片，没有物体位置。`siglip2` 的输入是 224 px，小物体会丢失。文字查询以英文效果最好，得分是很小的数，只有排序有意义，因此不使用固定的截断值。
- **模型是索引的一部分。** 不同模型的向量不可比较，模型升级后旧向量与新向量也会不一致。索引记录每个模型的向量长度，遇到不同长度就拒绝；模型变化时需要重建索引。
- **每日额度。** 公共池每个 UTC 日包含 100 次调用。共享的池可能对两个模型共用一份额度；代码依据每次响应报告的数字行事，不做假设。批处理中的各项依次执行，N 张图的批处理耗时约为单张处理时间的 N 倍；每块应远小于批处理 600 秒的上限。
- **上传的图片由客户端保留。** 客户端文件在内存中保留已上传图片的字节，以便句柄被逐出时重新上传。图库很大时，请分多次运行建库。
- 参考文档：[Embeddings and search](https://stack.foldbun.com/docs/use-cases/embeddings/)、[DINOv3](https://stack.foldbun.com/docs/models/dinov3/)、[SigLIP 2](https://stack.foldbun.com/docs/models/siglip2/) 和 [Rate limits and pools](https://stack.foldbun.com/docs/rate-limits/)。`dinov3` 的输出受 DINOv3 License 约束；基于它构建的产品应显示标签 "Built with DINOv3"。

## 数据 <!-- #data -->

smoke 数据集包含 20 张原图、5 个近重复变体和 5 条文字查询，来自 `datasets/smoke/05-image-library-search/`（`assets/smoke/` 链接到它）。照片来自 Open Images V7 验证集（图片以 CC BY 2.0 由作者发布，标签为 CC BY 4.0）；变体由它们按记录的变换派生。作者和许可证见 `assets/smoke/ATTRIBUTION.csv`。完整数据集（400 张图片、100 个变体、40 条查询）是发布附件，说明见 `datasets/README.md`。
