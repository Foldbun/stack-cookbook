<!-- en-sha: 238692d93498 -->
# 用文字提示词预标注图像数据集

[English](README.md) | 简体中文

用 `sam3` 按文字提示词分批分割一个图片目录，把高置信度的标注导出为 COCO 和 YOLO-seg，其余的放入带裁剪图的复核队列。运行可以续跑，重复运行不会重复扣费，每张图都有一行成本记录。

## 问题 <!-- #problem -->

新的检测或分割数据集需要标注，而从零手绘是其中最贵的部分。`dog` 这样的文字提示词只需几百分之一个 credit 就能找到大部分实例，但结果只是带分数的猜测。预标注的数据集要有用，需要同时满足三点：

- 高置信度的标注可以直接使用，且是训练代码能读的格式（COCO、YOLO-seg）；
- 不确定的标注既不丢失，也不被悄悄接受：由人带着裁剪图快速过一遍；
- 对数千张图的运行，在连接中断、批次过期或进程重启后仍能继续，同一张图不会被重复扣费。

本示例针对 `datasets/` 中的数据集：60 张图、4 个类别（Dog、Cat、Bird、Boat）。其中 4 张图作为 smoke 集提交在仓库里。

## 管线 <!-- #pipeline -->

```
 images/ + manifest.json
        |  每个文件的 sha256
        v
 POST /v1/images           免费；handle = img_<sha256>，重复上传不产生费用
        |
        v   每批 100 张，最多 2 批并行，key = 轮次 + 序号 + 请求体 sha256
 POST /v1/batch/segment    每张图 4 个文字提示，filter.minScore 0.3，事件流
        |                  过期 / 可重试的项进入下一轮，断流则用同一个 key 重发
        v
 按分数分流
   >= 0.6          接受  ----> coco.json、yolo/labels/*.txt + data.yaml
   0.3 .. 0.6      复核  ----> coco-review.json、review/queue.csv + review/crops/*.jpg
   预期有该类但未检出 -> 复核（原因 empty_but_expected）
        |
        v
 result.json（分流结果、调用记录）      costs.csv（每张图一行）
```

1. 扫描 manifest，计算每个文件的 sha256；与 manifest 不符的文件会被拒绝。
2. 每个不同的文件上传一次。上传免费，handle 由文件内容派生，重复上传无害。
3. 把图片切成每批最多 100 张，每批作为一个 `POST /v1/batch/segment` 事件流发送。一批只占一个限速令牌；批内的图片在服务端依次处理。
4. 每张图带 4 个文字提示（`dog`、`cat`、`bird`、`boat`）。`filter.minScore` 取复核阈值（0.3），因此 API 返回复核者可能需要的全部结果，低于该阈值的不返回。
5. 每个实例按分数分流；manifest 的 `expectedClasses` 中预期存在却为空的类别也进入复核。
6. 导出其他工具要读的文件，并写出每张图的成本。

另有两种模式使用同一条管线。`--exemplar` 用于文字难以描述的类别：在图中给一个示例画一个框，模型在该图中找出更多相似的对象。`--boxes` 加 `--model sam-lite` 把已有检测器的框变成掩码。

## 运行 <!-- #run -->

```bash
export FOLDBUN_API_KEY="fbs_..."
cd use-cases/01-dataset-prelabelling/python
uv run main.py --manifest ../../../datasets/smoke/01-dataset-prelabelling/manifest.json --out out
```

账户需要有余额，因为 `sam3` 从余额扣费。smoke 集按标价需要 2.8 credits。设置 `FOLDBUN_MAX_CREDITS` 后，客户端会拒绝任何可能超出该上限的调用。

| 选项 | 默认值 | 含义 |
|---|---|---|
| `--concurrency N` | 2 | 同时进行的批数；账户同时可开 1 到 4 个批，遇到 `429 too_many_batches` 会等待后重试 |
| `--batch-size N` | 100 | 每个批请求的图片数（API 允许每批 1,000 项、事件流最长 600 s） |
| `--accept` / `--review` | 0.6 / 0.3 | 达到前者的直接接受；介于两者之间的进入复核；低于 `--review` 的不请求 |
| `--rounds N` | 3 | 过期或失败的项最多重发几轮 |
| `--resume` | 关 | 跳过同一 `--out` 目录中此前已完成的图 |
| `--limit N` | 全部 | 只处理 manifest 的前 N 项 |
| `--masks` | 关 | 同时请求掩码；COCO 的 segmentation 改为 RLE，保留孔洞 |
| `--exemplar FILE` | | 示例框模式，见下 |
| `--boxes FILE --model sam-lite` | | 框模式，见下 |

manifest 的每一项除 `id` 和 `image` 外，还可以带 `prompts`（默认取 manifest 的 `classes` 的提示词）、`sha256` 和 `expectedClasses`，即已有的图像级标签（例如来自文件夹名）。预期有某个类别却没有检出时，会生成一行复核记录。

写入 `--out` 的文件：

| 文件 | 内容 |
|---|---|
| `result.json` | 每张图的状态、分流结果、账本行（`contract/result.schema.json`） |
| `coco.json` | 被接受的标注，COCO instances 格式，每个标注带 `score` |
| `coco-review.json` | 复核档的检测，格式相同，供能导入预标注的工具使用 |
| `yolo/labels/*.txt`、`yolo/data.yaml` | 被接受的标注，YOLO 分割格式；把图片链接或复制到 `yolo/images/` |
| `review/queue.csv`、`review/crops/` | 每个待检查的标注一行、一张 JPEG 裁剪图 |
| `costs.csv` | 每张图的标价、实扣、缓存、耗时和计数 |
| `raw/` | 每张已完成图的原始返回；`--resume` 读取它 |

示例框模式读取一个 JSON 文件，每张图一个示例。没有条目的图不会发送。正框是该类的示例，负框是要排除的相似物；框不额外收费，一张图按一个提示计费。

```json
{ "class": "Bird", "items": { "16774edaeacc5aed": { "positive": [[162, 121, 894, 685]], "negative": [] } } }
```

框模式读取一个 CSV，列为图像像素坐标 `x1,y1,x2,y2`，类别写在 `label` 列。每个框是 `sam-lite` 的一个对象提示，每框 0.01 credits，另加图像编码费。

```bash
uv run main.py --manifest ... --out out-boxes --boxes detections.csv --model sam-lite
```

完整数据集是 release 附件。把 `01-dataset-prelabelling-full.tar.gz` 解压到 `assets/`（见 `assets/full.json`），再传入 `assets/full.json`；`evaluate.py` 随后按 `assets/truth.json` 给这次运行打分。

### Go 版本

`go/` 用相同的参数产出相同的 `result.json`、导出文件和 `costs.csv`，写法则按长期运行的批处理任务来做。Python 版在 `raw/` 中为每张图片保存一个文件，Go 版有三处不同：

- **检查点文件。** `--out` 下的 `checkpoint.jsonl` 每完成一张图片追加一行，图片一到就写入并同步，而不是等整批结束。`--resume` 读取该文件，跳过请求未变的图片，并把它们的调用记为重放。因崩溃而被截断的最后一行会被忽略。
- **context 取消。** 整个运行过程接受一个 `context.Context`。取消之后不再启动新的批次或轮次，轮次之间的等待也立即结束。
- **收到 SIGINT 或 SIGTERM 时停止。** 进行中的批次会完成并写入检查点，导出文件和 `result.json` 按已有内容写出，程序以退出码 0 结束。从未发送的图片带有错误码 `interrupted`，再次以 `--resume` 运行即可补完。再收到一次信号则立即退出。

```bash
cd use-cases/01-dataset-prelabelling/go
go run . --manifest ../../../datasets/smoke/01-dataset-prelabelling/manifest.json --out out
```

标准库是唯一的依赖，因此复核裁剪图只对 JPEG 和 PNG 生成（会应用 EXIF 方向）；其他格式的图片照常分割，其复核行的 `crop` 为空。`go test ./...` 覆盖路由阈值、多边形的半像素偏移、批次键的派生和检查点的往返读写。

## 代码 <!-- #code -->

文件都很小，便于复制：`main.py`（参数、串联、`result.json`）、`batches.py`（扫描、上传、批次、续跑）、`routing.py`（分数档）、`exports.py`（COCO、YOLO、复核队列、成本），以及两个客户端文件。Go 版按同样的方式拆分（`main.go`、`batches.go`、`routing.go`、`exports.go`），另有 `crop.go` 负责复核裁剪图。

客户端从环境变量读取密钥和基础 URL，从不打印密钥：

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

发送任何请求之前先给每个文件算哈希。manifest 里有 `sha256` 时会核对，所以图片被改动会尽早失败，而不是给错误的文件生成标注：

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

上传免费，handle 是 `img_` 加图像的 sha256。同一个文件上传两次得到同一个 handle：

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

一张图对应一个批项。提示词就是类别；`minScore` 是复核阈值；重叠去除只在同一类别内进行；多边形不带孔洞，因为 COCO 多边形无法表示孔洞：

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

批的幂等 key 由轮次、序号和请求体的 sha256 组成。manifest 和选项相同，key 就相同，因此重复运行会重放已完成的项（`replayed: true`，不扣费）。改动选项会改变请求体和 key，这样被改动的项不会与旧项冲突（`idempotency_conflict`）：

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

批循环读取事件流。流在没有 `summary` 的情况下结束，说明被切断，此时用同一个 key 重发同一个请求体。每个 `ok` 项取得客户端刚写入的账本行；返回 `expired` 或 `error` 的项照实记录：

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

轮次：过期或以可重试错误码失败的图进入新的批（新的请求体、新的 key）。不可重试的错误，例如 `invalid_image`，保持为错误并列在 `result.json` 中：

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

分流只有几行。阈值做成选项，因为合适的值取决于类别：

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

API 的多边形顶点是像素中心，所以导出的每个顶点都要加 0.5，落到 COCO 和 YOLO 使用的像素格坐标上。框已经是像素边界。COCO 的 `bbox` 是 `[x, y, width, height]`：

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

YOLO-seg 每行写一个归一化多边形，每个部分一行。没有标注的图得到一个空文件，YOLO 会把它读作背景图：

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

复核队列是电子表格或标注工具都能加载的 CSV，每行配一张裁剪图；预期类别没有检出的图也有对应的行：

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

`costs.csv` 由每张图的账本行生成，因此也能看到重放和缓存命中：

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

### 导出文件如何验证

`tests/test_prelabelling.py` 针对假 API 运行程序，并按格式检查输出：COCO 的 id、类别、图内的框、至少 3 个顶点且位于半像素位置并落在框内的多边形环；带 `--masks` 时，RLE 的 segmentation 能解码（`clients/python/coco_rle.py`，已用 pycocotools 的向量测试）并与标注的面积和框一致；YOLO 每行有整数类别、偶数个取值于 0 到 1 的坐标，`data.yaml` 的类别名与之对应。测试还覆盖续跑、过期项、断流和另外两种模式。本仓库没有把 COCO 文件载入 pycocotools 或标注工具做验证。

## 实测结果 <!-- #results -->

下列数字在 60 张图的数据集上用 `sam3` 测得；维护者记录一次运行之前，该块保持为空。数字在多次运行之间会有小幅变化，因为被缓存的图像编码可能让结果略有不同。

数字的算法（`evaluate.py`）：

- 按类别在 IoU 0.5 下计算 precision 和 recall，采用 Open Images 的规则：只在该类经人工验证存在或不存在的图上计数；在该类未验证的图上，检测既不算对也不算错。`precision@0.5` 和 `recall@0.5` 只用被接受的标注；`*_with_review` 还包含复核档。
- `review_share` 是复核队列在全部检测与「预期有但为空」的行中所占的比例。`review_true_positive_share` 表示复核档检测中最终正确的比例，据此判断复核是否值得。
- `cost_per_1000_images` 是每 1,000 张的预算价：各次调用的标价，并对每次调用都计入图像编码费。图像的编码仍在缓存中时，调用的标价不含这项费用，而缓存命中没有保证，因此预算应以此数为准；`cost_per_1000_images_this_run` 是所记录的这次运行实际的标价，`encoding_cache_hit_share` 是其中命中缓存的调用占比。吞吐量不公布：它取决于客户端到 API 的网络路径（`evaluate.py --timing` 可在自己的运行上输出）。

<!-- metrics: expected/full-summary.json -->
| 指标 | 数值 |
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

## 成本 <!-- #cost -->

带 4 个文字提示的一张图，费用是一次图像编码（0.5）加 4 个提示（4 x 0.05）：按标价 0.70 credits，每 1,000 张约 700 credits。如果同一张图刚被编码过（例如换提示词重跑），编码费会被免除，但规划时不要依赖。示例框模式下一张图 0.55（一个提示，不论多少个框）。`sam-lite` 的框模式下为 0.2 加每框 0.01。重放的项不扣费。上传免费。

<!-- cost -->
| 运行 | 调用次数 | 标价（credits） | 实扣（credits） |
|---|---|---|---|
| smoke | 4 | 2.8 | 2.8 |
| full | 60 | 13.5 | 13.5 |
<!-- /cost -->

## 局限与不适用的情形 <!-- #limits -->

- 文字提示词适用于常见类别。稀有、细粒度或专业类别（医学、工业缺陷）的召回率较低：标注数千张图之前，先在小样本上量一下，并考虑 `--exemplar`。
- 框或掩码只是候选。复核队列存在的原因是分数不是概率；它不能代替对导出结果的最终人工检查。
- 很小的物体（小于图像短边的约 1/50）常被漏掉，严重重叠的人群或物体可能被合并成一个实例。
- COCO 多边形没有孔洞，所以有孔洞的物体在 `coco.json` 和 YOLO 文件中会被填满。需要保留孔洞时用 `--masks` 导出 RLE 的 segmentation。YOLO 导出始终使用多边形。
- 框模式下的分数是模型预测的掩码 IoU，不是检测器的置信度，并且不会丢弃任何框：低于 `--accept` 的都进入复核。
- 示例框只对画框的那张图有效。它在该图中找出更多实例，不会让模型为数据集的其余部分学会一个类别。
- 批内的图片依次处理：100 张图的一批需要几分钟而不是几秒，一个事件流最长 600 s。放不下的项会过期，并在下一轮重发。
- 幂等 key 保留 24 小时。`--resume` 使用本地的 `raw/` 副本，所以更晚也能续跑；但服务端已完成、进程崩溃前却没收到的项，可能被再次扣费。
- 复核行和裁剪图是给人看的。裁剪用 Pillow，是唯一的依赖。
- 文档中的相关页面：[Segmentation with text prompts](https://stack.foldbun.com/docs/use-cases/concept-segmentation/) 和 [Post-processing](https://stack.foldbun.com/docs/post-processing/)。

## 数据 <!-- #data -->

60 张图取自 Open Images Dataset V7 验证集。标注为 CC BY 4.0（Google LLC），图片为 CC BY 2.0，版权归各作者；提交在仓库中的 smoke 图片及其署名在 `datasets/smoke/01-dataset-prelabelling/`，其余见 `datasets/attribution/ATTRIBUTION-01-dataset-prelabelling.csv`。`fixtures/` 中的离线夹具是合成的：由 `fixtures/synthesize.py` 按真值框画出，不是模型输出。
