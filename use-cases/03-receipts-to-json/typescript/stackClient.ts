// Copied from clients/typescript/ in github.com/Foldbun/stack-cookbook. Edit the original there.
/**
 * Minimal client for the Foldbun Stack API (Node 22+ built-ins only: fetch, crypto).
 *
 * Semantics shared with the Python and Go client files:
 *
 * - Free tier: without FOLDBUN_API_KEY a call carries only `X-Foldbun-Project` (FOLDBUN_PROJECT_ID, default: the
 *   Cookbook's shared project) and is not charged. The free tier takes inline base64 images and single calls only.
 * - Paid: with FOLDBUN_API_KEY a call carries `Authorization` and the project header (the call's source project), is
 *   charged at list price and gets an idempotency key that is reused on retry.
 * - Limits: a 429 (`Retry-After`, `retryAfterMs`) is retried a bounded number of times with jitter; when
 *   `X-RateLimit-Remaining` is 0 the next call waits `X-RateLimit-Reset` seconds. `daily_limit_reached` and
 *   `free_tier_unavailable` are final.
 * - Also: base URL from FOLDBUN_API_BASE, a concurrency cap, a credit cap for keyed runs (FOLDBUN_MAX_CREDITS) and a
 *   ledger of every call.
 */
import { randomUUID } from "node:crypto";

export const VERSION = "1.2.0";
export const DEFAULT_BASE = "https://api.stack.foldbun.com";
export const USER_AGENT = `foldbun-stack-cookbook/${VERSION} (typescript; +https://github.com/Foldbun/stack-cookbook)`;
// The Cookbook's own project. A project ID is not a secret: it names who is calling and authorises nothing beyond
// the free tier, whose limits are shared by every reader of the Cookbook. To go beyond trying the recipes, create
// your own project in the console and set FOLDBUN_PROJECT_ID (or use an API key).
export const COOKBOOK_PROJECT = "fbp_2kIj7bWkULQkLAbceHhfLW4eY6O0";
const DEFAULT_MODEL: Record<string, string> = {
  "/v1/ocr": "ppocr-v6",
  "/v1/documents/parse": "paddleocr-vl",
  "/v1/remove-background": "birefnet",
  "/v1/refine": "sam3",
};
// [connect, read] timeouts in seconds
type Timeout = readonly [number, number];
const T_MODELS: Timeout = [10, 10];
const T_UPLOAD: Timeout = [10, 60];
const T_CALL: Timeout = [10, 90];
const T_BATCH: Timeout = [10, 45];
const BATCH_MAX_S = 600;
const MAX_RETRIES = 5;
const MAX_WAIT_S = 60;
const JOB_FINAL = new Set(["completed", "cancelled", "expired", "stopped"]); // final job statuses
const JOB_PAGE = 1000; // results per page
// Codes that are retried even when the error does not say `retryable`, and codes that are never retried.
const RETRY_CODES = new Set(["rate_limited", "free_busy"]);
const FINAL_CODES = new Set(["daily_limit_reached", "free_tier_unavailable"]);
const FREE_HINT: Record<string, string> = {
  daily_limit_reached:
    " The daily limit of the free tier is shared by everyone using this project and resets at 00:00 UTC;" +
    " set FOLDBUN_API_KEY to continue now, or FOLDBUN_PROJECT_ID to use your own project.",
  free_tier_unavailable: " The free tier is not available here; set FOLDBUN_API_KEY to use an API key.",
};

// API payloads are plain JSON; the client reads only the few fields it needs.
export type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any

/** An API error (`status`, `code`, ...) or a local refusal (`status` 0). */
export class StackError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly retryAfterMs: number;
  readonly requestId: string | null;

  constructor(status: number, code: string, message: string, retryable = false, retryAfterMs = 0, requestId: string | null = null) {
    super(message);
    this.name = "StackError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.retryAfterMs = retryAfterMs;
    this.requestId = requestId;
  }

  /** `status code: message (request id)`, the line the use cases print. */
  describe(): string {
    return `${this.status} ${this.code}: ${this.message}` + (this.requestId ? ` (request ${this.requestId})` : "");
  }
}

/** The run needs FOLDBUN_API_KEY because the free tier cannot do it; `reason` says what the free tier lacks. */
export class MissingApiKey extends StackError {
  constructor(reason: string) {
    super(0, "missing_api_key", `FOLDBUN_API_KEY is not set, and this needs a key: ${reason}.`);
    this.name = "MissingApiKey";
  }
}

export class BudgetExceeded extends StackError {
  constructor(message: string) {
    super(0, "budget_exceeded", message);
    this.name = "BudgetExceeded";
  }
}

/** Inline image input: simplest for a single call. */
export function imageBase64(data: Uint8Array): { base64: string } {
  return { base64: Buffer.from(data).toString("base64") };
}

/** Uploaded image input (needs a key): reuse one upload across calls and models; required in batches. */
export function imageHandle(handle: string): { id: string } {
  return { id: handle };
}

export interface CallOptions {
  /** Reused on every retry; a new UUID when omitted. */
  idempotencyKey?: string;
  /** Item tag: the ledger rows of this call are returned by `calls(tag)`. */
  tag?: string;
  /** [connect, read] seconds. */
  timeout?: readonly [number, number];
}

export interface JobOptions {
  /** Reused on every retry; a new UUID when omitted. */
  idempotencyKey?: string;
  /** Returned unchanged as `job.metadata` (at most 16 keys and 2 KiB). */
  metadata?: Record<string, unknown>;
}

export interface JobResultsOptions {
  /** Item tag: the ledger rows of these results are returned by `calls(tag)`. */
  tag?: string;
  /** Items per page, 1 to 1,000 (default 1,000). */
  limit?: number;
}

export interface ClientOptions {
  apiKey?: string;
  baseUrl?: string;
  project?: string;
  maxConcurrency?: number;
  maxCredits?: number;
}

export interface LedgerRow {
  endpoint: string;
  model: string | null;
  list: number;
  charged: number;
  cache: string | null;
  queueMs: number;
  processingMs: number;
  replayed: boolean;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

class Semaphore {
  private free: number;
  private waiting: Array<() => void> = [];
  constructor(n: number) {
    this.free = n;
  }
  async acquire(): Promise<void> {
    if (this.free > 0) {
      this.free--;
      return;
    }
    await new Promise<void>((resolve) => this.waiting.push(resolve));
  }
  release(): void {
    const next = this.waiting.shift();
    if (next) next();
    else this.free++;
  }
}

/** A response body read as lines, with an idle timeout and a total deadline. */
class Stream {
  private idle?: NodeJS.Timeout;
  private readonly total: NodeJS.Timeout;
  private readonly res: Response;
  private readonly ctl: AbortController;
  private readonly idleMs: number;
  timedOut = false;
  constructor(res: Response, ctl: AbortController, idleMs: number, totalMs: number) {
    this.res = res;
    this.ctl = ctl;
    this.idleMs = idleMs;
    this.total = setTimeout(() => {
      this.timedOut = true;
      ctl.abort();
    }, totalMs);
    this.bump();
  }
  private bump(): void {
    clearTimeout(this.idle);
    this.idle = setTimeout(() => this.ctl.abort(), this.idleMs);
  }
  async *lines(): AsyncGenerator<string> {
    const reader = this.res.body!.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        this.bump();
        buf += decoder.decode(value, { stream: true });
        let i: number;
        while ((i = buf.indexOf("\n")) >= 0) {
          yield buf.slice(0, i).replace(/\r$/, "");
          buf = buf.slice(i + 1);
        }
      }
      if (buf) yield buf.replace(/\r$/, "");
    } finally {
      this.close();
      reader.cancel().catch(() => {});
    }
  }
  close(): void {
    clearTimeout(this.idle);
    clearTimeout(this.total);
  }
}

interface Reply {
  status: number;
  headers: Headers;
  raw: Uint8Array;
  stream?: Stream;
}

export class StackClient {
  readonly apiKey: string | null; // optional: without it, the free tier
  readonly project: string;
  readonly free: boolean;
  readonly maxCredits: number | null;
  readonly ledger: Array<[string | undefined, LedgerRow]> = []; // one row per successful inference call; see calls()
  private readonly base: string;
  private readonly sem: Semaphore;
  private modelsPromise: Promise<Record<string, Json>> | null = null;
  private uploads = new Map<string, { data: Uint8Array; contentType: string }>();
  private spent = 0;
  private jobs = new Map<string, { hold: number; cost: number }>(); // job id -> credits still held, cost seen so far
  private jobItems = new Set<string>(); // `${job id}:${index}` of the results already in the ledger
  private reserved = 0;
  private gate = new Map<string, number>(); // model -> performance.now() before which the next call waits

  constructor(opts: ClientOptions = {}) {
    this.apiKey = opts.apiKey || process.env.FOLDBUN_API_KEY || null;
    this.project = opts.project || process.env.FOLDBUN_PROJECT_ID || COOKBOOK_PROJECT;
    this.free = !this.apiKey;
    this.base = (opts.baseUrl || process.env.FOLDBUN_API_BASE || DEFAULT_BASE).replace(/\/+$/, "");
    const cap = opts.maxCredits ?? process.env.FOLDBUN_MAX_CREDITS;
    this.maxCredits = cap === undefined || cap === "" ? null : Number(cap);
    this.sem = new Semaphore(opts.maxConcurrency ?? 4);
  }

  // #region http
  private async request(method: string, path: string, body: Uint8Array | string | undefined, headers: Record<string, string>, timeout: Timeout, stream: boolean): Promise<Reply> {
    const ctl = new AbortController();
    const h: Record<string, string> = { "X-Foldbun-Project": this.project, "User-Agent": USER_AGENT, Accept: "application/json" };
    if (this.apiKey) h.Authorization = `Bearer ${this.apiKey}`;
    Object.assign(h, headers);
    // fetch has one signal for the whole exchange, so connect and read seconds add up. A stream gets its own
    // idle timer once the headers are in.
    const timer = setTimeout(() => ctl.abort(), (timeout[0] + timeout[1]) * 1000);
    try {
      const res = await fetch(this.base + path, { method, headers: h, body: body as RequestInit["body"], signal: ctl.signal, redirect: "manual" });
      if (stream && res.status < 300) {
        return { status: res.status, headers: res.headers, raw: new Uint8Array(), stream: new Stream(res, ctl, timeout[1] * 1000, BATCH_MAX_S * 1000) };
      }
      return { status: res.status, headers: res.headers, raw: new Uint8Array(await res.arrayBuffer()) };
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Send with retries: `retryable` API errors (and 429s) always, network errors only when retryNetwork.
   *
   * `gate` names the rate-limit window of the call (the model): when a response said `X-RateLimit-Remaining: 0`,
   * the next call of that window waits `X-RateLimit-Reset` seconds first.
   */
  private async send(method: string, path: string, body?: Uint8Array | string, headers: Record<string, string> = {}, timeout: Timeout = T_CALL, retryNetwork = true, stream = false, gate?: string): Promise<Reply> {
    for (let attempt = 0; ; attempt++) {
      await this.waitGate(gate);
      let reply: Reply;
      try {
        reply = await this.request(method, path, body, headers, timeout, stream);
      } catch (exc) {
        const detail = exc instanceof Error ? (exc.cause instanceof Error ? exc.cause.message : exc.message) : String(exc);
        if (!retryNetwork || attempt === MAX_RETRIES) throw new StackError(0, "network_error", detail, true);
        await sleep(Math.min(2 ** attempt, MAX_WAIT_S) * 1000);
        continue;
      }
      if (reply.status < 300) {
        this.noteLimits(gate, reply);
        return reply;
      }
      const err = this.error(reply);
      if (FINAL_CODES.has(err.code) || !(err.retryable || RETRY_CODES.has(err.code)) || attempt === MAX_RETRIES) throw err;
      const header = reply.headers.get("Retry-After");
      const wait = Math.max(err.retryAfterMs / 1000, header && /^\d+$/.test(header) ? Number(header) : 0, 1);
      await sleep(Math.min(wait + Math.random() * Math.min(wait * 0.25, 2), MAX_WAIT_S) * 1000); // jitter on top, never shorter
    }
  }

  private async waitGate(gate: string | undefined): Promise<void> {
    while (gate !== undefined) {
      const wait = (this.gate.get(gate) ?? 0) - performance.now();
      if (wait <= 0) return;
      await sleep(wait);
    }
  }

  /** X-RateLimit-Remaining 0: hold the next call of this window for X-RateLimit-Reset seconds. */
  private noteLimits(gate: string | undefined, reply: Reply): void {
    const remaining = reply.headers.get("X-RateLimit-Remaining");
    const reset = reply.headers.get("X-RateLimit-Reset");
    if (gate === undefined || remaining === null || !/^\d+$/.test(remaining.trim()) || Number(remaining) > 0) return;
    const n = reset ? Number(reset) : NaN;
    const seconds = reset && Number.isFinite(n) ? Math.min(Math.max(n, 0), MAX_WAIT_S) : 1;
    this.gate.set(gate, Math.max(this.gate.get(gate) ?? 0, performance.now() + seconds * 1000));
  }

  private error(reply: Reply): StackError {
    const rid = reply.headers.get("X-Request-Id");
    const text = new TextDecoder().decode(reply.raw);
    try {
      const e = JSON.parse(text).error;
      const message: string = e.message + (this.free ? (FREE_HINT[e.code] ?? "") : "");
      return new StackError(reply.status, e.code, message, e.retryable ?? false, e.retryAfterMs ?? 0, rid);
    } catch {
      return new StackError(reply.status, "http_error", text.slice(0, 200), reply.status >= 500, 0, rid);
    }
  }
  // #endregion

  async get(path: string): Promise<Json> {
    const { raw } = await this.send("GET", path, undefined, {}, T_MODELS);
    return JSON.parse(new TextDecoder().decode(raw));
  }

  /** Refuse a feature the free tier does not have (uploads, handles, batches, big runs): throws MissingApiKey. */
  requireKey(reason: string): void {
    if (this.free) throw new MissingApiKey(reason);
  }

  /** GET /v1/whoami: the project (free) or the account (key) behind the credentials. */
  whoami(): Promise<Json> {
    return this.get("/v1/whoami");
  }

  /** `GET /v1/models` once, keyed by model id. Works with a project ID alone (each model then also carries its free-tier limits). */
  async models(): Promise<Record<string, Json>> {
    if (!this.modelsPromise) {
      this.modelsPromise = this.get("/v1/models").then(
        (r) => Object.fromEntries((r.data as Json[]).map((m) => [m.id, m])),
        (err) => {
          this.modelsPromise = null;
          throw err;
        },
      );
    }
    return this.modelsPromise;
  }

  // #region upload
  /** POST /v1/images with the raw bytes. Needs a key; returns {id, width, height, bytes, contentType}. */
  async upload(data: Uint8Array, contentType = "image/jpeg"): Promise<Json> {
    this.requireKey("image uploads are not part of the free tier, which takes inline base64 images");
    const { raw } = await this.send("POST", "/v1/images", data, { "Content-Type": contentType }, T_UPLOAD);
    const info = JSON.parse(new TextDecoder().decode(raw));
    this.uploads.set(info.id, { data, contentType });
    return info;
  }
  // #endregion

  // #region post
  /** One inference call. The idempotency key is reused on every retry, so a retry never charges twice. */
  async post(path: string, body: Json, opts: CallOptions = {}): Promise<Json> {
    const model: string | undefined = body.model || DEFAULT_MODEL[path];
    if (body.image?.id !== undefined) this.requireKey("image handles come from uploads, which are not part of the free tier");
    const worst = this.free ? 0 : estimate(await this.models(), path, body, model);
    this.reserve(worst);
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (!this.free) headers["Idempotency-Key"] = opts.idempotencyKey ?? randomUUID();
    const data = JSON.stringify(body);
    const timeout = opts.timeout ?? T_CALL;
    let reply: Reply;
    try {
      await this.sem.acquire();
      try {
        try {
          reply = await this.send("POST", path, data, headers, timeout, true, false, model);
        } catch (err) {
          const handle: string | undefined = body.image?.id;
          const saved = handle === undefined ? undefined : this.uploads.get(handle);
          if (!(err instanceof StackError) || err.code !== "image_not_found" || !saved) throw err;
          // Evicted: upload again (same handle) and repeat once. A 404 is stored under the idempotency
          // key and was not charged, so the repeat needs a new key.
          await this.upload(saved.data, saved.contentType);
          headers["Idempotency-Key"] = randomUUID();
          reply = await this.send("POST", path, data, headers, timeout, true, false, model);
        }
      } finally {
        this.sem.release();
      }
    } finally {
      this.release(worst);
    }
    const result = JSON.parse(new TextDecoder().decode(reply.raw));
    this.record(path, result, reply.headers.get("Idempotent-Replayed") === "true", opts.tag);
    return result;
  }
  // #endregion

  // #region batch
  /** POST /v1/batch/{endpoint} as Server-Sent Events. Yields [event, data] for progress, item and summary. Needs a key. */
  async *batch(endpoint: string, body: Json, opts: CallOptions = {}): AsyncGenerator<[string, Json]> {
    this.requireKey("batch requests are not part of the free tier");
    const path = `/v1/batch/${endpoint}`;
    const single = `/v1/${endpoint}`;
    const model: string | undefined = body.model || DEFAULT_MODEL[single];
    const models = await this.models();
    const worst = (body.items as Json[]).reduce((sum, it) => sum + estimate(models, single, it, model), 0);
    this.reserve(worst);
    const headers = { "Content-Type": "application/json", Accept: "text/event-stream", "Idempotency-Key": opts.idempotencyKey ?? randomUUID() };
    try {
      await this.sem.acquire();
      try {
        const { stream } = await this.send("POST", path, JSON.stringify(body), headers, T_BATCH, true, true, model);
        let event: string | null = null;
        try {
          for await (const line of stream!.lines()) {
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:") && event) {
              const data = JSON.parse(line.slice(5));
              if (event === "item" && data.status === "ok") this.record(path, data.result, Boolean(data.replayed), opts.tag);
              yield [event, data];
              if (event === "summary") return;
            }
          }
        } catch (exc) {
          if (exc instanceof StackError) throw exc;
          if (stream!.timedOut) throw new StackError(0, "batch_timeout", `batch exceeded ${BATCH_MAX_S} s`);
          throw new StackError(0, "network_error", `batch stream: ${exc instanceof Error ? exc.message : String(exc)}`, true);
        } finally {
          stream!.close();
        }
      } finally {
        this.sem.release();
      }
    } finally {
      this.release(worst);
    }
  }
  // #endregion

  // #region jobs
  /**
   * POST /v1/jobs: many embedding calls queued as one background job at a tenth of the list price. `items` are the
   * bodies of single embedding calls without `model` (`{ image: imageHandle(id) }` or `{ texts: [...] }`; images are
   * handles only). Needs a key. Returns the job (status `queued`); follow it with getJob and read jobResults.
   */
  async createJob(model: string, items: Json[], opts: JobOptions = {}): Promise<Json> {
    this.requireKey("async jobs are not part of the free tier");
    const body: Json = { endpoint: "embeddings", model, items };
    if (opts.metadata !== undefined) body.metadata = opts.metadata;
    const worst = estimateJob(await this.models(), model, items);
    this.reserve(worst);
    const headers = { "Content-Type": "application/json", "Idempotency-Key": opts.idempotencyKey ?? randomUUID() };
    let job: Json;
    try {
      await this.sem.acquire();
      try {
        const reply = await this.send("POST", "/v1/jobs", JSON.stringify(body), headers, T_CALL, true, false, model);
        job = JSON.parse(new TextDecoder().decode(reply.raw));
      } finally {
        this.sem.release();
      }
    } catch (err) {
      this.release(worst);
      throw err;
    }
    if (this.jobs.has(job.id)) this.release(worst); // a replay of a job this client already holds
    else this.jobs.set(job.id, { hold: worst, cost: 0 });
    return job;
  }

  /** GET /v1/jobs/{id}: status, `counts` and the `cost` so far. Poll at a relaxed pace (every 30 s or more). */
  async getJob(jobId: string): Promise<Json> {
    this.requireKey("async jobs are not part of the free tier");
    const { raw } = await this.send("GET", `/v1/jobs/${jobId}`, undefined, {}, T_MODELS);
    return this.trackJob(JSON.parse(new TextDecoder().decode(raw)));
  }

  /**
   * GET /v1/jobs/{id}/results, page after page by cursor. Yields every finished item `{index, status, result?, error?}`
   * in ascending index; `ok` items go into the ledger once (`cost` of the item), however often they are read.
   * Items keep finishing while the job runs: read the results after getJob reports a final status.
   */
  async *jobResults(jobId: string, opts: JobResultsOptions = {}): AsyncGenerator<Json> {
    this.requireKey("async jobs are not part of the free tier");
    let cursor: string | null = null;
    for (;;) {
      const query = new URLSearchParams({ limit: String(opts.limit ?? JOB_PAGE) });
      if (cursor) query.set("cursor", cursor);
      const { raw } = await this.send("GET", `/v1/jobs/${jobId}/results?${query}`, undefined, {}, T_CALL);
      const page = JSON.parse(new TextDecoder().decode(raw));
      for (const item of page.items as Json[]) {
        if (item.status === "ok") this.recordJobItem(jobId, item, opts.tag);
        yield item;
      }
      cursor = page.next ?? null;
      if (!cursor) return;
    }
  }

  /** POST /v1/jobs/{id}/cancel: items that have not started are cancelled and never charged. Returns the job. */
  async cancelJob(jobId: string): Promise<Json> {
    this.requireKey("async jobs are not part of the free tier");
    const { raw } = await this.send("POST", `/v1/jobs/${jobId}/cancel`, "", { "Content-Type": "application/json" }, T_MODELS);
    return this.trackJob(JSON.parse(new TextDecoder().decode(raw)));
  }

  /** Spend follows the job's `cost`; the rest of its hold is released once the job is final. */
  private trackJob(job: Json): Json {
    const held = this.jobs.get(job.id);
    if (held) {
      const delta = Math.max(0, job.cost - held.cost);
      const take = Math.min(delta, held.hold);
      this.spent += delta;
      this.reserved -= take;
      held.hold -= take;
      held.cost = Math.max(held.cost, job.cost);
      if (JOB_FINAL.has(job.status)) {
        this.reserved -= held.hold;
        held.hold = 0;
      }
    }
    return job;
  }

  private recordJobItem(jobId: string, item: Json, tag?: string): void {
    const id = `${jobId}:${item.index}`;
    if (this.jobItems.has(id)) return;
    this.jobItems.add(id);
    this.record("/v1/jobs", item.result, false, tag, false);
  }
  // #endregion

  // #region ledger
  private record(endpoint: string, result: Json, replayed: boolean, tag?: string, spend = true): void {
    const billing = result.billing ?? {};
    const row: LedgerRow = {
      endpoint,
      model: result.model ?? null,
      list: billing.total ?? billing.listPrice ?? result.cost ?? 0,
      charged: replayed || this.free ? 0 : (result.cost ?? 0),
      cache: result.cache ?? null,
      queueMs: result.timing?.queueMs ?? 0,
      processingMs: result.timing?.processingMs ?? 0,
      replayed,
    };
    this.ledger.push([tag, row]);
    if (!replayed && spend) this.spent += row.list;
  }

  /** Ledger rows (the `calls[]` of result.json), optionally only those made with `tag`. */
  calls(tag?: string): LedgerRow[] {
    return this.ledger.filter(([t]) => tag === undefined || t === tag).map(([, r]) => ({ ...r }));
  }

  summary(): { calls: number; listTotal: number; chargedTotal: number } {
    const rows = this.ledger.map(([, r]) => r).filter((r) => !r.replayed);
    return {
      calls: this.ledger.length,
      listTotal: round(rows.reduce((s, r) => s + r.list, 0)),
      chargedTotal: round(rows.reduce((s, r) => s + r.charged, 0)),
    };
  }
  // #endregion

  private reserve(worst: number): void {
    if (this.maxCredits !== null && this.spent + this.reserved + worst > this.maxCredits + 1e-9) {
      throw new BudgetExceeded(`call refused: up to ${g(worst)} credits would pass FOLDBUN_MAX_CREDITS=${g(this.maxCredits)} (spent ${g(this.spent)})`);
    }
    this.reserved += worst;
  }

  private release(worst: number): void {
    this.reserved -= worst;
  }
}

function round(x: number): number {
  return Math.floor(x * 1e6 + 0.5) / 1e6;
}

/** Python's `{x:g}`: six significant digits, no trailing zeros. */
function g(x: number): string {
  if (x === 0) return "0";
  const exp = Number(x.toExponential(5).split("e")[1]);
  if (exp < -4 || exp >= 6) return x.toExponential(5).replace(/\.?0+e/, "e").replace(/e([+-])(\d)$/, "e$10$2");
  return x.toFixed(5 - exp).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

/** Worst-case cost of an async job: the async price (`asyncPrices` of the model) of every item. */
export function estimateJob(models: Record<string, Json>, model: string, items: Json[]): number {
  const price: Record<string, number> = {};
  for (const p of models[model]?.asyncPrices ?? []) price[p.unit] = p.price;
  return round(items.reduce((sum: number, it) => sum + ("texts" in it ? (price.text ?? 0) * it.texts.length : (price.image ?? 0)), 0));
}

/** Worst-case list price of one call (the encoding fee is always counted). */
export function estimate(models: Record<string, Json>, path: string, body: Json, model: string | undefined): number {
  const price: Record<string, number> = {};
  for (const p of models[model ?? ""]?.prices ?? []) price[p.unit] = p.price;
  const unit = (u: string, n = 1): number => (price[u] ?? 0) * n;
  if (path === "/v1/segment") {
    const prompts: Json[] = body.prompts ?? [];
    const texts = prompts.filter((p) => "text" in p).length;
    const boxes = prompts.filter((p) => "box" in p).length;
    const points = prompts.filter((p) => "point" in p).length;
    if (model === "sam-lite") return round(unit("encode_image") + unit("box", boxes) + unit("point", points));
    return round(unit("encode_image") + unit("prompt", texts || (boxes ? 1 : 0)) + unit("point", points));
  }
  if (path === "/v1/refine") return round(unit("encode_image") + unit("refine"));
  if (path === "/v1/embeddings") return round("texts" in body ? unit("text", body.texts.length) : unit("image"));
  return round(path === "/v1/documents/parse" ? unit("page") : unit("image"));
}
