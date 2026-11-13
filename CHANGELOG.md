# Changelog

All notable changes to this repository. Versions follow [Semantic Versioning](https://semver.org/); the client files carry their own version (`VERSION` in each file).

## Unreleased

- `04-documents-to-rag-chunks`: scanned and photographed document pages to Markdown chunks for a search index with `paddleocr-vl` (Python): one `/v1/documents/parse` call per page, repeated headers and footers removed across pages, chunks that keep tables whole and carry page, box and heading path, `chunks.jsonl` and one Markdown file per document. An optional `--pdf` input renders pages with `pypdfium2`. Dataset: 30 synthetic pages (15 clean, 15 photographed) with block, table-cell and Markdown ground truth (CC0, public-domain text). `evaluate.py` reports text NED, table cell F1, reading-order tau, the clean/photographed gap, chunk statistics and cost per 1,000 pages; no latency.
- `08-cost-and-latency-monitoring` (Python): `monitoring.py` wraps the client file and exports Prometheus metrics from the billing, cache and timing of every answer and from each batch `item` event, with a sliding-hour spend guard (alert or refuse); a 21-call replay, `evaluate.py` (books agree, cache hits, guard trigger, middleware overhead), an alert-rules file and a Grafana dashboard.
- The vendored API description `harness/vendor/openapi.yaml` is now 1.1.0 (adds `GET /v1/whoami`, `account_disabled`, `forbidden` and `moved` error codes, upload limits, batch result cap; one phrase naming a retired host is generalised). The fake API serves `GET /v1/whoami`.
- Root test dependencies: `prometheus-client` (tests of use case 08).

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
