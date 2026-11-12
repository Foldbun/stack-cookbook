#!/usr/bin/env bash
# First call to the Foldbun Stack API with curl and jq: check the key, upload an image, read its text, show the cost.
set -euo pipefail

manifest="$(dirname "$0")/../assets/smoke/manifest.json"
out="out"
limit=0
while [ $# -gt 0 ]; do
  case "$1" in
    --manifest) manifest="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --limit) limit="$2"; shift 2 ;;
    --concurrency) shift 2 ;;  # accepted for the common interface; this script is sequential
    --resume) shift ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

# region client
if [ -z "${FOLDBUN_API_KEY:-}" ]; then
  echo 'FOLDBUN_API_KEY is not set. See "API keys" in the README; every use case page shows the output and cost of a real run, so it can be read without a key.' >&2
  exit 2
fi
base="${FOLDBUN_API_BASE:-https://api.stack.foldbun.com}"
ua="foldbun-stack-cookbook/1.0.0 (curl; +https://github.com/Foldbun/stack-cookbook)"
# endregion

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SECONDS=0

# call METHOD PATH [curl args...]: retries retryable errors after max(retryAfterMs, 1 s), at most 5 times.
# Leaves the body in $tmp/body and the headers in $tmp/headers; prints the HTTP status.
call() {
  local method="$1" path="$2"; shift 2
  local attempt status
  for attempt in 0 1 2 3 4 5; do
    status="$(curl -sS -o "$tmp/body" -D "$tmp/headers" -w '%{http_code}' -X "$method" "$base$path" \
      -H "Authorization: Bearer $FOLDBUN_API_KEY" -H "User-Agent: $ua" "$@")"
    if [ "$status" -lt 300 ] || [ "$(jq -r '.error.retryable // false' "$tmp/body")" != "true" ] || [ "$attempt" -eq 5 ]; then
      echo "$status"; return
    fi
    sleep "$(jq -r '[.error.retryAfterMs / 1000, 1] | max | if . > 60 then 60 else . end' "$tmp/body")"
  done
}

fail() {
  echo "$1 $(jq -r '.error.code + ": " + .error.message' "$tmp/body") (request $(grep -i '^x-request-id:' "$tmp/headers" | tr -d '\r' | cut -d' ' -f2))" >&2
}

status="$(call GET /v1/models)"
if [ "$status" != 200 ]; then fail "$status"; exit 1; fi
echo "key accepted; ppocr-v6 available: $(jq '.data[] | select(.id == "ppocr-v6") | .available' "$tmp/body")"

dir="$(dirname "$manifest")"
echo '[]' > "$tmp/items"
count=0
while read -r item; do
  if [ "$limit" -gt 0 ] && [ "$count" -ge "$limit" ]; then break; fi
  count=$((count + 1))
  id="$(jq -r .id <<<"$item")"
  file="$dir/$(jq -r .image <<<"$item")"
  case "$file" in *.webp) type=image/webp ;; *.png) type=image/png ;; *) type=image/jpeg ;; esac
  echo "$id"
  # region image-input
  status="$(call POST /v1/images -H "Content-Type: $type" --data-binary "@$file")"
  if [ "$status" -ge 300 ]; then fail "$status"; exit 1; fi
  handle="$(jq -r .id "$tmp/body")"
  # endregion
  # region first-call
  key="$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)"
  jq -n --arg h "$handle" '{model: "ppocr-v6", image: {id: $h}}' > "$tmp/request"
  status="$(call POST /v1/ocr -H "Content-Type: application/json" -H "Idempotency-Key: $key" --data-binary "@$tmp/request")"
  if [ "$status" = 403 ] && [ "$(jq -r .error.code "$tmp/body")" = model_locked ]; then fail "$status"; exit 3; fi
  if [ "$status" -ge 300 ]; then fail "$status"; exit 1; fi
  jq -r '.lines[] | "  \(.confidence * 100 | round / 100)  \(.text)"' "$tmp/body"
  jq -r '"  list price \(.billing.total) credits, charged \(.cost)"' "$tmp/body"
  # endregion
  replayed=false
  if grep -qi '^idempotent-replayed: true' "$tmp/headers"; then replayed=true; fi
  jq --arg id "$id" --argjson replayed "$replayed" --slurpfile items "$tmp/items" -n '
    input as $r | $items[0] + [{
      id: $id, status: "ok",
      calls: [{endpoint: "/v1/ocr", model: $r.model, list: $r.billing.total,
               charged: (if $replayed then 0 else $r.cost end), cache: null,
               queueMs: $r.timing.queueMs, processingMs: $r.timing.processingMs, replayed: $replayed}],
      output: {image: {width: $r.image.width, height: $r.image.height},
               lines: [$r.lines[] | {text, confidence, bbox}]}
    }]' "$tmp/body" > "$tmp/items.new"
  mv "$tmp/items.new" "$tmp/items"
done < <(jq -c '.items[]' "$manifest")

mkdir -p "$out"
jq --arg started "$started" --argjson elapsed "$((SECONDS * 1000))" --arg uc "$(jq -r .usecase "$manifest")" '{
  usecase: $uc, language: "curl", client: "curl", startedAt: $started, elapsedMs: $elapsed, items: .,
  ledger: {calls: ([.[].calls[]] | length),
           listTotal: ([.[].calls[] | select(.replayed | not) | .list] | add // 0),
           chargedTotal: ([.[].calls[] | select(.replayed | not) | .charged] | add // 0)}
}' "$tmp/items" > "$out/result.json"
