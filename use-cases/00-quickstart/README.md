# First call

English | [简体中文](README.zh-CN.md)

Check an API key, upload an image, read its text with `ppocr-v6` and see what the call cost. The model is in the public pool, so a new account can run this.

## Problem <!-- #problem -->

A first integration has to get four things right before anything else: authentication, sending an image, reading the result and understanding the bill. This example does each once, with nothing else in the way.

## Pipeline <!-- #pipeline -->

1. `GET /v1/models`: free, checks the key and lists prices and what the account may use.
2. `POST /v1/images`: upload the file once and get a handle (`img_...`). Free.
3. `POST /v1/ocr` with the handle: the recognised lines, plus `billing` and `timing`.

## Run it <!-- #run -->

```bash
export FOLDBUN_API_KEY="fbs_..."
cd use-cases/00-quickstart/python && uv run main.py
# or: bash use-cases/00-quickstart/curl/run.sh
# or: cd use-cases/00-quickstart/typescript && pnpm install && node main.ts
# or: cd use-cases/00-quickstart/go && go run .
```

Each implementation writes `out/result.json` in the format of `contract/result.schema.json`.

## Code <!-- #code -->

The client is created once. The key comes from `FOLDBUN_API_KEY` and is never printed.

<!-- code: client -->
<details><summary>curl (curl/run.sh)</summary>

```bash
if [ -z "${FOLDBUN_API_KEY:-}" ]; then
  echo 'FOLDBUN_API_KEY is not set. See "API keys" in the README; every use case page shows the output and cost of a real run, so it can be read without a key.' >&2
  exit 2
fi
base="${FOLDBUN_API_BASE:-https://api.stack.foldbun.com}"
ua="foldbun-stack-cookbook/1.0.0 (curl; +https://github.com/Foldbun/stack-cookbook)"
```

</details>

<details><summary>Python (python/main.py)</summary>

```python
try:
    client = sc.StackClient(max_concurrency=args.concurrency)  # FOLDBUN_API_KEY, FOLDBUN_API_BASE
    models = client.models()  # free: checks the key and lists prices
except sc.MissingApiKey as err:
    print(err.message, file=sys.stderr)
    return EXIT_NO_KEY
print(f"key accepted; ppocr-v6 available: {models['ppocr-v6']['available']}")
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
let client: sc.StackClient;
try {
  client = new sc.StackClient({ maxConcurrency: Number(args.concurrency) }); // FOLDBUN_API_KEY, FOLDBUN_API_BASE
} catch (err) {
  if (err instanceof sc.MissingApiKey) {
    console.error(err.message);
    return EXIT_NO_KEY;
  }
  throw err;
}
const models = await client.models(); // free: checks the key and lists prices
console.log(`key accepted; ppocr-v6 available: ${models["ppocr-v6"].available}`);
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
client, err := stackclient.New(stackclient.Config{MaxConcurrency: *concurrency}) // FOLDBUN_API_KEY, FOLDBUN_API_BASE
if stackclient.Code(err) == "missing_api_key" {
	fmt.Fprintln(os.Stderr, stackclient.MissingKey)
	return exitNoKey
} else if err != nil {
	fmt.Fprintln(os.Stderr, err)
	return exitFailed
}
models, err := client.Models() // free: checks the key and lists prices
if err != nil {
	fmt.Fprintln(os.Stderr, err)
	return exitFailed
}
fmt.Printf("key accepted; ppocr-v6 available: %t\n", models["ppocr-v6"].Available)
```

</details>
<!-- /code -->

An image can be sent inline as base64, which is simplest for one call, or uploaded once and referenced by its handle, which suits several calls or models on the same image and is required in batches. `url` inputs are not available yet.

<!-- code: image-input -->
<details><summary>curl (curl/run.sh)</summary>

```bash
status="$(call POST /v1/images -H "Content-Type: $type" --data-binary "@$file")"
if [ "$status" -ge 300 ]; then fail "$status"; exit 1; fi
handle="$(jq -r .id "$tmp/body")"
```

</details>

<details><summary>Python (python/main.py)</summary>

```python
if item.get("imageInput") == "base64":
    image = sc.image_base64(data)  # inline: simplest for a single call
else:
    uploaded = client.upload(data, content_type)  # free; the handle can be reused for 7 days at most
    image = sc.image_handle(uploaded["id"])
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
let image;
if (item.imageInput === "base64") {
  image = sc.imageBase64(data); // inline: simplest for a single call
} else {
  const uploaded = await client.upload(data, contentType); // free; the handle can be reused for 7 days at most
  image = sc.imageHandle(uploaded.id);
}
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
var image map[string]string
if it.ImageInput == "base64" {
	image = stackclient.ImageBase64(data) // inline: simplest for a single call
} else {
	uploaded, err := client.Upload(data, contentType) // free; the handle can be reused for 7 days at most
	if err != nil {
		return nil, err
	}
	image = stackclient.ImageHandle(uploaded.ID)
}
```

</details>
<!-- /code -->

The call itself, and how to read what it cost:

<!-- code: first-call -->
<details><summary>curl (curl/run.sh)</summary>

```bash
key="$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)"
jq -n --arg h "$handle" '{model: "ppocr-v6", image: {id: $h}}' > "$tmp/request"
status="$(call POST /v1/ocr -H "Content-Type: application/json" -H "Idempotency-Key: $key" --data-binary "@$tmp/request")"
if [ "$status" = 403 ] && [ "$(jq -r .error.code "$tmp/body")" = model_locked ]; then fail "$status"; exit 3; fi
if [ "$status" -ge 300 ]; then fail "$status"; exit 1; fi
jq -r '.lines[] | "  \(.confidence * 100 | round / 100)  \(.text)"' "$tmp/body"
jq -r '"  list price \(.billing.total) credits, charged \(.cost)"' "$tmp/body"
```

</details>

<details><summary>Python (python/main.py)</summary>

```python
result = client.post("/v1/ocr", {"model": "ppocr-v6", "image": image}, tag=item["id"])
for line in result["lines"]:
    print(f"  {line['confidence']:.2f}  {line['text']}")
billing = result["billing"]
covered = f"covered by the {billing['pool']} pool ({billing['allowance']['used']}/{billing['allowance']['limit']} today)" if billing["pool"] else "billed"
print(f"  list price {billing['total']} credits, charged {result['cost']} ({covered})")
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
const result = await client.post("/v1/ocr", { model: "ppocr-v6", image }, { tag: item.id });
for (const line of result.lines) {
  console.log(`  ${line.confidence.toFixed(2)}  ${line.text}`);
}
const billing = result.billing;
const covered = billing.pool ? `covered by the ${billing.pool} pool (${billing.allowance.used}/${billing.allowance.limit} today)` : "billed";
console.log(`  list price ${billing.total} credits, charged ${result.cost} (${covered})`);
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
raw, err := client.Post("/v1/ocr", map[string]any{"model": "ppocr-v6", "image": image}, stackclient.Opts{Tag: it.ID})
if err != nil {
	return nil, err
}
var result struct {
	output
	Cost    float64 `json:"cost"`
	Billing struct {
		Total     float64 `json:"total"`
		Pool      string  `json:"pool"`
		Allowance struct{ Used, Limit int }
	} `json:"billing"`
}
if err := json.Unmarshal(raw, &result); err != nil {
	return nil, err
}
for _, l := range result.Lines {
	fmt.Printf("  %.2f  %s\n", l.Confidence, l.Text)
}
b, covered := result.Billing, "billed"
if b.Pool != "" {
	covered = fmt.Sprintf("covered by the %s pool (%d/%d today)", b.Pool, b.Allowance.Used, b.Allowance.Limit)
}
fmt.Printf("  list price %g credits, charged %g (%s)\n", b.Total, result.Cost, covered)
```

</details>
<!-- /code -->

## Cost <!-- #cost -->

One `ppocr-v6` call has a list price of 0.25 credits. Calls inside the daily allowance of the public pool are not charged: `cost` is 0, and `billing` shows the list price, the pool and today's count.

<!-- cost -->
| Run | Calls | List price (credits) | Charged (credits) |
|---|---|---|---|
| smoke | 1 | 0.25 | 0 |
<!-- /cost -->

## Limits and when not to use this <!-- #limits -->

- The handle of an uploaded image is kept for 7 days at most and can be evicted earlier; on `404 image_not_found`, upload again. The Python, TypeScript and Go clients do this by themselves.
- `ppocr-v6` reads lines; it does not analyse layout. For reading order, tables and formulas, see [document parsing](https://stack.foldbun.com/docs/use-cases/document-parsing/).
- See [Quickstart](https://stack.foldbun.com/docs/quickstart/), [Images](https://stack.foldbun.com/docs/images/) and [Billing](https://stack.foldbun.com/docs/billing/) in the documentation.

## Data <!-- #data -->

`assets/smoke/settings.webp` is a synthetic screenshot made for Foldbun Stack and released under CC0 1.0; see `assets/smoke/ATTRIBUTION.csv`.
