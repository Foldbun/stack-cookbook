# Contributing

Thank you for helping. Responses are best effort. There is no email channel: use issues and pull requests.

- Open an issue before starting a new use case or a new language, so the scope can be agreed first.
- Every pull request must pass `uv run python -m harness check`. It runs offline and needs no API key.
- Maintainers run the live suite with their own key and refresh `expected/` after merging. Do not commit results of your own live runs, and never commit a key, a `.env` file or recorded account data.

## Layout and contract

```
contract/            JSON Schemas: usecase.toml, manifest.json, result.json
clients/<language>/  the client file of each language (the original; implementations hold byte-identical copies)
harness/             python -m harness: check, live, refresh, snippets, i18n, scan, sync-clients, fakeapi
use-cases/NN-name/   usecase.toml, README.md, README.zh-CN.md, assets/, expected/, fixtures/, one directory per language
```

Every implementation is a command-line program with the same arguments:

```
<run> --manifest <path> --out <dir> [--concurrency N] [--limit N] [--resume]
```

- It reads `manifest.json` (`contract/manifest.schema.json`): `items[]` with `id`, usually `image` (relative to the manifest), and use-case parameters. Ground truth is never in the manifest.
- It writes `<dir>/result.json` (`contract/result.schema.json`): per item `id`, `status` (`ok`, `error`, `expired`), `calls[]` (the client's ledger rows for that item), `output` (validated by `use-cases/<id>/output.schema.json` when present) and, on error, `error`; plus `ledger` totals. Use-case artefacts (COCO JSON, Markdown, images) go next to it with the same names in every language.
- Exit codes: `0` the run completed and `result.json` was written (items may have failed; that is data); `1` the run failed; `2` `FOLDBUN_API_KEY` is not set (print the standard message from the client file); `3` the account tier cannot use a model (`403 model_locked`; the harness records a skip); `4` the credit cap (`FOLDBUN_MAX_CREDITS`) refused a call.
- Never print or log the key, and do not read `.env` files.

A use case with `kind = "server"` in `usecase.toml` (02) has HTTP backends instead: the harness starts `<run> --port N` with the same environment variables, waits for `GET /api/health`, drives the backend with `harness/clickbot.py`, writes `result.json` itself (the ledger rows come from the backend's `GET /api/usage`) and stops the process group. The backend contract is `contract/02-backend.yaml`.

The client file of each language implements the same behaviour; `clients/python/stack_client.py` is the reference:

| Behaviour | Rule |
|---|---|
| Configuration | key from `FOLDBUN_API_KEY`, base URL from `FOLDBUN_API_BASE` (default `https://api.stack.foldbun.com`), cap from `FOLDBUN_MAX_CREDITS` |
| User-Agent | `foldbun-stack-cookbook/<client version> (<language>; +https://github.com/Foldbun/stack-cookbook)` |
| Timeouts (connect / read) | models 10 / 10 s; upload 10 / 60 s; inference 10 / 90 s; batch 10 s / 45 s idle, 600 s in total |
| Retries | only errors with `retryable: true`; wait `max(retryAfterMs, Retry-After, 1 s)`, at most 60 s per wait, at most 5 retries; network errors only when the request has an idempotency key |
| Idempotency | every inference call and batch sends `Idempotency-Key` (a new UUID unless given) and reuses it on every retry |
| Evicted images | on `404 image_not_found` for a handle this client uploaded: upload again, then repeat the call once with a new idempotency key (the 404 is stored under the old key and was not charged) |
| Pacing | a token bucket per pool at the `perMinute` of `GET /v1/models`; at most 4 requests in flight by default |
| Credit cap | before sending, the worst-case list price (encoding fee always counted) plus what was already spent must stay within `FOLDBUN_MAX_CREDITS` |
| Ledger | one row per successful call or `ok` batch item: `endpoint`, `model`, `list` (`billing.total`), `charged` (`cost`, 0 when replayed), `cache`, `queueMs`, `processingMs`, `replayed`; totals leave out replayed rows |
| Errors | typed: `status`, `code`, `message`, `retryable`, `retryAfterMs`, `requestId` (from `X-Request-Id`); branch on `code` only |

## The fake API

`uv run python -m harness fakeapi --port 8787` serves a local fake of the API; point `FOLDBUN_API_BASE` at it and use the key `fbs_fake` (a funded account) or `fbs_fake_public` (a new account, public pool only). It checks every request and response against the vendored API description (`harness/vendor/openapi.yaml`, the file published at https://stack.foldbun.com/docs/openapi.yaml) and reports violations at `GET /_fake/log`, together with a log of the requests it received (`User-Agent`, `Idempotency-Key`, status, replay).

- Results come from `use-cases/*/fixtures/*.json` when an entry matches the endpoint, model, sha256 of the image file and (optionally) the request body; otherwise they are synthesised and deterministic.
- `POST /_fake/reset` with a scenario injects behaviour: errors with `retryAfterMs`, delays, image eviction, per-item batch errors, expiry and dropped event streams. The shared scenarios in `harness/scenarios.json` describe what a correct client does in each; the client tests of every language run them.
- Not implemented: CBOR responses, multipart uploads, `webp` output of background removal without a fixture, real rate limiting (scripted 429s instead), concurrency limits.

## How to add an implementation in a new language

1. Open an issue that says in which use cases the language is a realistic choice.
2. Add the toolchain to `harness/languages.toml`: `requires`, `setup`, `run`, `lint` and the minimum versions in `min`.
3. Write `clients/<language>/` with the behaviour in the table above, and tests that run it against the fake API with every scenario in `harness/scenarios.json`. Register the client file names in `CLIENTS` in `harness/__main__.py` so `check` compares the copies.
4. Implement `00-quickstart` first, then the chosen use cases. Use the same region names as the reference language, so the README code blocks line up, and produce the same `result.json` and artefacts. Add the language to `languages` in each `usecase.toml`.
5. Run `uv run python -m harness sync-clients`, `snippets` and `check`. A maintainer then runs `live` with a key and refreshes `expected/`.

## How to add a use case

1. Open an issue with the problem, its constraints, the models, the languages and a dataset whose licence allows redistribution.
2. Create `use-cases/NN-name/` with `usecase.toml` (`contract/usecase.schema.json`): titles and summaries in English and Chinese, models, tier, languages, reference language, the smoke manifest and its credit budget, and `[compare]` tolerances (`[compare_live]` replaces single rules in runs against the real API, for values it does not promise, such as the cache state of a call).
3. Add 3 to 5 smoke items under `assets/smoke/` with `manifest.json`, `truth.json` when there is ground truth, and a row per file in `assets/smoke/ATTRIBUTION.csv`. Full sets are published as release assets, not committed.
4. Write `output.schema.json` for `items[].output`, and `evaluate.py` when the use case reports quality metrics: `evaluate.py --result <result.json> --truth <truth.json>` prints one JSON object of metrics on its last line; bounds go in `[quality]`.
5. Write the reference implementation, with fixtures in `fixtures/` if the synthesised answers of the fake API are not enough, then run `uv run python -m harness check --uc NN --write-offline` to record `expected/offline.json`.
6. Write `README.md` and `README.zh-CN.md` with the same sections. Headings carry an anchor comment, e.g. `## Problem <!-- #problem -->`; code, numbers and figures come only from marker blocks (`<!-- code: NAME -->`, `<!-- metrics: FILE -->`, `<!-- cost -->`, `<!-- figure: FILE -->`). Run `harness snippets`, then `harness i18n --accept NN-name` once the Chinese text matches the English.

## Commands

| Command | Key | What it does |
|---|---|---|
| `check [--uc 00,03] [--lang python] [--write-offline]` | no | contract, client copies, attribution, lint, snippets, i18n, scan, tests, offline matrix against the fake API |
| `live --size smoke\|full [--uc …] [--lang …] [--max-credits N] [--dry-run]` | yes | the real API; the planned list price must fit the cap (smoke: `FOLDBUN_MAX_CREDITS` or 60; full: `--max-credits` required) before anything is sent |
| `refresh` (same options, plus `--record-fixtures`) | yes | `live`, then writes `expected/` and optionally records fixtures |
| `snippets`, `i18n [--accept ID]`, `scan`, `sync-clients` | no | README blocks, translation sync, publication scan, client copies |
| `fakeapi [--port N] [--scenario FILE]` | no | the fake API |
