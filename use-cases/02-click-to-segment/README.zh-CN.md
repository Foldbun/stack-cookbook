<!-- en-sha: 9074f98eb91c -->
# 点击分割组件与持有密钥的后端

[English](README.md) | 简体中文

在浏览器里点几下，分割出一个物体。一个小型后端持有 API 密钥，每张图只上传到 Foldbun Stack 一次，并把每次点击连同之前的全部点击一起发给 `sam-lite`；浏览器拿回的是多边形，始终接触不到密钥。点击模拟器按真人的方式驱动后端，实测一个物体需要几次点击、分割质量如何、花费多少。

## 问题 <!-- #problem -->

标注工具、图片编辑器或商品目录常需要「点一下物体，得到它的轮廓」。模型调用本身并不难，决定能否放到公开页面上的是调用之外的部分：

- **密钥不能进入浏览器。** 页面里的任何内容访问者都能读到，所以由后端发起调用，页面只和这个后端通信。
- **每次点击都是一次计费调用。** `sam-lite` 的标价是每次细化 0.01 credits，另加图像编码 0.2。Stack 会缓存编码结果，第一次之后的点击通常只收 0.01，但缓存不作保证，预算应按每次点击都未命中来规划。
- **模型不保存状态。** `POST /v1/refine` 每次都接收该物体的全部点。后端也一样：只保存图像，不保存点击，因此刷新页面或打开第二个标签页都不会造成状态错位。
- **访问者共用一个账户。** 账户的免费池每分钟提供 60 次调用。后端按会话限速，客户端文件再按池的速率统一调度全部调用，单个高频访问者无法占满所有人的配额。
- **点击延迟会被直接感知。** 交互式调用若 2 秒内未完成，会以可重试的 `504` 失败；后端报告每次 Stack 调用的耗时，供页面显示。客户端看到的延迟取决于它到 API 的网络路径，因此本用例的实测不包含这一项。

## 工作方式 <!-- #design -->

各语言的后端实现同一份 HTTP 契约 `contract/02-backend.yaml`：

| 请求 | 应答 |
|---|---|
| `POST /api/images`（图像原始字节） | `{imageId, width, height}`；同一内容只上传到 Stack 一次，并缓存其 handle |
| `POST /api/refine {imageId, points, box?}` | `{polygon, bbox, area, score, model, costCredits, listCredits, cache, latencyMs}` |
| `GET /api/usage` | 本会话的点击数、credits、缓存命中数和账本行 |
| `GET /api/health` | 就绪状态、语言和客户端版本 |

除 `/api/health` 外，每个请求都带 `X-Session-Id`，由页面自行生成（每次加载页面一个）。点和多边形采用 Stack API 的坐标：应用 EXIF 方向后的图像像素，x 向右，y 向下。错误只有一种格式 `{"error": {"code", "message", "retryable", "retryAfterMs"}}`；Stack 错误与后端错误码的对应关系见契约。

`POST /v1/refine` 默认返回带孔洞的简化多边形，因此后端直接转发 Stack 的多边形，不需要解码掩码。

## 组件 <!-- #component -->

`<stack-segmenter>` 是原生 Web Component：不依赖框架，也没有运行时依赖，约 400 行 TypeScript，位于 [`web/stack-segmenter.ts`](web/stack-segmenter.ts)，在 shadow root 中自带最少的样式。无需浏览器即可测试的部分（坐标换算、点列表、请求队列、多边形转路径）放在 [`web/logic.ts`](web/logic.ts)，对应的 `web/logic.test.ts` 用 `node --test` 运行。组件文件按 `upload`、`click`、`request`、`draw`、`export` 五个区域组织。

- **点击。** 左键添加正点（点在物体上），右键或 Alt+点击添加负点（排除这一处）。撤销（按钮或 Ctrl/Cmd+Z）删除最后一个点，清除删除全部点。每次点击都发送**全部**点，因为 `POST /api/refine` 不保存点击状态。
- **一次一个请求。** 请求进行期间的点击先排队；累积的点击合并成一个后续请求发送，已经过时的请求，其返回结果直接丢弃。
- **缩放。** 点击位置换算为图像自身的像素（`naturalWidth` x `naturalHeight`，已应用 EXIF 方向，即 API 的坐标），多边形连同孔洞按显示尺寸画回图上，窗口缩放后同样重画。
- **数字。** 得分、点数、上一次点击扣的 credits、缓存命中或未命中及延迟，以及本会话的调用次数和 credits（来自 `GET /api/usage`）。
- **错误。** 后端标记为可重试的 `429` 和 `503` 在 `retryAfterMs` 之后重试（最多三次）；`404 image_not_found` 会重新上传一次图像。其余情况只按错误码显示一句通俗的话，不显示后端自己的文字。
- **键盘。** 按钮是带标签的真正按钮。图像区域可以获得焦点：方向键移动光标（配合 Shift 步长更大），Enter 或空格添加正点，Shift+Enter 或 Alt+Enter 添加负点。

### 构建并打开

```bash
cd use-cases/02-click-to-segment/web
pnpm install
pnpm build                               # tsc：在 dist/ 生成可直接用于浏览器的 ES 模块和 index.html
cd ../typescript
node main.ts --port 8080                 # 需设置 FOLDBUN_API_KEY；提供 ../web/dist 中的文件
```

打开 `http://127.0.0.1:8080/`，选择或拖入一张图像后点击即可。后端提供 `--static <dir>`（默认 `../web/dist`）目录中的文件：`GET /` 对应 `index.html`，`GET /<name>` 对应该目录下直接存放的文件，其余一概不提供（`contract/02-backend.yaml` 的 “Static files” 一节）。

### 嵌入自己的页面

```html
<script type="module" src="/stack-segmenter.js"></script>

<stack-segmenter id="seg" model="sam-lite" src="/photo.jpg"></stack-segmenter>

<script>
  document.getElementById("seg").addEventListener("segment", (e) => {
    console.log(e.detail.polygon); // [{outer: [x0, y0, x1, y1, ...], holes: [[...]]}]，原图像素
  });
</script>
```

将 `web/dist/` 中的 `stack-segmenter.js` 与 `logic.js` 一起复制到站点，二者放在同一目录。

| 属性 | 含义 |
|---|---|
| `backend` | 后端的基础 URL，默认为页面自身的源。 |
| `model` | 要使用的模型（`sam-lite` 或 `sam3`），后端的 `SEGMENTER_MODELS` 允许时才有效，默认为后端的第一个模型。 |
| `src` | 要加载的图像 URL。不设置时，由使用者选择文件或把文件拖到组件上。该 URL 必须能被页面读取。 |

属性：`backend`、`model`、`src` 与同名 HTML 属性一致；`sessionId` 是本次页面加载的标识（`crypto.randomUUID()`）；`load(fileOrUrl)` 加载图像；`undo()` 与 `clear()`；`toJSON()` 返回当前结果，没有结果时为 `null`。

**`segment` 事件**在每次得到返回结果后触发，`detail` 为 `{imageId, width, height, model, score, points, polygon}`；`polygon` 是后端返回的多边形部件列表，每个部件含 `outer` 环和可选的 `holes`，单位为原图像素。“导出 JSON”按钮把同一对象保存为 `segment.json`。

样式通过元素上的 CSS 自定义属性调整：`--seg-accent`、`--seg-positive`、`--seg-negative`、`--seg-fill`、`--seg-text`、`--seg-muted`、`--seg-surface`、`--seg-border`、`--seg-hover`、`--seg-radius`。`web/index.html` 是设置了这些属性的小型演示页。

### 为什么密钥不会到达浏览器

组件只调用本后端的三个路由：`POST /api/images`、`POST /api/refine` 和 `GET /api/usage`。Foldbun Stack API 的密钥由后端持有，后端负责每张图只上传一次、调用 Stack，并在返回结果时去掉账户信息。因此页面不可能泄露密钥，限制也都放在后端（按会话限速、`SEGMENTER_MODELS`、`FOLDBUN_MAX_CREDITS`）。会话只是计量单位，不是身份认证：页面公开时，应把后端放在站点自己的登录之后。

### 同源要求

后端不发送 CORS 头，所以页面与后端必须同源：要么由后端直接提供页面（如上所述），要么把两者放在同一个反向代理之后，并把 `/api/` 转发给后端。位于其他源的页面无法通过 `backend="https://..."` 调用后端。

## 运行 <!-- #run -->

```bash
export FOLDBUN_API_KEY="fbs_..."
export FOLDBUN_MAX_CREDITS=2           # optional: the client refuses calls that could pass it
cd use-cases/02-click-to-segment/typescript
pnpm install
node main.ts --port 8080               # listening on http://127.0.0.1:8080
# or: cd ../python && uv run main.py --port 8080   # the same backend in Python
# or: cd ../go && go run . --port 8080             # the same backend in Go
```

在第二个终端中：

```bash
S=demo-1
curl -s -H "X-Session-Id: $S" -H "Content-Type: image/jpeg" --data-binary @../assets/smoke/images/77bfbbef28ac344c.jpg \
  http://127.0.0.1:8080/api/images
curl -s -H "X-Session-Id: $S" -H "Content-Type: application/json" \
  -d '{"imageId": "im_...", "points": [{"x": 521, "y": 424}]}' http://127.0.0.1:8080/api/refine
curl -s -H "X-Session-Id: $S" http://127.0.0.1:8080/api/usage
```

参数（三种语言相同）：`--port`（默认 8080）、`--host`（默认 127.0.0.1）和 `--static DIR`（默认 `../web/dist`），即页面所在目录：`/` 返回其中的 `index.html`，`/<名称>` 返回该目录下直接存放的文件。`SEGMENTER_MODELS` 是请求可以指定的模型列表，以逗号分隔（默认 `sam-lite`；如需对比可加入 `sam3`）。后端不发送 CORS 头：页面和后端应部署在同一个源下。

要实测一个后端，由 harness 在空闲端口上启动它并运行点击模拟器：有密钥时用 `uv run python -m harness live --uc 02`，离线对假 API 用 `uv run python -m harness check --uc 02`。

完整集合（30 个物体，每个都有标准答案掩码，5 MB）是仓库的发布附件（见 `datasets/README.md`）。其清单已提交为 `assets/full.json`，每个物体的点击上限为 10 次；图片、掩码和标准答案在压缩包里。把压缩包解压到 `assets/`，再在仓库根目录通过模拟器运行参考后端（TypeScript）。按标价最多 63 credits（30 个物体 x 10 次点击 x 0.21，即每次点击都未命中缓存），缓存正常时约 9 credits。对真实 API 模拟器每秒最多点击一次，因此运行需要几分钟。

```bash
tar xzf 02-click-to-segment-full.tar.gz -C use-cases/02-click-to-segment/assets
export FOLDBUN_API_KEY="fbs_..."
uv run python -m harness live --size full --uc 02 --max-credits 63 --dry-run   # 只打印计划，不发送请求
uv run python -m harness refresh --size full --uc 02 --max-credits 63          # 运行、打分，写入 expected/full.json 与 expected/full-summary.json，之后执行 `uv run python -m harness snippets`
```

`harness live` 运行方式相同，只按 `usecase.toml` 的 `[quality]` 下限检查结果。只测量 `sam-lite`；harness 不包含 `sam3` 对比轮。

## 代码 <!-- #code -->

### 只上传一次 <!-- #code-upload -->

`imageId` 由图像字节计算得出，所以同一文件在任何会话、任何语言中的 id 都相同，重复上传不产生费用。

<!-- code: upload-once -->
<details><summary>TypeScript (typescript/backend.ts)</summary>

```ts
private async uploadImage(req: IncomingMessage, s: Session): Promise<sc.Json> {
  this.limit(s.upload);
  const type = (req.headers["content-type"] ?? "").split(";")[0].trim().toLowerCase();
  if (!IMAGE_TYPES.has(type)) throw new ApiError(415, "unsupported_media_type", "send the raw bytes as image/jpeg, image/png or image/webp");
  const data = await readBody(req, LIMITS.imageBytes);
  if (data.length === 0) throw invalid("the body is empty: send the raw bytes of the image");
  const imageId = "im_" + createHash("sha256").update(data).digest("hex").slice(0, 32);
  let pending = this.images.get(imageId);
  if (pending) {
    this.images.delete(imageId); // most recently used goes last
  } else {
    // one Stack upload per distinct content; the client file keeps the bytes and uploads again if Stack evicts them
    pending = this.stack.upload(data, type).then((u) => ({ handle: u.id, width: u.width, height: u.height }));
    pending.catch(() => this.images.delete(imageId));
  }
  this.images.set(imageId, pending);
  while (this.images.size > LIMITS.images) this.images.delete(this.images.keys().next().value!);
  let img: Image;
  try {
    img = await pending;
  } catch (err) {
    throw fromStack(err);
  }
  s.uploads++;
  return { imageId, width: img.width, height: img.height };
}
```

</details>

<details><summary>Python (python/backend.py)</summary>

```python
def _upload_image(self, req: Request, s: Session) -> dict:
    self._limit(s.upload)
    content_type = (req.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type not in IMAGE_TYPES:
        raise ApiError(415, "unsupported_media_type", "send the raw bytes as image/jpeg, image/png or image/webp")
    data = req.read_body(IMAGE_BYTES)
    if not data:
        raise invalid("the body is empty: send the raw bytes of the image")
    image_id = "im_" + hashlib.sha256(data).hexdigest()[:32]
    with self._lock:
        pending = self._images.get(image_id)
        owner = pending is None
        if pending is None:
            pending = self._images[image_id] = Future()
        self._images.move_to_end(image_id)  # most recently used goes last
        while len(self._images) > MAX_IMAGES:
            self._images.popitem(last=False)
    if owner:
        # one Stack upload per distinct content; the client file keeps the bytes and uploads again if Stack evicts them.
        # Concurrent uploads of the same bytes wait for this one instead of uploading again.
        try:
            u = self.stack.upload(data, content_type)
            pending.set_result(Image(u["id"], u["width"], u["height"]))
        except BaseException as exc:
            with self._lock:
                if self._images.get(image_id) is pending:
                    del self._images[image_id]
            pending.set_exception(exc)
    try:
        img = pending.result()
    except Exception as exc:
        raise from_stack(exc) from exc
    with self._lock:
        s.uploads += 1
    return {"imageId": image_id, "width": img.width, "height": img.height}
```

</details>

<details><summary>Go (go/backend.go)</summary>

```go
// uploadImage handles POST /api/images: one Stack upload per distinct content, whatever the session.
func (b *Backend) uploadImage(r *http.Request, s *session) (any, error) {
	if err := s.limit(s.upload, b.now()); err != nil {
		return nil, err
	}
	contentType := strings.ToLower(strings.TrimSpace(strings.SplitN(r.Header.Get("Content-Type"), ";", 2)[0]))
	if !imageTypes[contentType] {
		return nil, newError(415, "unsupported_media_type", "send the raw bytes as image/jpeg, image/png or image/webp")
	}
	data, err := readBody(r, maxImageBytes)
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return nil, invalid("the body is empty: send the raw bytes of the image")
	}
	sum := sha256.Sum256(data)
	imageID := "im_" + hex.EncodeToString(sum[:])[:32]
	img, started := b.begin(imageID)
	if started {
		// the client file keeps the bytes and uploads again if Stack evicts them
		info, err := b.stack.Upload(data, contentType)
		b.finish(img, info, err)
	}
	if err := ready(r.Context(), img); err != nil {
		if r.Context().Err() != nil {
			return nil, err
		}
		return nil, fromStack(err)
	}
	s.mu.Lock()
	s.uploads++
	s.mu.Unlock()
	return map[string]any{"imageId": imageID, "width": img.width, "height": img.height}, nil
}
```

</details>
<!-- /code -->

### 一次点击 <!-- #code-refine -->

页面发送该物体的全部点；后端按图像尺寸检查这些点，并带上显式的 `positive` 转发给 `POST /v1/refine`。

<!-- code: refine -->
<details><summary>TypeScript (typescript/backend.ts)</summary>

```ts
private async refine(req: IncomingMessage, s: Session): Promise<sc.Json> {
  this.limit(s.refine);
  const raw = await readBody(req, LIMITS.refineBytes);
  let body: sc.Json;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    throw invalid("the body is not JSON");
  }
  const { imageId, points, box, model } = parseRefine(body, this.models);
  const pending = this.images.get(imageId);
  if (!pending) throw new ApiError(404, "image_not_found", "unknown imageId: upload the image again");
  this.images.delete(imageId); // a refine is a use of the image: most recently used goes last
  this.images.set(imageId, pending);
  const img = await pending.catch(() => {
    throw new ApiError(404, "image_not_found", "unknown imageId: upload the image again");
  });
  checkBounds(points, box, img);
  // Stateless: every click sends all points of the object. The body is the same in every language.
  const call: sc.Json = { model, image: sc.imageHandle(img.handle), points };
  if (box) call.box = box;
  const t = performance.now();
  let result: sc.Json;
  try {
    result = await this.stack.post("/v1/refine", call, { tag: s.tag });
  } catch (err) {
    if (err instanceof sc.StackError) this.log(`stack error: ${err.describe()}`);
    throw fromStack(err);
  }
  const latencyMs = Math.round(performance.now() - t);
  s.clicks++;
  // Stack returns the outline as a simplified polygon with holes by default: no mask decoding needed here.
  const inst = result.instances?.[0] ?? {};
  return {
    polygon: inst.polygon ?? [],
    bbox: inst.bbox ?? [0, 0, 0, 0],
    area: inst.area ?? 0,
    score: inst.score ?? 0,
    model: result.model ?? model,
    costCredits: result.cost ?? 0,
    listCredits: result.billing?.total ?? 0,
    cache: result.cache === "hit" ? "hit" : "miss",
    latencyMs,
  };
}
```

</details>

<details><summary>Python (python/backend.py)</summary>

```python
def _refine(self, req: Request, s: Session) -> dict:
    self._limit(s.refine)
    try:
        doc = json.loads(req.read_body(REFINE_BYTES).decode("utf-8"))
    except (ValueError, RecursionError):
        raise invalid("the body is not JSON") from None
    image_id, points, box, model = parse_refine(doc, self.models)
    with self._lock:
        pending = self._images.get(image_id)
        if pending is not None:
            self._images.move_to_end(image_id)
    not_found = ApiError(404, "image_not_found", "unknown imageId: upload the image again")
    if pending is None:
        raise not_found
    try:
        img = pending.result()
    except Exception:
        raise not_found from None
    check_bounds(points, box, img)
    # Stateless: every click sends all points of the object. The body is the same in every language.
    call: dict = {"model": model, "image": sc.image_handle(img.handle), "points": points}
    if box:
        call["box"] = box
    t = time.monotonic()
    try:
        result = self.stack.post("/v1/refine", call, tag=s.tag)
    except sc.StackError as exc:
        self.log(f"stack error: {exc}")
        raise from_stack(exc) from exc
    latency_ms = round((time.monotonic() - t) * 1000)
    with self._lock:
        s.clicks += 1
    # Stack returns the outline as a simplified polygon with holes by default: no mask decoding needed here.
    inst = (result.get("instances") or [{}])[0]
    return {
        "polygon": inst.get("polygon") or [],
        "bbox": inst.get("bbox") or [0, 0, 0, 0],
        "area": inst.get("area", 0),
        "score": inst.get("score", 0),
        "model": result.get("model") or model,
        "costCredits": result.get("cost", 0),
        "listCredits": (result.get("billing") or {}).get("total", 0),
        "cache": "hit" if result.get("cache") == "hit" else "miss",
        "latencyMs": latency_ms,
    }
```

</details>

<details><summary>Go (go/backend.go)</summary>

```go
// refine handles POST /api/refine: all points of the object arrive every time, the backend keeps no click state.
func (b *Backend) refine(r *http.Request, s *session) (any, error) {
	if err := s.limit(s.refine, b.now()); err != nil {
		return nil, err
	}
	raw, err := readBody(r, maxRefineBytes)
	if err != nil {
		return nil, err
	}
	req, err := parseRefine(raw, b.models)
	if err != nil {
		return nil, err
	}
	notFound := newError(404, "image_not_found", "unknown imageId: upload the image again")
	img := b.lookup(req.imageID)
	if img == nil {
		return nil, notFound
	}
	if err := ready(r.Context(), img); err != nil {
		if r.Context().Err() != nil {
			return nil, err
		}
		return nil, notFound
	}
	if err := checkBounds(req.points, req.box, img.width, img.height); err != nil {
		return nil, err
	}
	// Stateless: every click sends all points of the object.
	call := stackRefine{Model: req.model, Image: stackclient.ImageHandle(img.handle), Points: req.points, Box: req.box}
	start := time.Now()
	data, err := b.stack.Post("/v1/refine", call, stackclient.Opts{Tag: s.tag})
	if err != nil {
		b.log("stack error: " + err.Error())
		return nil, fromStack(err)
	}
	latencyMs := int(time.Since(start).Round(time.Millisecond) / time.Millisecond)
	var result stackResult
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, fmt.Errorf("unreadable answer of Stack: %w", err)
	}
	s.mu.Lock()
	s.clicks++
	s.mu.Unlock()
	// Stack returns the outline as a simplified polygon with holes by default: no mask decoding needed here.
	out := map[string]any{
		"polygon":     json.RawMessage("[]"),
		"bbox":        []float64{0, 0, 0, 0},
		"area":        0,
		"score":       0,
		"model":       req.model,
		"costCredits": result.Cost,
		"listCredits": result.Billing.Total,
		"cache":       "miss",
		"latencyMs":   latencyMs,
	}
	if result.Model != "" {
		out["model"] = result.Model
	}
	if result.Cache == "hit" {
		out["cache"] = "hit"
	}
	if len(result.Instances) > 0 {
		inst := result.Instances[0]
		if len(inst.Polygon) > 0 && string(inst.Polygon) != "null" {
			out["polygon"] = inst.Polygon
		}
		if len(inst.BBox) > 0 && string(inst.BBox) != "null" {
			out["bbox"] = inst.BBox
		}
		out["area"], out["score"] = inst.Area, inst.Score
	}
	return out, nil
}
```

</details>
<!-- /code -->

### 会话与限速 <!-- #code-limits -->

每个会话有两个令牌桶：细化每分钟 60 次、突发 10 次；上传每分钟 10 次、突发 5 次。超出限额时返回 `429 rate_limited` 并带 `Retry-After`。

<!-- code: session-limit -->
<details><summary>TypeScript (typescript/backend.ts)</summary>

```ts
/** Token bucket: `perMinute` sustained, `burst` at once. take() returns 0 when allowed, else the wait in ms. */
export class RateLimit {
  private tokens: number;
  private at: number;
  private readonly perMinute: number;
  private readonly burst: number;

  constructor(perMinute: number, burst: number, now: number) {
    this.perMinute = perMinute;
    this.burst = burst;
    this.tokens = burst;
    this.at = now;
  }

  take(now: number): number {
    this.tokens = Math.min(this.burst, this.tokens + ((now - this.at) * this.perMinute) / 60_000);
    this.at = now;
    if (this.tokens >= 1) {
      this.tokens -= 1;
      return 0;
    }
    return Math.ceil(((1 - this.tokens) * 60_000) / this.perMinute);
  }
}

interface Session {
  id: string;
  tag: string; // ledger tag of this session's calls in the client file
  seen: number;
  uploads: number;
  clicks: number;
  refine: RateLimit;
  upload: RateLimit;
}
```

</details>

<details><summary>Python (python/backend.py)</summary>

```python
class RateLimit:
    """Token bucket: `per_minute` sustained, `burst` at once. take() returns 0 when allowed, else the wait in ms."""

    def __init__(self, per_minute: float, burst: float, now: float):
        self.per_minute, self.burst, self.tokens, self.at = per_minute, burst, burst, now

    def take(self, now: float) -> int:
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.per_minute / 60_000)
        self.at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0
        return math.ceil((1 - self.tokens) * 60_000 / self.per_minute)


@dataclass
class Session:
    id: str
    tag: str  # ledger tag of this session's calls in the client file
    seen: float
    refine: RateLimit
    upload: RateLimit
    uploads: int = 0
    clicks: int = 0
```

</details>

<details><summary>Go (go/backend.go)</summary>

```go
// rateLimit is a token bucket: perMinute sustained, burst at once. take returns 0 when allowed, else the wait in ms.
type rateLimit struct {
	tokens, perMinute, burst float64
	at                       time.Time
}

func newRateLimit(perMinute, burst int, now time.Time) *rateLimit {
	return &rateLimit{tokens: float64(burst), perMinute: float64(perMinute), burst: float64(burst), at: now}
}

func (l *rateLimit) take(now time.Time) int {
	elapsedMs := float64(now.Sub(l.at).Milliseconds())
	l.tokens = math.Min(l.burst, l.tokens+elapsedMs*l.perMinute/60_000)
	l.at = now
	if l.tokens >= 1 {
		l.tokens--
		return 0
	}
	return int(math.Ceil((1 - l.tokens) * 60_000 / l.perMinute))
}

// session is an accounting and rate-limit unit, not authentication.
type session struct {
	id   string
	tag  string    // ledger tag of this session's calls in the client file
	seen time.Time // guarded by Backend.mu, like elem
	elem *list.Element

	mu              sync.Mutex // guards the rest
	uploads, clicks int
	refine, upload  *rateLimit
}

// limit takes one token from the bucket, or fails with 429 and the wait.
func (s *session) limit(bucket *rateLimit, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if wait := bucket.take(now); wait > 0 {
		return &apiError{Status: 429, Code: "rate_limited", Message: "too many requests in this session", Retryable: true, RetryAfterMs: wait}
	}
	return nil
}
```

</details>
<!-- /code -->

### 错误 <!-- #code-errors -->

Stack 的错误信息可能涉及账户情况，因此只记录在日志中，不转发；页面看到的是后端的错误码，上游错误另附 Stack 的错误码。

<!-- code: errors -->
<details><summary>TypeScript (typescript/backend.ts)</summary>

```ts
/** A backend error, sent as {"error": {code, message, retryable, retryAfterMs, upstreamCode?, requestId?}}. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly retryAfterMs: number;
  readonly upstreamCode: string | undefined;
  readonly requestId: string | undefined;
  readonly headers: Record<string, string> = {}; // extra response headers, e.g. Allow with a 405

  constructor(status: number, code: string, message: string, retryable = false, retryAfterMs = 0, upstreamCode?: string, requestId?: string | null) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.retryAfterMs = retryAfterMs;
    this.upstreamCode = upstreamCode;
    this.requestId = requestId ?? undefined;
  }

  body(): sc.Json {
    const { code, message, retryable, retryAfterMs, upstreamCode, requestId } = this;
    return { error: { code, message, retryable, retryAfterMs, ...(upstreamCode ? { upstreamCode } : {}), ...(requestId ? { requestId } : {}) } };
  }
}

/** Map an error of the client file (after its own retries) to the backend's error. Stack's message is not forwarded. */
export function fromStack(err: unknown): ApiError {
  if (err instanceof sc.BudgetExceeded) return new ApiError(503, "credit_cap", "the backend's credit cap is reached");
  if (err instanceof sc.StackError && !err.retryable) {
    return new ApiError(502, "upstream_refused", "Foldbun Stack refused the call", false, 0, err.code, err.requestId);
  }
  if (!(err instanceof sc.StackError)) {
    const internal = new ApiError(500, "internal", "internal error"); // not an answer or a network failure: a bug, no detail for the caller
    internal.cause = err;
    return internal;
  }
  return new ApiError(503, "upstream_busy", "Foldbun Stack is busy; try again", true, Math.max(1000, err.retryAfterMs), err.code, err.requestId);
}
```

</details>

<details><summary>Python (python/backend.py)</summary>

```python
class ApiError(Exception):
    """A backend error, sent as {"error": {code, message, retryable, retryAfterMs, upstreamCode?, requestId?}}."""

    def __init__(self, status, code, message, retryable=False, retry_after_ms=0, upstream_code=None, request_id=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retryable, self.retry_after_ms = retryable, retry_after_ms
        self.upstream_code, self.request_id = upstream_code, request_id
        self.allow: str | None = None  # Allow header of a 405

    def body(self) -> dict:
        err = {"code": self.code, "message": self.message, "retryable": self.retryable, "retryAfterMs": self.retry_after_ms}
        if self.upstream_code:
            err["upstreamCode"] = self.upstream_code
        if self.request_id:
            err["requestId"] = self.request_id
        return {"error": err}


def from_stack(err: BaseException) -> ApiError:
    """Map an error of the client file (after its own retries) to the backend's error. Stack's message is not forwarded."""
    if isinstance(err, ApiError):
        return err
    if isinstance(err, sc.BudgetExceeded):
        return ApiError(503, "credit_cap", "the backend's credit cap is reached")
    if isinstance(err, sc.StackError):
        if not err.retryable:
            return ApiError(502, "upstream_refused", "Foldbun Stack refused the call", False, 0, err.code, err.request_id)
        return ApiError(503, "upstream_busy", "Foldbun Stack is busy; try again", True, max(1000, err.retry_after_ms), err.code, err.request_id)
    if isinstance(err, OSError | http.client.HTTPException | ValueError):  # no usable answer (for example, not JSON)
        return ApiError(503, "upstream_busy", "Foldbun Stack is busy; try again", True, 1000, "network_error")
    return ApiError(500, "internal", "internal error")
```

</details>

<details><summary>Go (go/backend.go)</summary>

```go
// apiError is sent as {"error": {code, message, retryable, retryAfterMs, upstreamCode?, requestId?}}.
type apiError struct {
	Status       int    `json:"-"`
	Allow        string `json:"-"` // Allow header of a 405
	cause        error  // what went wrong with a 500, for the log only
	Code         string `json:"code"`
	Message      string `json:"message"`
	Retryable    bool   `json:"retryable"`
	RetryAfterMs int    `json:"retryAfterMs"`
	UpstreamCode string `json:"upstreamCode,omitempty"`
	RequestID    string `json:"requestId,omitempty"`
}

func (e *apiError) Error() string { return e.Code + ": " + e.Message }

func newError(status int, code, message string) *apiError {
	return &apiError{Status: status, Code: code, Message: message}
}

// fromStack maps an error of the client file (after its own retries) to the backend's error.
// Stack's message is not forwarded.
func fromStack(err error) *apiError {
	var se *stackclient.Error
	switch {
	case !errors.As(err, &se): // not an answer or a network failure: a bug, no detail for the caller
		e := newError(500, "internal", "internal error")
		e.cause = err
		return e
	case se.Code == "budget_exceeded":
		return newError(503, "credit_cap", "the backend's credit cap is reached")
	case !se.Retryable:
		return &apiError{Status: 502, Code: "upstream_refused", Message: "Foldbun Stack refused the call", UpstreamCode: se.Code, RequestID: se.RequestID}
	}
	return &apiError{Status: 503, Code: "upstream_busy", Message: "Foldbun Stack is busy; try again", Retryable: true,
		RetryAfterMs: max(1000, se.RetryAfterMs), UpstreamCode: se.Code, RequestID: se.RequestID}
}
```

</details>
<!-- /code -->

### 用量 <!-- #code-usage -->

即客户端账本中属于本会话的行：页面可以据此显示点击的花费，模拟器则把这些行复制进 `result.json`。

<!-- code: usage -->
<details><summary>TypeScript (typescript/backend.ts)</summary>

```ts
private usage(s: Session): sc.Json {
  const calls = this.stack.calls(s.tag);
  const billed = calls.filter((c) => !c.replayed);
  const sum = (f: (c: sc.LedgerRow) => number) => Math.round(billed.reduce((a, c) => a + f(c), 0) * 1e6) / 1e6;
  return {
    sessionId: s.id,
    uploads: s.uploads,
    clicks: s.clicks,
    costCredits: sum((c) => c.charged),
    listCredits: sum((c) => c.list),
    cacheHits: calls.filter((c) => c.cache === "hit").length,
    cacheMisses: calls.filter((c) => c.cache === "miss").length,
    calls,
  };
}
```

</details>

<details><summary>Python (python/backend.py)</summary>

```python
def _usage(self, req: Request, s: Session) -> dict:
    calls = self.stack.calls(s.tag)
    billed = [c for c in calls if not c["replayed"]]
    with self._lock:
        uploads, clicks = s.uploads, s.clicks
    return {
        "sessionId": s.id,
        "uploads": uploads,
        "clicks": clicks,
        "costCredits": round(sum(c["charged"] for c in billed), 6),
        "listCredits": round(sum(c["list"] for c in billed), 6),
        "cacheHits": sum(1 for c in calls if c["cache"] == "hit"),
        "cacheMisses": sum(1 for c in calls if c["cache"] == "miss"),
        "calls": calls,
    }
```

</details>

<details><summary>Go (go/backend.go)</summary>

```go
// usage answers GET /api/usage: the session's counters and its rows of the client's ledger.
func (b *Backend) usage(s *session) any {
	calls := b.stack.Calls(s.tag)
	var cost, list float64
	var hits, misses int
	for _, c := range calls {
		if !c.Replayed {
			cost += c.Charged
			list += c.List
		}
		if c.Cache != nil && *c.Cache == "hit" {
			hits++
		} else if c.Cache != nil && *c.Cache == "miss" {
			misses++
		}
	}
	round6 := func(x float64) float64 { return math.Round(x*1e6) / 1e6 }
	s.mu.Lock()
	defer s.mu.Unlock()
	return map[string]any{
		"sessionId": s.id, "uploads": s.uploads, "clicks": s.clicks,
		"costCredits": round6(cost), "listCredits": round6(list),
		"cacheHits": hits, "cacheMisses": misses, "calls": calls,
	}
}
```

</details>
<!-- /code -->

### 服务 <!-- #code-server -->

<!-- code: server -->
<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
const { values } = parseArgs({
  options: { port: { type: "string", default: "8080" }, host: { type: "string", default: "127.0.0.1" }, static: { type: "string" } },
});
// The page's files: --static (relative to the working directory), by default the build of the web component next to this directory.
const staticDir = values.static ? resolve(values.static) : resolve(import.meta.dirname, "../web/dist");
let client: sc.StackClient;
try {
  client = new sc.StackClient(); // key, base URL and credit cap from FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
} catch (err) {
  if (!(err instanceof sc.MissingApiKey)) throw err;
  console.error(err.message);
  process.exit(EXIT_NO_KEY);
}
const models = (process.env.SEGMENTER_MODELS || "sam-lite")
  .split(",")
  .map((m) => m.trim())
  .filter(Boolean);
const backend = new Backend(client, { models, staticDir });
const server = createServer(backend.handle);
server.listen(Number(values.port), values.host, () => console.log(`listening on http://${values.host}:${values.port}`));

for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
    server.closeIdleConnections();
    setTimeout(() => process.exit(0), 4000).unref(); // the contract allows 5 s
  });
}
```

</details>

<details><summary>Python (python/main.py)</summary>

```python
try:
    client = sc.StackClient()  # key, base URL and credit cap from FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
except sc.MissingApiKey as err:
    print(err.message, file=sys.stderr)
    return EXIT_NO_KEY
models = [m.strip() for m in (os.environ.get("SEGMENTER_MODELS") or "sam-lite").split(",") if m.strip()]
server = Server(args.host, args.port, Backend(client, models=models, static_dir=args.static))

def stop(signum, frame):
    threading.Thread(target=server.shutdown).start()  # serve_forever runs in this thread: shutdown must be called from another

for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, stop)
print(f"listening on http://{args.host}:{server.server_address[1]}", flush=True)
server.serve_forever()
server.server_close()
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
func run(args []string) int {
	fs := flag.NewFlagSet("backend", flag.ContinueOnError)
	port := fs.Int("port", 8080, "port to listen on")
	host := fs.String("host", "127.0.0.1", "address to listen on")
	static := fs.String("static", "../web/dist", "directory with the page's files, served at /")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return exitFailed
	}
	// key, base URL and credit cap come from FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
	client, err := stackclient.New(stackclient.Config{})
	if err != nil {
		if stackclient.Code(err) == "missing_api_key" {
			fmt.Fprintln(os.Stderr, stackclient.MissingKey)
			return exitNoKey
		}
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	logger := log.New(os.Stderr, "", log.LstdFlags)
	backend := NewBackend(client, Options{Models: modelsFromEnv(), StaticDir: *static, Log: func(line string) { logger.Println(line) }})
	ln, err := net.Listen("tcp", net.JoinHostPort(*host, strconv.Itoa(*port)))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	srv := &http.Server{
		Handler:           backend,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       60 * time.Second, // up to 20 MiB of image
		WriteTimeout:      5 * time.Minute,  // a click may wait for the retries of the client file
		IdleTimeout:       60 * time.Second,
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	served := make(chan error, 1)
	go func() { served <- srv.Serve(ln) }()
	fmt.Printf("listening on http://%s\n", net.JoinHostPort(*host, strconv.Itoa(ln.Addr().(*net.TCPAddr).Port)))
	select {
	case err := <-served:
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 4*time.Second) // the contract allows 5 s
	defer cancel()
	if srv.Shutdown(shutdown) != nil {
		srv.Close()
	}
	return 0
}
```

</details>
<!-- /code -->

## 实测方法 <!-- #measurement -->

`harness/clickbot.py` 通过契约驱动任意一种后端，因此各语言的结果可以逐项比较。它遵循交互式分割的通行协议：第一次点击位于真值掩码内离边界最远的点；之后每次点击位于当前最大错误区域内离其边界最远的点，漏掉的区域加正点，多出的区域加负点。IoU 达到 0.90 或达到点击上限（smoke 4 次，full 10 次）时停止；对真实 API 每秒最多点击一次。

报告的指标有：NoC@85 和 NoC@90（IoU 达到 0.85 或 0.90 所需的点击数；始终未达到的物体按上限计）、k 次点击后的平均 IoU、每个物体的 credits，以及缓存命中率。`planning_credits_per_object` 对每次点击都计入图像编码费；`list_credits_per_object` 是所记录的这次运行的标价，其中编码仍在缓存中的点击没有支付这项费用。缓存命中没有保证，因此预算应以前者为准。它不报告每次点击的耗时：该数值取决于读者到 API 之间的网络路径，不反映服务本身。

## 结果 <!-- #results -->

在录得真实运行之前，以下数字来自离线假 API。对见过的请求，它的细化结果重放真实冒烟运行的记录；其余请求则返回围住正点的矩形：这些数字说明模拟器、后端和计费工作正常且各语言一致，并不反映 `sam-lite` 的分割质量。

Smoke 集（3 个物体，最多 4 次点击），离线：

<!-- metrics: expected/offline-summary.json -->
| 指标 | 数值 |
|---|---|
| `objects` | 3 |
| `objects_ok` | 3 |
| `noc85_mean` | 1.333 |
| `noc85_reached` | 1 |
| `noc90_mean` | 1.333 |
| `noc90_reached` | 1 |
| `miou_at_1` | 0.885 |
| `miou_at_2` | 0.9424 |
| `miou_at_3` | 0.9424 |
| `miou_at_4` | 0.9424 |
| `clicks_per_object` | 1.333 |
| `list_credits_per_object` | 0.213333 |
| `planning_credits_per_object` | 0.28 |
| `charged_credits_per_object` | 0 |
| `cache_hit_rate` | 0.25 |
<!-- /metrics -->

完整集合（30 个物体，至多 10 次点击），实测；维护者记录一次运行后，由 `expected/full-summary.json` 自动填入：

<!-- metrics: expected/full-summary.json -->
| 指标 | 数值 |
|---|---|
| `objects` | 30 |
| `objects_ok` | 30 |
| `noc85_mean` | 1.5 |
| `noc85_reached` | 1 |
| `noc90_mean` | 1.8 |
| `noc90_reached` | 0.967 |
| `miou_at_1` | 0.7706 |
| `miou_at_2` | 0.8975 |
| `miou_at_3` | 0.9338 |
| `miou_at_4` | 0.9429 |
| `miou_at_5` | 0.9473 |
| `miou_at_6` | 0.949 |
| `miou_at_7` | 0.9489 |
| `miou_at_8` | 0.9487 |
| `miou_at_9` | 0.9485 |
| `miou_at_10` | 0.9213 |
| `clicks_per_object` | 1.8 |
| `list_credits_per_object` | 0.024667 |
| `planning_credits_per_object` | 0.378 |
| `charged_credits_per_object` | 0.024667 |
| `cache_hit_rate` | 0.9815 |
<!-- /metrics -->

## 成本 <!-- #cost -->

使用 `sam-lite` 时，若只有第一次点击未命中编码缓存，k 次点击分割一个物体的标价为 0.2 + 0.01 k credits；若每次都未命中，最多为 0.21 k。上传免费。充值账户的免费池每天最多覆盖 1,000 次调用，这些调用计入标价但不实际扣费。

<!-- cost -->
| 运行 | 调用次数 | 标价（credits） | 实扣（credits） |
|---|---|---|---|
| smoke | 4 | 0.64 | 0 |
| full | 54 | 0.74 | 0.74 |
<!-- /cost -->

## 局限与不适用的场景 <!-- #limits -->

- **细长或透明的物体**（线缆、玻璃、头发、栅栏）需要较多点击。
- **第一次点击要支付编码费**（`sam-lite` 为 0.2 credits）。缓存不作保证，应按每次点击都未命中来规划。
- **一次只处理一个物体。** 若要找出图中某一类的全部物体，应改用文字提示（用例 01）。
- **后端是示例，不是访问控制。** 会话标识只用于区分访问者以便计数和限速；放到公开页面之前，应先置于站点自身的登录之后。
- **状态保存在内存中。** 后端重启后页面需要重新上传图像（`404 image_not_found`）；客户端在进程存续期间保留每张图像的字节和每一条账本行。

参见文档中的 [Images](https://stack.foldbun.com/docs/images/)、[Rate limits](https://stack.foldbun.com/docs/rate-limits/) 和 [Billing](https://stack.foldbun.com/docs/billing/)。

## 数据 <!-- #data -->

Smoke 集是来自 Open Images V7 验证集的 3 个物体（一只鹅、一只鸟和一个托盘）：图像以 CC BY 2.0 授权，版权归各自作者，长边缩放到不超过 1,024 像素；分割掩码以 CC BY 4.0 授权，版权归 Google LLC，已缩放到与图像一致。署名信息见 `assets/smoke/ATTRIBUTION.csv`。
