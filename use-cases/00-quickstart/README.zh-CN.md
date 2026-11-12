<!-- en-sha: 6f24a8675615 -->
# 第一次调用

[English](README.md) | 简体中文

检查 API 密钥、上传一张图片、用 `ppocr-v6` 读出其中的文字，并查看这次调用的费用。该模型属于公共池，新账户即可运行。

## 问题 <!-- #problem -->

第一次集成在做其他事情之前，需要先把四件事做对：认证、发送图片、读取结果、理解计费。本示例把每一件各做一次，不夹杂其他内容。

## 管线 <!-- #pipeline -->

1. `GET /v1/models`：免费，检查密钥，列出价格和账户可用的模型。
2. `POST /v1/images`：上传一次文件，得到 handle（`img_...`）。免费。
3. 用 handle 调用 `POST /v1/ocr`：返回识别出的文字行，以及 `billing` 和 `timing`。

## 运行 <!-- #run -->

```bash
export FOLDBUN_API_KEY="fbs_..."
cd use-cases/00-quickstart/python && uv run main.py
# 或：bash use-cases/00-quickstart/curl/run.sh
# 或：cd use-cases/00-quickstart/typescript && pnpm install && node main.ts
# 或：cd use-cases/00-quickstart/go && go run .
```

每个实现都把 `out/result.json` 按 `contract/result.schema.json` 的格式写出。

## 代码 <!-- #code -->

客户端只创建一次。密钥取自 `FOLDBUN_API_KEY`，从不打印。

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

图片可以用 base64 内联发送，单次调用时最简单；也可以先上传一次、用 handle 引用，适合同一张图多次调用或用于多个模型，批处理也只接受 handle。`url` 输入尚未开放。

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

调用本身，以及如何读取费用：

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

## 费用 <!-- #cost -->

一次 `ppocr-v6` 调用的标价是 0.25 credits。公共池每日额度内的调用不扣费：`cost` 为 0，`billing` 显示标价、所用的池和当日计数。

<!-- cost -->
| 运行 | 调用次数 | 标价（credits） | 实扣（credits） |
|---|---|---|---|
| smoke | 1 | 0.25 | 0 |
<!-- /cost -->

## 局限与不适用的场景 <!-- #limits -->

- 上传图片的 handle 最多保留 7 天，也可能更早被逐出；遇到 `404 image_not_found` 时重新上传即可，Python、TypeScript 和 Go 客户端都会自动处理。
- `ppocr-v6` 按行识别文字，不做版面分析。需要阅读顺序、表格和公式时，参见[文档解析](https://stack.foldbun.com/docs/use-cases/document-parsing/)。
- 文档中的相关页面：[快速开始](https://stack.foldbun.com/docs/quickstart/)、[图片](https://stack.foldbun.com/docs/images/)、[计费](https://stack.foldbun.com/docs/billing/)。

## 数据 <!-- #data -->

`assets/smoke/settings.webp` 是为 Foldbun Stack 制作的合成截图，以 CC0 1.0 发布；见 `assets/smoke/ATTRIBUTION.csv`。
