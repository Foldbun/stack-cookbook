# Changelog

All notable changes to this repository. Versions follow [Semantic Versioning](https://semver.org/); the client files carry their own version (`VERSION` in each file).

## 0.3.1 - 2026-11-17

- Site: the language filter row above the use-case matrix is aligned again; every button except "All" sat 8px below the label.

## 0.3.0 - 2026-11-17

The recipes run on the Foldbun Stack free tier by default: no key and no sign-up.

- Free tier by default. With no `FOLDBUN_API_KEY` the clients send only `X-Foldbun-Project` with the Cookbook's public project ID and inline base64 images (no uploads, handles, batches or idempotency keys). `FOLDBUN_PROJECT_ID` is optional and replaces it with the reader's own project and its limits.
- The API key is optional. When set, calls are charged at list price and the project header records the project as the source. `FOLDBUN_MAX_CREDITS` still caps what a keyed run may spend; on the free tier the recipes print the list price of what ran, labelled "free tier, not charged".
- Every smoke run works with no key. A full run needs `FOLDBUN_API_KEY` once it goes beyond 10 items (use case 05: 25 images), and uploads, image handles and batches are refused locally; the run prints one line (`FOLDBUN_API_KEY is not set, and this needs a key: <reason>.`) and exits with code 2.
- Throttling follows the server: a `429` (`rate_limited`, `free_busy`) and server-marked retryable errors are retried up to 5 times after `max(retryAfterMs, Retry-After, 1 s)` plus jitter (at most 60 s); `daily_limit_reached` and `free_tier_unavailable` are final; when a success reports `X-RateLimit-Remaining: 0` the next call for that model waits `X-RateLimit-Reset` seconds. The client-side per-pool pacing is gone.
- `GET /v1/models` works with a project ID alone, so the clients no longer refuse it without a key; each model lists its free-tier limits.
- Client files 1.1.0 (Python, TypeScript, Go). Exit code 3 means a model locked for the key (`403 model_locked`); a `403 free_tier_unavailable` is a failed call whose message points to `FOLDBUN_API_KEY`.
- The vendored API description `harness/vendor/openapi.yaml` is now 1.3.0 (free tier by project ID, `GET /v1/whoami` for a project, per-model `free` limits, `tier` and `sourceProject` in billing; the daily allowances are retired). The fake API answers as 1.3.0 describes, with no exceptions in its response validation.
- Use case 08: `--guard-max` defaults to 1.0 credit per hour; on the free tier the monitoring layer exports `stack_free_used_today` and `stack_free_daily_limit` per weight class.
- The use-case table says what runs without a key ("Smoke run" or "Everything"), here and on the site.
- The wording of the retired daily allowances ("public pool", "free pool", included calls, tiers by account balance) is removed from the READMEs, `CONTRIBUTING.md`, `clients/README.md` and the site. Cost tables keep the measured list prices, now framed as what a keyed run costs.
- The root README has a section on how the recipes authenticate (free tier, own project, API key).

## 0.2.0 - 2026-11-14

Three more use cases, each measured on its own dataset.

- `04-documents-to-rag-chunks`: scanned and photographed document pages to Markdown chunks for a search index with `paddleocr-vl` (Python): one `/v1/documents/parse` call per page, repeated headers and footers removed across pages, chunks that keep tables whole and carry page, box and heading path, `chunks.jsonl` and one Markdown file per document. An optional `--pdf` input renders pages with `pypdfium2`. Dataset: 30 synthetic pages (15 clean, 15 photographed) with block, table-cell and Markdown ground truth (CC0, public-domain text). `evaluate.py` reports text NED, table cell F1, reading-order tau, the clean/photographed gap, chunk statistics and cost per 1,000 pages; no latency.
- `08-cost-and-latency-monitoring` (Python): `monitoring.py` wraps the client file and exports Prometheus metrics from the billing, cache and timing of every answer and from each batch `item` event, with a sliding-hour spend guard (alert or refuse); a 21-call replay, `evaluate.py` (books agree, cache hits, guard trigger, middleware overhead), an alert-rules file and a Grafana dashboard.
- The vendored API description `harness/vendor/openapi.yaml` is now 1.1.0 (adds `GET /v1/whoami`, `account_disabled`, `forbidden` and `moved` error codes, upload limits, batch result cap; one phrase naming a retired host is generalised). The fake API serves `GET /v1/whoami`.
- Root test dependencies: `prometheus-client` (tests of use case 08).
- `harness scan` checks an image embedded in a recorded answer as an image (metadata) instead of matching the word list against its base64 text.
- `06-product-photo-pipeline`: marketplace photos to catalogue images with `birefnet` and four local quality rules (Python; the dataset of 40 Open Images photos with mask truth is in `datasets/`). `evaluate.py` reports the IoU against the masks, the share of photos the rules stop, by kind of photo, and cost per 1,000 photos; no latency.

## 0.1.0 - 2026-11-12

First public release.

- Use cases, each with an English and a Chinese README, measured results on a real dataset, and one self-contained implementation directory per language:
  - `00-quickstart`: a first call with `ppocr-v6` (curl, Python, TypeScript, Go).
  - `01-dataset-prelabelling`: pre-label an image dataset from text prompts with `sam3`, or from boxes with `sam-lite` (Python, Go).
  - `02-click-to-segment`: a click-to-segment component with a key-holding backend and a web front end (Python, TypeScript, Go).
  - `03-receipts-to-json`: receipts and invoices to validated JSON with `ppocr-v6` and a `paddleocr-vl` fallback (Python, TypeScript, Go).
  - `05-image-library-search`: search an image library by image, by text and for near-duplicates with `dinov3` and `siglip2` (Python, Go).
- Client files `clients/python/stack_client.py`, `clients/typescript/stackClient.ts` and `clients/go/stackclient` 1.0.0: retries, per-pool throttling, a credit cap (`FOLDBUN_MAX_CREDITS`), batch streaming and a ledger of every call. They are copied into each implementation and are not published as packages.
- Contract: `contract/usecase.schema.json`, `manifest.schema.json`, `result.schema.json` and the backend contract of use case 02.
- Harness: `check`, `live`, `refresh`, `snippets`, `i18n`, `scan`, `sync-clients` and `fakeapi`; `harness check` runs everything offline against a local fake of the API, without a key.
- Datasets: smoke sets committed under `datasets/smoke/`, full sets built by `datasets/build/` and published as a release asset, with a per-file attribution table and `datasets/LICENSES.md`.
- Site: the Astro Starlight source of the Cookbook pages (English and Chinese), built under `/cookbook/`.
- Licences: code under MIT; images and datasets under their own licences, see `ATTRIBUTION.md`.
