# Changelog

All notable changes to this repository. Versions follow [Semantic Versioning](https://semver.org/); the client files carry their own version (`VERSION` in each file).

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
