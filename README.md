# Foldbun Stack Cookbook

English | [简体中文](README.zh-CN.md)

Runnable example projects for the [Foldbun Stack](https://stack.foldbun.com) vision model API, in Python, TypeScript and Go. Each use case solves one real engineering problem end to end — dataset pre-labelling, click-to-segment, receipts to JSON, image search and more — with measured throughput, quality and cost on a real dataset.

Browse the use cases at **https://stack.foldbun.com/cookbook/**.

## Quick start <!-- #quick-start -->

No key and no sign-up: the recipes run on the Foldbun Stack free tier under the Cookbook's own project (see [How the recipes authenticate](#authentication)).

```bash
git clone https://github.com/Foldbun/stack-cookbook.git
cd stack-cookbook/use-cases/00-quickstart/python
uv run main.py
```

curl: `bash ../curl/run.sh`. TypeScript: `cd ../typescript && pnpm install && node main.ts`. Go: `cd ../go && go run .`.

Every use case has a small smoke run that works the same way. The full runs are larger and need an API key.

## How the recipes authenticate <!-- #authentication -->

There are two ways to call the API, and the recipes choose between them by looking at the environment:

- **Free tier (the default).** With no `FOLDBUN_API_KEY`, a recipe sends only the header `X-Foldbun-Project: fbp_2kIj7bWkULQkLAbceHhfLW4eY6O0`, the Cookbook's own public project. All seven models are served free of charge, with inline base64 images only (up to 4 MiB decoded): no image upload, no image handles, no batch endpoints and no idempotency keys. The limits are per project, by the weight of the model, with a share per client IP. The Cookbook's project is shared by all readers, so it can be busy, and the free tier is queued behind paid traffic and has no availability commitment. A refused call is an HTTP 429 with `Retry-After`; the recipes wait for it and carry on. The free tier is not available from some regions (403). See [the free tier](https://stack.foldbun.com/docs/free-tier/) and [rate limits](https://stack.foldbun.com/docs/rate-limits/).
- **Your own project.** Create a project in the [console](https://stack.foldbun.com/console/) (every account has a default one) and set `FOLDBUN_PROJECT_ID` to its ID. The recipes then count against your project's own limits, which are 30, 10 and 2 requests per minute and 2,000, 500 and 50 per day for light, medium and heavy models, instead of the shared ones.
- **An API key (paid).** Set `FOLDBUN_API_KEY` to a key from the console (`fbs_...`). Every call is then charged at the list price, and the project header, if any, records the project as the call's source. A key also enables uploads, handles and batches. Nothing is free with a key.

The smoke run of every use case works with no key. The full runs need `FOLDBUN_API_KEY`: they are large, and some use uploads, handles or batches. Without it, a full run says so and exits. `FOLDBUN_MAX_CREDITS` caps what a keyed run may spend: the client refuses a call that could pass it. On the free tier a recipe prints the list price of what it ran, labelled "free tier, not charged". See [Authentication](https://stack.foldbun.com/docs/authentication/).

## Use cases <!-- #use-cases -->

<!-- usecases -->
| Use case | Tier | curl | Python | TypeScript | Go |
|---|---|---|---|---|---|
| [00 First call](use-cases/00-quickstart/README.md) | Public pool | ✓ | ✓ | ✓ | ✓ |
| [01 Pre-label an image dataset from text prompts](use-cases/01-dataset-prelabelling/README.md) | Billed |  | ✓ |  | ✓ |
| [02 Click-to-segment component with a key-holding backend](use-cases/02-click-to-segment/README.md) | Funded |  | ✓ | ✓ | ✓ |
| [03 Receipts and invoices to validated JSON](use-cases/03-receipts-to-json/README.md) | Billed |  | ✓ | ✓ | ✓ |
| [04 Scanned documents to Markdown chunks for a search index](use-cases/04-documents-to-rag-chunks/README.md) | Billed |  | ✓ |  |  |
| [05 Search an image library by image, by text, and for near-duplicates](use-cases/05-image-library-search/README.md) | Public pool |  | ✓ |  | ✓ |
| [06 Marketplace photos to catalogue images](use-cases/06-product-photo-pipeline/README.md) | Billed |  | ✓ |  |  |
| [08 Monitor cost and latency of a production integration](use-cases/08-cost-and-latency-monitoring/README.md) | Funded |  | ✓ |  |  |
<!-- /usecases -->

Every smoke run is free on the free tier and needs no key. The full runs need an API key and are charged at the [list prices](https://stack.foldbun.com/docs/billing/).

## Languages and toolchains <!-- #languages -->

| Language | Tested with | Runs as |
|---|---|---|
| Python | Python 3.12 and 3.14, [uv](https://docs.astral.sh/uv/) | `uv run main.py` |
| TypeScript | Node 24, pnpm | `node main.ts` (type stripping; `tsc --noEmit` only checks) |
| Go | Go 1.22 or later | `go run .` |
| curl | bash, curl, jq | `bash run.sh` |

Each implementation directory is self-contained: it has its own dependency manifest and lock file, and its own copy of the small client file from `clients/<language>/`. Copy the directory into a project and it runs. The client files are not an SDK and are not published to any package registry.

Every implementation is a command-line program with the same arguments, `--manifest <path> --out <dir> [--concurrency N] [--limit N] [--resume]`, and writes `<dir>/result.json` as described in `contract/`.

## How the examples are tested <!-- #testing -->

`uv run python -m harness check` runs everything offline, without a key or a project ID: schemas, lint, the client tests, and every implementation against a local fake of the API whose answers are checked against the [published API description](https://stack.foldbun.com/docs/openapi.yaml). Before each release, a maintainer runs `harness live` with a real key; its results are the numbers on the use case pages (list prices, so what a keyed run costs). Nothing runs in GitHub Actions. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Data and licences <!-- #data -->

Every image and dataset has a licence that allows redistribution, with a row per file in an attribution table. See [ATTRIBUTION.md](ATTRIBUTION.md).

## Help and feedback <!-- #help -->

Open a [GitHub issue](https://github.com/Foldbun/stack-cookbook/issues) for a problem with an example or a request for a use case or language. Questions about the API itself are answered in the [documentation](https://stack.foldbun.com/docs/). To report a security problem, see [SECURITY.md](SECURITY.md).

## Licence <!-- #licence -->

The code is released under the [MIT licence](LICENSE). Images and datasets keep their own licences, listed in [ATTRIBUTION.md](ATTRIBUTION.md).
