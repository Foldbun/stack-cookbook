# Click-to-segment component with a key-holding backend

English | [简体中文](README.zh-CN.md)

Segment one object with a few clicks in the browser. A small backend holds the API key, uploads each image to Foldbun Stack once, and sends every click to `sam-lite` together with all earlier clicks; the browser gets a polygon back and never sees the key. A click simulator drives the backend the way a person would and measures how many clicks an object needs, how well the polygon fits and what it costs.

## Problem <!-- #problem -->

An annotation tool, a photo editor or a product catalogue wants "click on the thing, get its outline". The model call is easy; the parts around it decide whether it can go on a public page:

- **The key must not reach the browser.** Anything in the page can be read by its visitors, so a backend makes the calls and the page talks only to that backend.
- **Every click is a paid call.** `sam-lite` lists 0.01 credits per refinement plus 0.2 for encoding the image. Stack caches the encoding, so the clicks after the first one are usually 0.01, but the cache is not guaranteed and a budget has to plan for a miss on every click.
- **The model keeps no state.** `POST /v1/refine` takes all points of the object every time. The backend does the same: it stores images, not clicks, so a reload or a second tab cannot get out of step.
- **Visitors share one account.** The account's free pool serves 60 calls per minute. The backend limits each session and the client file paces all calls at the pool's rate, so one busy visitor cannot use up the minute for everyone.
- **Click latency is felt.** An interactive call that has not finished within 2 s fails with a retryable `504`, and the backend reports the time of each Stack call so that the page can show it. Latency seen from a client depends on its network path to the API, so the measurements of this use case do not include it.

## How it works <!-- #design -->

The backend implements one HTTP contract in every language, `contract/02-backend.yaml`:

| Request | Answer |
|---|---|
| `POST /api/images` (raw image bytes) | `{imageId, width, height}`; the image is uploaded to Stack once per content and its handle cached |
| `POST /api/refine {imageId, points, box?}` | `{polygon, bbox, area, score, model, costCredits, listCredits, cache, latencyMs}` |
| `GET /api/usage` | clicks, credits, cache hits and the ledger rows of the session |
| `GET /api/health` | readiness, language and client version |

Every request except `/api/health` carries `X-Session-Id`, an identifier the page chooses (one per page load). Points and polygons use the coordinates of the Stack API: pixels of the image with its EXIF orientation applied, x to the right, y down. Errors have one shape, `{"error": {"code", "message", "retryable", "retryAfterMs"}}`; the contract lists how Stack errors map to the backend's codes.

`POST /v1/refine` returns a simplified polygon with holes by default, so the backend passes Stack's polygon through and decodes no mask.

## Component <!-- #component -->

`<stack-segmenter>` is a native Web Component: no framework and no runtime dependency, about 400 lines of TypeScript in [`web/stack-segmenter.ts`](web/stack-segmenter.ts), with its own minimal styles in a shadow root. The parts that need no browser (coordinate mapping, the point list, the request queue, polygon to path) are in [`web/logic.ts`](web/logic.ts), and `node --test` covers them in `web/logic.test.ts`. The component file is organised in the regions `upload`, `click`, `request`, `draw` and `export`.

- **Clicks.** Left click adds a positive point (on the object); right click or Alt+click adds a negative point (leave this out). Undo (button or Ctrl/Cmd+Z) removes the last point, Clear removes all. Every click sends **all** points, because `POST /api/refine` keeps no click state.
- **One request at a time.** A click made while a request is in flight waits; clicks that pile up are sent together in one follow-up request, and the answer to a request that is already out of date is dropped.
- **Scaling.** Click positions are converted to pixels of the image itself (`naturalWidth` x `naturalHeight`, EXIF orientation applied, the coordinates of the API) and the polygon, with its holes, is drawn back at the displayed size, again after every resize.
- **Numbers.** Score, number of points, credits charged for the last click, cache hit or miss with the latency, and the session's calls and credits (from `GET /api/usage`).
- **Errors.** `429` and `503` that the backend marks retryable are repeated after `retryAfterMs` (at most three times); `404 image_not_found` uploads the image again once. Everything else is shown as one plain sentence chosen by the error code; the backend's own text is never displayed.
- **Keyboard.** The buttons are real buttons with labels. The image area takes focus: arrow keys move a cursor (Shift for bigger steps), Enter or Space adds a positive point, Shift+Enter or Alt+Enter a negative one.

### Build and open

```bash
cd use-cases/02-click-to-segment/web
pnpm install
pnpm build                               # tsc: browser-ready ES modules and index.html in dist/
cd ../typescript
node main.ts --port 8080                 # with FOLDBUN_API_KEY set; serves ../web/dist
```

Open `http://127.0.0.1:8080/`, choose or drop an image and click. The backend serves the files of `--static <dir>` (default `../web/dist`): `GET /` is `index.html` and `GET /<name>` a file directly in that directory, nothing else (`contract/02-backend.yaml`, "Static files").

### Embed it in a page

```html
<script type="module" src="/stack-segmenter.js"></script>

<stack-segmenter id="seg" model="sam-lite" src="/photo.jpg"></stack-segmenter>

<script>
  document.getElementById("seg").addEventListener("segment", (e) => {
    console.log(e.detail.polygon); // [{outer: [x0, y0, x1, y1, ...], holes: [[...]]}], pixels of the original image
  });
</script>
```

Copy `stack-segmenter.js` and `logic.js` from `web/dist/` to the site, next to each other.

| Attribute | Meaning |
|---|---|
| `backend` | Base URL of the backend. Default: the page's own origin. |
| `model` | Model to ask for (`sam-lite` or `sam3`), only if the backend's `SEGMENTER_MODELS` allows it. Default: the backend's first model. |
| `src` | URL of an image to load. Without it, the person chooses a file or drops one on the component. It must be readable by the page. |

Properties: `backend`, `model` and `src` mirror the attributes; `sessionId` is the identifier of this page load (`crypto.randomUUID()`); `load(fileOrUrl)` loads an image; `undo()` and `clear()`; `toJSON()` is the current result or `null`.

The **`segment` event** fires after every answer. `detail` is `{imageId, width, height, model, score, points, polygon}`; `polygon` is the backend's list of parts, each with an `outer` ring and optional `holes`, in pixels of the original image. The Export JSON button saves the same object as `segment.json`.

Style it with CSS custom properties on the element: `--seg-accent`, `--seg-positive`, `--seg-negative`, `--seg-fill`, `--seg-text`, `--seg-muted`, `--seg-surface`, `--seg-border`, `--seg-hover` and `--seg-radius`. `web/index.html` is a small demo page that sets them.

### Why the key never reaches the browser

The component calls only the three routes of this backend, `POST /api/images`, `POST /api/refine` and `GET /api/usage`. The backend holds the Foldbun Stack API key, uploads each image once, calls Stack and returns the result without account details. The page therefore cannot leak the key, and the backend is the place for limits (per-session rate limits, `SEGMENTER_MODELS`, `FOLDBUN_MAX_CREDITS`). A session is an accounting unit, not authentication: put the backend behind the site's own login when the page is public.

### Same origin

The backend sends no CORS headers, so the page and the backend must share one origin: either let the backend serve the page (as above), or put both behind one reverse proxy that routes `/api/` to the backend. A page on another origin cannot call it with `backend="https://..."`.

## Run it <!-- #run -->

```bash
export FOLDBUN_API_KEY="fbs_..."
export FOLDBUN_MAX_CREDITS=2           # optional: the client refuses calls that could pass it
cd use-cases/02-click-to-segment/typescript
pnpm install
node main.ts --port 8080               # listening on http://127.0.0.1:8080
# or: cd ../python && uv run main.py --port 8080   # the same backend in Python
# or: cd ../go && go run . --port 8080             # the same backend in Go
```

In a second terminal:

```bash
S=demo-1
curl -s -H "X-Session-Id: $S" -H "Content-Type: image/jpeg" --data-binary @../assets/smoke/images/77bfbbef28ac344c.jpg \
  http://127.0.0.1:8080/api/images
curl -s -H "X-Session-Id: $S" -H "Content-Type: application/json" \
  -d '{"imageId": "im_...", "points": [{"x": 521, "y": 424}]}' http://127.0.0.1:8080/api/refine
curl -s -H "X-Session-Id: $S" http://127.0.0.1:8080/api/usage
```

Options, the same in the three languages: `--port` (default 8080), `--host` (default 127.0.0.1) and `--static DIR` (default `../web/dist`), the directory of the page: its `index.html` is answered at `/` and the files directly inside it at `/<name>`. `SEGMENTER_MODELS` is the comma-separated list of models a request may name (default `sam-lite`; add `sam3` to compare). The backend sends no CORS headers: serve the page and the backend from the same origin.

To measure a backend, the harness starts it on a free port and runs the click simulator: `uv run python -m harness live --uc 02` with a key, or `uv run python -m harness check --uc 02` against the offline fake API.

The full set (30 objects, each with a ground-truth mask, 5 MB) is a release asset of the repository (see `datasets/README.md`). Its manifest is committed as `assets/full.json` with a limit of 10 clicks per object; the images, masks and ground truth are in the archive. Extract it into `assets/` and run the reference backend (TypeScript) through the simulator from the repository root. At most 63 credits at list price (30 objects x 10 clicks x 0.21 if no click ever hit the cache), about 9 when the cache works. The run takes a few minutes because the simulator clicks at most once per second against the real API.

```bash
tar xzf 02-click-to-segment-full.tar.gz -C use-cases/02-click-to-segment/assets
export FOLDBUN_API_KEY="fbs_..."
uv run python -m harness live --size full --uc 02 --max-credits 63 --dry-run   # the plan, nothing is sent
uv run python -m harness refresh --size full --uc 02 --max-credits 63          # runs, scores, writes expected/full.json and expected/full-summary.json, then `uv run python -m harness snippets`
```

`harness live` runs the same and only checks the result against the floors in `usecase.toml` `[quality]`. Only `sam-lite` is measured; a `sam3` comparison pass is not part of the harness.

## Code <!-- #code -->

### Upload once <!-- #code-upload -->

The `imageId` is derived from the bytes, so the same file has the same id in every session and every language, and a second upload costs nothing.

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

### One click <!-- #code-refine -->

The page sends all points of the object; the backend checks them against the image size and forwards them with an explicit `positive` to `POST /v1/refine`.

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

### Sessions and limits <!-- #code-limits -->

Each session has two token buckets: 60 refinements per minute with a burst of 10, and 10 uploads per minute with a burst of 5. Over the limit the answer is `429 rate_limited` with `Retry-After`.

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

### Errors <!-- #code-errors -->

Stack's messages can mention the account, so they are logged and not forwarded; the page sees the backend's code and, for upstream errors, Stack's code.

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

### Usage <!-- #code-usage -->

The session's rows of the client's ledger: the page can show what the clicks cost, and the simulator copies the rows into `result.json`.

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

### Server <!-- #code-server -->

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

## Measurement <!-- #measurement -->

`harness/clickbot.py` drives any backend through the contract, so the languages can be compared one to one. It follows the usual protocol of interactive segmentation: the first click is the point of the ground-truth mask farthest from its boundary; each later click is the point farthest from the boundary of the largest wrong region, positive where the object was missed and negative where the polygon covers too much. It stops at IoU 0.90 or after the click limit (smoke 4, full 10), and against the real API it clicks at most once per second.

It reports NoC@85 and NoC@90 (clicks until the IoU reaches 0.85 or 0.90; an object that never gets there counts as the limit), the mean IoU after k clicks, credits per object and the cache hit rate. `planning_credits_per_object` counts the image encoding fee on every click; `list_credits_per_object` is what the recorded run was priced at, where clicks that found the encoding still cached did not pay it. A cache hit is not promised, so plan with the first number. It does not report time per click: that depends on the network path between the reader and the API, not on the service.

## Results <!-- #results -->

Until a live run is recorded, the numbers below come from the offline fake API. Its refinement replays the results recorded from the real smoke run for the requests it has seen, and is a rectangle around the positive clicks otherwise: it shows that the simulator, the backend and the accounting work and agree across languages, not how well `sam-lite` segments.

Smoke set (3 objects, at most 4 clicks), offline:

<!-- metrics: expected/offline-summary.json -->
| Metric | Value |
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

Full set (30 objects, at most 10 clicks), live run; filled in from `expected/full-summary.json` once a maintainer has recorded one:

<!-- metrics: expected/full-summary.json -->
| Metric | Value |
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

## Cost <!-- #cost -->

With `sam-lite`, one object segmented in k clicks costs 0.2 + 0.01 k credits at list price when only the first click misses the encoding cache, and at most 0.21 k when every click misses. Uploads are free. On a funded account the free pool covers up to 1,000 calls a day, which are listed but not charged.

<!-- cost -->
| Run | Calls | List price (credits) | Charged (credits) |
|---|---|---|---|
| smoke | 4 | 0.64 | 0 |
| full | 54 | 0.74 | 0.74 |
<!-- /cost -->

## Limits and when not to use this <!-- #limits -->

- **Thin or transparent objects** (cables, glass, hair, fences) need many clicks.
- **The first click pays for the encoding** (0.2 credits with `sam-lite`). The cache is not guaranteed, so plan each click as a miss.
- **One object at a time.** To find every object of a kind in an image, use text prompts instead (use case 01).
- **The backend is a sample, not an access control.** A session identifier only separates visitors for counting and limits; put the backend behind the site's own login before it goes on a public page.
- **State lives in memory.** After a restart the page has to upload its image again (`404 image_not_found`), and the client keeps every image's bytes and every ledger row for the life of the process.

See [Images](https://stack.foldbun.com/docs/images/), [Rate limits](https://stack.foldbun.com/docs/rate-limits/) and [Billing](https://stack.foldbun.com/docs/billing/) in the documentation.

## Data <!-- #data -->

The smoke set is 3 objects (a goose, a bird and a platter) from the validation split of Open Images V7: images CC BY 2.0 by their authors, resized to at most 1,024 px; segmentation masks CC BY 4.0 by Google LLC, resized to the image. Attribution: `assets/smoke/ATTRIBUTION.csv`.
