# First call

English | [简体中文](README.zh-CN.md)

Send an image, read its text with `ppocr-v6` and see what the call cost. The smoke run needs no API key and no sign-up: it runs on the free tier.

## Problem <!-- #problem -->

A first integration has to get four things right before anything else: authentication, sending an image, reading the result and understanding the bill. This example does each once, with nothing else in the way.

## Pipeline <!-- #pipeline -->

1. `GET /v1/models`: free, lists the models and their prices.
2. With a key only, `POST /v1/images`: upload the file once and get a handle (`img_...`). Free. On the free tier there is no upload: the image goes inline as base64 (up to 4 MiB decoded).
3. `POST /v1/ocr` with the image or the handle: the recognised lines, plus `billing` and `timing`.

## Run it <!-- #run -->

```bash
cd use-cases/00-quickstart/python && uv run main.py
# or: bash use-cases/00-quickstart/curl/run.sh
# or: cd use-cases/00-quickstart/typescript && pnpm install && node main.ts
# or: cd use-cases/00-quickstart/go && go run .
```

No key is needed: the call is served by the free tier under the Cookbook's shared project (see [how the recipes authenticate](../../README.md#authentication)). To run with a key instead, set `FOLDBUN_API_KEY`; the call is then charged at the list price and the image is uploaded once and sent by handle. Set `FOLDBUN_PROJECT_ID` to use your own project and its limits.

Each implementation writes `out/result.json` in the format of `contract/result.schema.json`.

## Code <!-- #code -->

The client is created once. The key is optional and comes from `FOLDBUN_API_KEY`; the project ID comes from `FOLDBUN_PROJECT_ID` and defaults to the Cookbook's. Neither is ever printed.

<!-- code: client -->
<details><summary>curl (curl/run.sh)</summary>

```bash
# The Cookbook's own project: public, shared by every reader, and free-tier only. To go beyond trying the recipes,
# create your own project in the console and set FOLDBUN_PROJECT_ID (or use an API key).
project="${FOLDBUN_PROJECT_ID:-fbp_2kIj7bWkULQkLAbceHhfLW4eY6O0}"
base="${FOLDBUN_API_BASE:-https://api.stack.foldbun.com}"
ua="foldbun-stack-cookbook/1.1.0 (curl; +https://github.com/Foldbun/stack-cookbook)"
# No FOLDBUN_API_KEY: the free tier, which takes the project header only. With a key: paid, and the project is the source project.
auth=(-H "X-Foldbun-Project: $project")
free=true
if [ -n "${FOLDBUN_API_KEY:-}" ]; then
  auth+=(-H "Authorization: Bearer $FOLDBUN_API_KEY")
  free=false
fi
```

</details>

<details><summary>Python (python/main.py)</summary>

```python
# No FOLDBUN_API_KEY: the free tier, with the Cookbook's shared project (FOLDBUN_PROJECT_ID to use your own).
client = sc.StackClient(max_concurrency=args.concurrency)  # FOLDBUN_API_KEY, FOLDBUN_PROJECT_ID, FOLDBUN_API_BASE
try:
    who = client.whoami()  # shows who is calling: a project (free tier) or an account (API key)
except sc.StackError as err:
    print(f"{err.status} {err.code}: {err.message}", file=sys.stderr)
    return EXIT_NO_KEY if err.code == "missing_api_key" else EXIT_FAILED
print(f"free tier, project {who['project']['id']}" if client.free else f"key accepted; balance {who['balance']} credits")
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
// No FOLDBUN_API_KEY: the free tier, with the Cookbook's shared project (FOLDBUN_PROJECT_ID to use your own).
const client = new sc.StackClient({ maxConcurrency: Number(args.concurrency) }); // FOLDBUN_API_KEY, FOLDBUN_PROJECT_ID, FOLDBUN_API_BASE
let who;
try {
  who = await client.whoami(); // shows who is calling: a project (free tier) or an account (API key)
} catch (err) {
  if (!(err instanceof sc.StackError)) throw err;
  console.error(err.describe());
  return err.code === "missing_api_key" ? EXIT_NO_KEY : EXIT_FAILED;
}
console.log(client.free ? `free tier, project ${who.project.id}` : `key accepted; balance ${who.balance} credits`);
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
// No FOLDBUN_API_KEY: the free tier, with the Cookbook's shared project (FOLDBUN_PROJECT_ID to use your own).
client, err := stackclient.New(stackclient.Config{MaxConcurrency: *concurrency}) // FOLDBUN_API_KEY, FOLDBUN_PROJECT_ID, FOLDBUN_API_BASE
if err != nil {
	fmt.Fprintln(os.Stderr, err)
	return exitFailed
}
whoami, err := client.WhoAmI() // shows who is calling: a project (free tier) or an account (API key)
if err != nil {
	fmt.Fprintln(os.Stderr, err)
	if stackclient.Code(err) == "missing_api_key" {
		return exitNoKey
	}
	return exitFailed
}
var who struct {
	Balance json.Number `json:"balance"`
	Project struct {
		ID string `json:"id"`
	} `json:"project"`
}
if err := json.Unmarshal(whoami, &who); err != nil {
	fmt.Fprintln(os.Stderr, "whoami:", err)
	return exitFailed
}
if client.Free() {
	fmt.Printf("free tier, project %s\n", who.Project.ID)
} else {
	fmt.Printf("key accepted; balance %s credits\n", who.Balance)
}
```

</details>
<!-- /code -->

An image can be sent inline as base64, which is simplest for one call and the only way on the free tier, or uploaded once (with a key) and referenced by its handle, which suits several calls or models on the same image and is required in batches. `url` inputs are not available yet.

<!-- code: image-input -->
<details><summary>curl (curl/run.sh)</summary>

```bash
if [ "$free" = true ]; then
  # inline: the only form the free tier takes
  base64 < "$file" | tr -d '\n' > "$tmp/b64"
  jq -n --rawfile b "$tmp/b64" '{model: "ppocr-v6", image: {base64: $b}}' > "$tmp/request"
  idem=()
else
  # needs a key; the handle can be reused for 7 days at most
  status="$(call POST /v1/images -H "Content-Type: $type" --data-binary "@$file")"
  if [ "$status" -ge 300 ]; then fail "$status"; exit 1; fi
  jq -n --arg h "$(jq -r .id "$tmp/body")" '{model: "ppocr-v6", image: {id: $h}}' > "$tmp/request"
  idem=(-H "Idempotency-Key: $(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)")
fi
```

</details>

<details><summary>Python (python/main.py)</summary>

```python
if client.free or item.get("imageInput") == "base64":
    image = sc.image_base64(data)  # inline: simplest for a single call, and the only form the free tier takes
else:
    uploaded = client.upload(data, content_type)  # needs a key; the handle can be reused for 7 days at most
    image = sc.image_handle(uploaded["id"])
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
let image;
if (client.free || item.imageInput === "base64") {
  image = sc.imageBase64(data); // inline: simplest for a single call, and the only form the free tier takes
} else {
  const uploaded = await client.upload(data, contentType); // needs a key; the handle can be reused for 7 days at most
  image = sc.imageHandle(uploaded.id);
}
```

</details>

<details><summary>Go (go/main.go)</summary>

```go
var image map[string]string
if client.Free() || it.ImageInput == "base64" {
	image = stackclient.ImageBase64(data) // inline: simplest for a single call, and the only form the free tier takes
} else {
	uploaded, err := client.Upload(data, contentType) // needs a key; the handle can be reused for 7 days at most
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
status="$(call POST /v1/ocr -H "Content-Type: application/json" "${idem[@]}" --data-binary "@$tmp/request")"
if [ "$status" = 403 ] && [ "$(jq -r .error.code "$tmp/body")" = model_locked ]; then fail "$status"; exit 3; fi
if [ "$status" -ge 300 ]; then fail "$status"; exit 1; fi
jq -r '.lines[] | "  \(.confidence * 100 | round / 100)  \(.text)"' "$tmp/body"
if [ "$free" = true ]; then
  jq -r '"  list price \(.billing.total) credits (free tier, not charged)"' "$tmp/body"
else
  jq -r '"  list price \(.billing.total) credits, charged \(.cost)"' "$tmp/body"
fi
```

</details>

<details><summary>Python (python/main.py)</summary>

```python
result = client.post("/v1/ocr", {"model": "ppocr-v6", "image": image}, tag=item["id"])
for line in result["lines"]:
    print(f"  {line['confidence']:.2f}  {line['text']}")
price = result["billing"]["total"]  # list price in credits
print(f"  list price {price} credits (free tier, not charged)" if client.free else f"  list price {price} credits, charged {result['cost']}")
```

</details>

<details><summary>TypeScript (typescript/main.ts)</summary>

```ts
const result = await client.post("/v1/ocr", { model: "ppocr-v6", image }, { tag: item.id });
for (const line of result.lines) {
  console.log(`  ${line.confidence.toFixed(2)}  ${line.text}`);
}
const price = result.billing.total; // list price in credits
console.log(client.free ? `  list price ${price} credits (free tier, not charged)` : `  list price ${price} credits, charged ${result.cost}`);
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
		Total float64 `json:"total"` // list price in credits
	} `json:"billing"`
}
if err := json.Unmarshal(raw, &result); err != nil {
	return nil, err
}
for _, l := range result.Lines {
	fmt.Printf("  %.2f  %s\n", l.Confidence, l.Text)
}
if client.Free() {
	fmt.Printf("  list price %g credits (free tier, not charged)\n", result.Billing.Total)
} else {
	fmt.Printf("  list price %g credits, charged %g\n", result.Billing.Total, result.Cost)
}
```

</details>
<!-- /code -->

## Cost <!-- #cost -->

One `ppocr-v6` call has a list price of 0.25 credits. A smoke run is free on the free tier: the run prints that value labelled "free tier, not charged", and nothing is debited. With a key, every call is charged at the list price.

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

`assets/smoke/settings.webp` is a synthetic screenshot made for Foldbun Stack and released under CC0 1.0; see `assets/smoke/ATTRIBUTION.csv`. Rights holders can request removal of an image or dataset through the [contact page](https://foldbun.com/contact/).
