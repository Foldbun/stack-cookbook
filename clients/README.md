# Client files

Each language has one small client file for the Foldbun Stack API. It is not an SDK and is not published to any package registry: every implementation under `use-cases/` holds a byte-identical copy, so a use case can be copied out of the repository and run on its own. `harness check` fails when a copy differs from the original here; `harness sync-clients` overwrites the copies.

| | Python | TypeScript | Go |
|---|---|---|---|
| File | `python/stack_client.py` | `typescript/stackClient.ts` | `go/stackclient/client.go` |
| Copy in an implementation | `stack_client.py` | `stackClient.ts` | `stackclient/client.go` (package `stackclient`) |
| Optional second file | | | `go/stackclient/rle.go`: COCO compressed RLE masks to and from a bitmap (copy it when a use case needs pixel masks) |
| Toolchain | Python 3.12+ | Node 24+ (types are stripped at run time, `tsc` checks them) | Go 1.22+ |
| Dependencies | standard library | Node built-ins (`fetch`, `crypto`) | standard library |
| Result of a call | `dict` | `any` (parsed JSON) | `json.RawMessage`, decoded into the caller's own struct |
| Batch | generator of `(event, data)` | async generator of `[event, data]` | callback `func(Event) error` (return `ErrStop` to end early) |
| Errors | `StackError`, `MissingApiKey`, `BudgetExceeded` | the same classes | `*stackclient.Error`; `stackclient.Code(err)` gives the code |

## What "identical semantics" covers

The three files send the same requests and produce the same ledger for the same inputs. The rules are the table in [CONTRIBUTING.md](../CONTRIBUTING.md#layout-and-contract); in short:

- configuration from `FOLDBUN_API_KEY`, `FOLDBUN_API_BASE` and `FOLDBUN_MAX_CREDITS`, and the same `User-Agent` (with the language name);
- an `Idempotency-Key` on every inference call and batch, reused on every retry;
- retries only for errors with `retryable: true`, after `max(retryAfterMs, Retry-After, 1 s)` (at most 60 s, at most 5 retries); network errors are retried with a doubling pause;
- on `404 image_not_found` for a handle the client uploaded: upload again and repeat the call once, with a new idempotency key;
- a token bucket per pool, at most 4 requests in flight, and the credit cap checked against the worst-case list price before anything is sent (the refusal text is the same);
- the ledger: one row per successful call or `ok` batch item, with the fields of `contract/result.schema.json`; replayed rows are counted but add nothing to the totals;
- typed errors that carry `status`, `code`, `message`, `retryable`, `retryAfterMs` and the request id.

It does not cover the shape of the language API (see the table), the exact wording of network error messages, or the way timeouts are enforced: the Python client uses separate connect and read timeouts, while `fetch` and Go's `net/http` get one deadline for the exchange (connect plus read time), plus an idle timer on batch streams.

## Conformance tests

All three clients run the scenarios of [`harness/scenarios.json`](../harness/scenarios.json) against the fake API and assert the same observable results: the requests that were sent, the retries and their timing, the idempotency keys, and the final ledger.

| Language | Tests | Run by hand |
|---|---|---|
| Python | `tests/test_python_client.py` | `uv run pytest tests/test_python_client.py` |
| TypeScript | `typescript/stackClient.test.ts` | `FAKE_API_URL=<fake API> node --test` in `clients/typescript` (after `pnpm install`) |
| Go | `go/stackclient/*_test.go` | `FAKE_API_URL=<fake API> go test ./...` in `clients/go` |

`tests/test_client_conformance.py` starts the fake API, sets `FAKE_API_URL` and runs the TypeScript and Go tests together with `tsc --noEmit`, `go vet` and `gofmt`, so `uv run python -m harness check` covers all three. Start your own fake API with `uv run python -m harness fakeapi --port 8787`.

## COCO RLE (Go)

`go/stackclient/rle.go` decodes the `counts` of a `coco` mask into a bitmap (`DecodeCOCORLE(height, width, counts)`, where `height` and `width` are the two numbers of `size`) and encodes a bitmap back (`EncodeCOCORLE`). Runs are column-major and start with background, as in the API description. The test vectors in `go/stackclient/testdata/rle_vectors.json` come from `gen_rle_vectors.py` next to them, an independent implementation; they are committed and never generated at run time.

## Changing a client

Change the original, then run `uv run python -m harness sync-clients`. A change of behaviour belongs in all three languages, with a test in each. Bump `VERSION` in the three files together.
