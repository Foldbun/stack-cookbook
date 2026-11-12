# Foldbun Stack Cookbook

English | [简体中文](README.zh-CN.md)

Runnable example projects for the [Foldbun Stack](https://stack.foldbun.com) vision model API, in Python, TypeScript and Go. Each use case solves one real engineering problem end to end — dataset pre-labelling, click-to-segment, receipts to JSON, image search and more — with measured throughput, quality and cost on a real dataset.

Browse the use cases at **https://stack.foldbun.com/cookbook/**.

## Quick start <!-- #quick-start -->

The commands need an API key, and keys are not open yet (see [API keys](#api-keys)). Everything except the calls to the API runs without one: `uv run python -m harness check` runs every implementation against a local fake of the API.

```bash
git clone https://github.com/Foldbun/stack-cookbook.git
cd stack-cookbook/use-cases/00-quickstart/python
export FOLDBUN_API_KEY="fbs_..."
uv run main.py
```

curl: `bash ../curl/run.sh`. TypeScript: `cd ../typescript && pnpm install && node main.ts`. Go: `cd ../go && go run .`.

## API keys <!-- #api-keys -->

The examples call the API with a key created in the Foldbun Stack console. API keys are coming soon. Until then, the [Playground](https://stack.foldbun.com/playground/) runs every model without an account, and every use case page shows the measured results and cost of a real run, so the examples can be read without a key. See [Authentication](https://stack.foldbun.com/docs/authentication/).

## Use cases <!-- #use-cases -->

<!-- usecases -->
| Use case | Tier | curl | Python | TypeScript | Go |
|---|---|---|---|---|---|
| [00 First call](use-cases/00-quickstart/README.md) | Public pool | ✓ | ✓ | ✓ | ✓ |
| [01 Pre-label an image dataset from text prompts](use-cases/01-dataset-prelabelling/README.md) | Billed |  | ✓ |  | ✓ |
| [02 Click-to-segment component with a key-holding backend](use-cases/02-click-to-segment/README.md) | Funded |  | ✓ | ✓ | ✓ |
| [03 Receipts and invoices to validated JSON](use-cases/03-receipts-to-json/README.md) | Billed |  | ✓ | ✓ | ✓ |
| [05 Search an image library by image, by text, and for near-duplicates](use-cases/05-image-library-search/README.md) | Public pool |  | ✓ |  | ✓ |
<!-- /usecases -->

Public pool: runs on a new account. Funded: needs a topped-up account; calls inside the free pool are not charged. Billed: charged from the balance at the [list prices](https://stack.foldbun.com/docs/billing/).

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

`uv run python -m harness check` runs everything offline, without a key: schemas, lint, the client tests, and every implementation against a local fake of the API whose answers are checked against the [published API description](https://stack.foldbun.com/docs/openapi.yaml). Before each release, a maintainer runs `harness live` with a real key; its results are the numbers on the use case pages. Nothing runs in GitHub Actions. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Data and licences <!-- #data -->

Every image and dataset has a licence that allows redistribution, with a row per file in an attribution table. See [ATTRIBUTION.md](ATTRIBUTION.md).

## Help and feedback <!-- #help -->

Open a [GitHub issue](https://github.com/Foldbun/stack-cookbook/issues) for a problem with an example or a request for a use case or language. Questions about the API itself are answered in the [documentation](https://stack.foldbun.com/docs/). To report a security problem, see [SECURITY.md](SECURITY.md).

## Licence <!-- #licence -->

The code is released under the [MIT licence](LICENSE). Images and datasets keep their own licences, listed in [ATTRIBUTION.md](ATTRIBUTION.md).
