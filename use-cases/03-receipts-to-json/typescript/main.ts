// Receipts and invoices to validated JSON: read with ppocr-v6, extract fields by rules, validate, fall back to paddleocr-vl.
//
// Writes <out>/result.json (contract/result.schema.json), <out>/receipts.jsonl (validated receipts) and
// <out>/needs_review.jsonl (items that no model could make pass validation). See README.md.
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, extname, join, resolve } from "node:path";
import { parseArgs } from "node:util";

import { extractReceipt, rowsFromBlocks, rowsFromOcr } from "./extract.ts";
import type { Receipt } from "./extract.ts";
import * as sc from "./stackClient.ts";
import { passed, validate } from "./validate.ts";
import type { Check } from "./validate.ts";

const EXIT_FAILED = 1;
const EXIT_NO_KEY = 2;
const EXIT_TIER = 3;
const EXIT_BUDGET = 4;
const OCR_MODEL = "ppocr-v6";
const FALLBACK_MODEL = "paddleocr-vl";
const PIPELINE = "2"; // changes when extraction or validation rules change; a resumed run only reuses items of the same version

interface Item {
  id: string;
  image: string;
}

interface Attempt {
  model: string;
  receipt: Receipt | null;
  passed: boolean;
  checks: Check[];
  note?: string;
}

interface Entry {
  id: string;
  status: string;
  calls: sc.LedgerRow[];
  output: sc.Json;
  error?: { status: number; code: string; message: string };
  _sha256?: string;
}

// #region idempotency
/** Same file and model give the same key (at most 128 printable ASCII characters). Within 24 hours the API
 * replays the stored answer for a repeated key without running or charging the call, so a restarted run is free.
 * The key names this program ("ts"): the API compares the bytes of the request, and another program that writes
 * the same JSON with different spacing would get `409 idempotency_conflict` under a shared key. */
function idempotencyKey(imageSha256: string, model: string): string {
  return `r03-ts-${imageSha256}-${model}`;
}
// #endregion

const sha256 = (data: Uint8Array): string => createHash("sha256").update(data).digest("hex");

function contentType(name: string): string {
  return { ".png": "image/png", ".webp": "image/webp" }[extname(name).toLowerCase()] ?? "image/jpeg";
}

// #region attempt
/** What an attempt keeps in the output: the receipt as that model read it and the names of the checks it failed. */
function summary(a: Attempt) {
  const failedChecks = a.checks.filter((c) => !c.passed).map((c) => c.name);
  return { model: a.model, passed: a.passed, failedChecks, receipt: a.receipt, ...(a.note !== undefined ? { note: a.note } : {}) };
}

function attempt(model: string, receipt: Receipt, today: string): Attempt {
  const checks = validate(receipt, today);
  return { model, receipt, passed: passed(checks), checks };
}
// #endregion

/** One image through the whole pipeline. Returns the item entry of result.json plus a private `_sha256`. */
async function processItem(client: sc.StackClient, item: Item, base: string, useFallback: boolean, today: string): Promise<Entry> {
  const data = await readFile(join(base, item.image));
  const sha = sha256(data);
  const handle = (await client.upload(data, contentType(item.image))).id; // free; both models read the same handle
  const attempts: Attempt[] = [];

  // 1. ppocr-v6: 0.25 credits, inside the public pool's daily allowance.
  // #region ocr-call
  const ocr = await client.post(
    "/v1/ocr",
    { model: OCR_MODEL, image: sc.imageHandle(handle), options: { words: false } },
    { idempotencyKey: idempotencyKey(sha, OCR_MODEL), tag: item.id },
  );
  attempts.push(attempt(OCR_MODEL, extractReceipt(rowsFromOcr(ocr.lines), OCR_MODEL, ocr.image.width), today));
  // #endregion

  // 2. Only an item that failed validation is read again, by paddleocr-vl on the same handle: 2 credits, paid models only.
  // #region fallback-call
  if (useFallback && !attempts[0].passed) {
    try {
      const page = await client.post(
        "/v1/documents/parse",
        { model: FALLBACK_MODEL, image: sc.imageHandle(handle), options: { blocks: true } },
        { idempotencyKey: idempotencyKey(sha, FALLBACK_MODEL), tag: item.id },
      );
      attempts.push(attempt(FALLBACK_MODEL, extractReceipt(rowsFromBlocks(page.blocks), FALLBACK_MODEL, page.image.width), today));
    } catch (err) {
      if (!(err instanceof sc.StackError) || err.code === "budget_exceeded") throw err;
      // An account without the paid pool (model_locked) or a failed call: the item stays with what ppocr-v6 read.
      attempts.push({ model: FALLBACK_MODEL, receipt: null, passed: false, checks: [], note: err.code });
    }
  }
  // #endregion

  // 3. The result is the first attempt that passed; otherwise the attempt with the most passed checks goes to review.
  const good = attempts.find((a) => a.passed);
  const score = (a: Attempt) => a.checks.filter((c) => c.passed).length;
  const tried = attempts.filter((a) => a.receipt);
  const best = good ?? tried.reduce((top, a) => (score(a) >= score(top) ? a : top)); // ties go to the later attempt
  const output = {
    outcome: good ? "validated" : "needs_review",
    model: best.model,
    receipt: best.receipt,
    checks: best.checks,
    attempts: attempts.map(summary),
  };
  return { id: item.id, status: "ok", calls: client.calls(item.id), output, _sha256: sha };
}

// #region resume
const itemFile = (out: string, itemId: string): string => join(out, "items", `${itemId}.json`);

/** The saved entry of an item finished by an earlier run of this directory, when the file, the rules and the options are unchanged. */
async function loadDone(out: string, item: Item, base: string, useFallback: boolean): Promise<Entry | null> {
  const path = itemFile(out, item.id);
  if (!existsSync(path)) return null;
  const saved = JSON.parse(await readFile(path, "utf8"));
  const key = [sha256(await readFile(join(base, item.image))), PIPELINE, useFallback];
  return JSON.stringify(saved.key) === JSON.stringify(key) && saved.entry.status === "ok" ? saved.entry : null;
}

/** One small file per finished item, written atomically, so a killed run loses at most the items in flight. */
async function saveDone(out: string, entry: Entry, useFallback: boolean): Promise<void> {
  const path = itemFile(out, entry.id);
  await mkdir(dirname(path), { recursive: true });
  const { _sha256, ...clean } = entry;
  const tmp = `${path}.tmp`;
  await writeFile(tmp, JSON.stringify({ key: [_sha256, PIPELINE, useFallback], entry: clean }));
  await rename(tmp, path);
}
// #endregion

function ledgerOf(entries: Entry[]) {
  const rows = entries.flatMap((e) => e.calls);
  const fresh = rows.filter((c) => !c.replayed);
  const round6 = (x: number) => Math.round((x + 1e-12) * 1e6) / 1e6;
  return { calls: rows.length, listTotal: round6(fresh.reduce((s, c) => s + c.list, 0)), chargedTotal: round6(fresh.reduce((s, c) => s + c.charged, 0)) };
}

const localDate = (d: Date): string => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

async function main(): Promise<number> {
  const { values: args } = parseArgs({
    options: {
      manifest: { type: "string", default: join(import.meta.dirname, "../assets/smoke/manifest.json") },
      out: { type: "string", default: "out" },
      concurrency: { type: "string", default: "4" },
      limit: { type: "string" },
      resume: { type: "boolean", default: false }, // reuse the items already finished in --out instead of reading them again
      "no-fallback": { type: "boolean", default: false }, // OCR only: items that fail validation go straight to needs_review.jsonl
    },
  });
  const useFallback = !args["no-fallback"];
  const concurrency = Number(args.concurrency);
  const manifestPath = resolve(args.manifest);
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  const base = dirname(manifestPath);
  const today = localDate(new Date());
  const started = new Date();
  const t0 = performance.now();
  let client: sc.StackClient;
  try {
    client = new sc.StackClient({ maxConcurrency: concurrency }); // FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
    await client.models(); // free: checks the key and loads prices and the per-minute limits the client paces itself by
  } catch (err) {
    if (!(err instanceof sc.StackError)) throw err;
    if (err instanceof sc.MissingApiKey) {
      console.error(err.message);
      return EXIT_NO_KEY;
    }
    console.error(`${err.status} ${err.code}: ${err.message}`);
    return EXIT_FAILED;
  }

  const items: Item[] = (manifest.items as Item[]).slice(0, args.limit === undefined ? undefined : Number(args.limit));
  const entries = new Map<string, Entry>();
  const todo: Item[] = [];
  for (const item of items) {
    const done = args.resume ? await loadDone(args.out, item, base, useFallback) : null;
    if (done) {
      entries.set(item.id, done);
      console.log(`${item.id}: resumed`);
    } else {
      todo.push(item);
    }
  }

  // #region concurrency
  // The client allows `--concurrency` requests in flight and spaces calls by the per-minute limit of the model's pool,
  // so the workers here only keep it busy. One item is one task: its calls are sequential (OCR, then maybe parse).
  let code = 0;
  let next = 0;
  async function worker(): Promise<void> {
    while (code === 0 && next < todo.length) {
      const item = todo[next++];
      let entry: Entry;
      try {
        entry = await processItem(client, item, base, useFallback, today);
      } catch (err) {
        if (!(err instanceof sc.StackError)) throw err;
        console.error(`${item.id}: ${err.describe()}`);
        if (err.code === "model_locked" || err.code === "budget_exceeded") {
          code = err.code === "model_locked" ? EXIT_TIER : EXIT_BUDGET; // the others stop picking up new items
          continue;
        }
        entry = { id: item.id, status: "error", calls: client.calls(item.id), output: null, error: { status: err.status, code: err.code, message: err.message } };
      }
      entries.set(item.id, entry);
      if (entry.status === "ok") {
        await saveDone(args.out, entry, useFallback);
        console.log(`${item.id}: ${entry.output.outcome} (${entry.output.model})`);
      }
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, concurrency) }, worker));
  // #endregion

  const done: Entry[] = items.flatMap((i) => {
    const entry = entries.get(i.id);
    if (!entry) return [];
    const { _sha256, ...clean } = entry;
    return [clean];
  });
  await mkdir(args.out, { recursive: true });
  // #region needs-review
  // What no model could make pass goes to a person, with the best receipt as a starting point and the reasons.
  const receipts: string[] = [];
  const review: string[] = [];
  for (const entry of done) {
    const out = entry.output;
    if (out === null) continue;
    if (out.outcome === "validated") {
      receipts.push(JSON.stringify({ id: entry.id, model: out.model, receipt: out.receipt }));
    } else {
      const reasons = out.checks.filter((c: Check) => !c.passed).map((c: Check) => c.detail);
      review.push(JSON.stringify({ id: entry.id, reasons, model: out.model, receipt: out.receipt }));
    }
  }
  await writeFile(join(args.out, "receipts.jsonl"), receipts.map((l) => l + "\n").join(""));
  await writeFile(join(args.out, "needs_review.jsonl"), review.map((l) => l + "\n").join(""));
  // #endregion
  const result = {
    usecase: manifest.usecase,
    language: "typescript",
    client: `stackClient ${sc.VERSION}`,
    startedAt: started.toISOString().replace(/\.\d+Z$/, "Z"),
    elapsedMs: Math.floor(performance.now() - t0),
    items: done,
    ledger: ledgerOf(done),
  };
  await writeFile(join(args.out, "result.json"), JSON.stringify(result, null, 2) + "\n");
  return code;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error(err instanceof sc.StackError ? err.describe() : err);
    process.exit(EXIT_FAILED);
  },
);
