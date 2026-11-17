<!-- en-sha: 8218fdd2caf6 -->
# 第一次调用

[English](README.md) | 简体中文

发送一张图片、用 `ppocr-v6` 读出其中的文字，并查看这次调用的费用。smoke 运行不需要 API 密钥，也不需要注册：它在免费层上运行。

## 问题 <!-- #problem -->

第一次集成在做其他事情之前，需要先把四件事做对：认证、发送图片、读取结果、理解计费。本示例把每一件各做一次，不夹杂其他内容。

## 管线 <!-- #pipeline -->

1. `GET /v1/models`：免费，列出模型和价格。
2. 仅在使用密钥时，`POST /v1/images`：上传一次文件，得到 handle（`img_...`）。免费。免费层没有上传：图片以 base64 内联发送（解码后最大 4 MiB）。
3. 用图片或 handle 调用 `POST /v1/ocr`：返回识别出的文字行，以及 `billing` 和 `timing`。

## 运行 <!-- #run -->

```bash
cd use-cases/00-quickstart/python && uv run main.py
# 或：bash use-cases/00-quickstart/curl/run.sh
# 或：cd use-cases/00-quickstart/typescript && pnpm install && node main.ts
# 或：cd use-cases/00-quickstart/go && go run .
```

无需密钥：调用由免费层在 Cookbook 的共享项目下处理（见[示例如何认证](../../README.zh-CN.md#authentication)）。想改用密钥运行，设置 `FOLDBUN_API_KEY` 即可；此时调用按标价扣费，图片上传一次后以 handle 发送。设置 `FOLDBUN_PROJECT_ID` 可改用自己的项目及其限额。

每个实现都把 `out/result.json` 按 `contract/result.schema.json` 的格式写出。

## 代码 <!-- #code -->

客户端只创建一次。密钥可选，取自 `FOLDBUN_API_KEY`；项目 ID 取自 `FOLDBUN_PROJECT_ID`，默认是 Cookbook 的项目。两者都从不打印。

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

图片可以用 base64 内联发送，单次调用时最简单，也是免费层唯一的方式；也可以（使用密钥时）先上传一次、用 handle 引用，适合同一张图多次调用或用于多个模型，批处理也只接受 handle。`url` 输入尚未开放。

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

调用本身，以及如何读取费用：

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

## 费用 <!-- #cost -->

一次 `ppocr-v6` 调用的标价是 0.25 credits。smoke 运行在免费层上免费：运行时会打印这笔标价，并标注“免费层，不扣费”，不会从任何账户扣款。使用密钥时，每次调用都按标价扣费。

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
