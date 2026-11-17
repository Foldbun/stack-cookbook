#!/usr/bin/env bash
# First call to the Foldbun Stack API with curl and jq: read the text of an image and see what the call is worth. Needs no key.
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
# endregion

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SECONDS=0

# call METHOD PATH [curl args...]: a 429 or other retryable error is retried after max(retryAfterMs, Retry-After, 1 s) plus
# a little jitter, at most 5 times; daily_limit_reached and free_tier_unavailable are final. When a response says
# X-RateLimit-Remaining: 0, the next call waits X-RateLimit-Reset seconds.
# Leaves the body in $tmp/body and the headers in $tmp/headers; prints the HTTP status.
call() {
  local method="$1" path="$2"; shift 2
  local attempt status code retryable wait remaining reset retry_after
  for attempt in 0 1 2 3 4 5; do
    status="$(curl -sS -o "$tmp/body" -D "$tmp/headers" -w '%{http_code}' -X "$method" "$base$path" \
      "${auth[@]}" -H "User-Agent: $ua" "$@")"
    if [ "$status" -lt 300 ]; then
      remaining="$(grep -i '^x-ratelimit-remaining:' "$tmp/headers" | tr -d '\r' | cut -d' ' -f2 || true)"
      reset="$(grep -i '^x-ratelimit-reset:' "$tmp/headers" | tr -d '\r' | cut -d' ' -f2 || true)"
      if [ "$remaining" = 0 ]; then sleep "${reset:-1}"; fi
      echo "$status"; return
    fi
    code="$(jq -r '.error.code // ""' "$tmp/body")"
    retryable="$(jq -r '.error.retryable // false' "$tmp/body")"
    case "$code" in rate_limited | free_busy) retryable=true ;; daily_limit_reached | free_tier_unavailable) retryable=false ;; esac
    if [ "$retryable" != "true" ] || [ "$attempt" -eq 5 ]; then
      echo "$status"; return
    fi
    retry_after="$(grep -i '^retry-after:' "$tmp/headers" | tr -d '\r' | cut -d' ' -f2 || true)"
    wait="$(jq -r --arg ra "${retry_after:-0}" '[.error.retryAfterMs / 1000, ($ra | tonumber? // 0), 1] | max | . * (1 + 0.25 * (now * 1000 % 1000 / 1000)) | if . > 60 then 60 else . end' "$tmp/body")"
    sleep "$wait"
  done
}

fail() {
  echo "$1 $(jq -r '.error.code + ": " + .error.message' "$tmp/body") (request $(grep -i '^x-request-id:' "$tmp/headers" | tr -d '\r' | cut -d' ' -f2))" >&2
}

status="$(call GET /v1/whoami)"
if [ "$status" != 200 ]; then fail "$status"; exit 1; fi
if [ "$free" = true ]; then
  echo "free tier, project $(jq -r .project.id "$tmp/body")"
else
  echo "key accepted; balance $(jq -r .balance "$tmp/body") credits"
fi

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
  # endregion
  # region first-call
  status="$(call POST /v1/ocr -H "Content-Type: application/json" "${idem[@]}" --data-binary "@$tmp/request")"
  if [ "$status" = 403 ] && [ "$(jq -r .error.code "$tmp/body")" = model_locked ]; then fail "$status"; exit 3; fi
  if [ "$status" -ge 300 ]; then fail "$status"; exit 1; fi
  jq -r '.lines[] | "  \(.confidence * 100 | round / 100)  \(.text)"' "$tmp/body"
  if [ "$free" = true ]; then
    jq -r '"  list price \(.billing.total) credits (free tier, not charged)"' "$tmp/body"
  else
    jq -r '"  list price \(.billing.total) credits, charged \(.cost)"' "$tmp/body"
  fi
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
