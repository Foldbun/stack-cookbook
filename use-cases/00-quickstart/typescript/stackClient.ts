// Copied from clients/typescript/ in github.com/Foldbun/stack-cookbook. Edit the original there.
/**
 * Minimal client for the Foldbun Stack API (Node 22+ built-ins only: fetch, crypto).
 *
 * Semantics shared with the Python and Go client files: API key from FOLDBUN_API_KEY, base URL from
 * FOLDBUN_API_BASE, retries only for `retryable` errors, an idempotency key per call that is reused on retry,
 * a per-pool token bucket, a concurrency cap, a credit cap (FOLDBUN_MAX_CREDITS) and a ledger of every call.
 */
import { randomUUID } from "node:crypto";

export const VERSION = "1.0.0";
export const DEFAULT_BASE = "https://api.stack.foldbun.com";
export const USER_AGENT = `foldbun-stack-cookbook/${VERSION} (typescript; +https://github.com/Foldbun/stack-cookbook)`;
export const MISSING_KEY =
  'FOLDBUN_API_KEY is not set. See "API keys" in the README; every use case page shows the output and cost of a real run, so it can be read without a key.';
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

export class MissingApiKey extends StackError {
  constructor() {
    super(0, "missing_api_key", MISSING_KEY);
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

/** Uploaded image input: reuse one upload across calls and models; required in batches. */
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

export interface ClientOptions {
  apiKey?: string;
  baseUrl?: string;
  maxConcurrency?: number;
  maxCredits?: number;
  throttle?: boolean;
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

export class Bucket {
  private readonly rate: number;
  private readonly cap: number;
  private tokens: number;
  private t = performance.now();
  constructor(perMinute: number) {
    this.rate = perMinute / 60;
    this.cap = this.tokens = perMinute;
  }
  async take(): Promise<void> {
    for (;;) {
      const now = performance.now();
      this.tokens = Math.min(this.cap, this.tokens + ((now - this.t) / 1000) * this.rate);
      this.t = now;
      if (this.tokens >= 1) {
        this.tokens -= 1;
        return;
      }
      await sleep(((1 - this.tokens) / this.rate) * 1000);
    }
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
  readonly apiKey: string;
  readonly maxCredits: number | null;
  readonly throttle: boolean;
  readonly ledger: Array<[string | undefined, LedgerRow]> = []; // one row per successful inference call; see calls()
  private readonly base: string;
  private readonly sem: Semaphore;
  private modelsPromise: Promise<Record<string, Json>> | null = null;
  private buckets = new Map<string, Bucket>();
  private uploads = new Map<string, { data: Uint8Array; contentType: string }>();
  private spent = 0;
  private reserved = 0;

  constructor(opts: ClientOptions = {}) {
    const key = opts.apiKey ?? process.env.FOLDBUN_API_KEY;
    if (!key) throw new MissingApiKey();
    this.apiKey = key;
    this.base = (opts.baseUrl || process.env.FOLDBUN_API_BASE || DEFAULT_BASE).replace(/\/+$/, "");
    const cap = opts.maxCredits ?? process.env.FOLDBUN_MAX_CREDITS;
    this.maxCredits = cap === undefined || cap === "" ? null : Number(cap);
    this.throttle = opts.throttle ?? true;
    this.sem = new Semaphore(opts.maxConcurrency ?? 4);
  }

  // #region http
  private async request(method: string, path: string, body: Uint8Array | string | undefined, headers: Record<string, string>, timeout: Timeout, stream: boolean): Promise<Reply> {
    const ctl = new AbortController();
    const h = { Authorization: `Bearer ${this.apiKey}`, "User-Agent": USER_AGENT, Accept: "application/json", ...headers };
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

  /** Send with retries: `retryable` API errors always, network errors only when retryNetwork. */
  private async send(method: string, path: string, body?: Uint8Array | string, headers: Record<string, string> = {}, timeout: Timeout = T_CALL, retryNetwork = true, stream = false): Promise<Reply> {
    for (let attempt = 0; ; attempt++) {
      let reply: Reply;
      try {
        reply = await this.request(method, path, body, headers, timeout, stream);
      } catch (exc) {
        const detail = exc instanceof Error ? (exc.cause instanceof Error ? exc.cause.message : exc.message) : String(exc);
        if (!retryNetwork || attempt === MAX_RETRIES) throw new StackError(0, "network_error", detail, true);
        await sleep(Math.min(2 ** attempt, MAX_WAIT_S) * 1000);
        continue;
      }
      if (reply.status < 300) return reply;
      const err = StackClient.error(reply);
      if (!err.retryable || attempt === MAX_RETRIES) throw err;
      const header = reply.headers.get("Retry-After");
      const waitMs = Math.max(err.retryAfterMs, header && /^\d+$/.test(header) ? Number(header) * 1000 : 0, 1000);
      await sleep(Math.min(waitMs / 1000, MAX_WAIT_S) * 1000);
    }
  }

  private static error(reply: Reply): StackError {
    const rid = reply.headers.get("X-Request-Id");
    const text = new TextDecoder().decode(reply.raw);
    try {
      const e = JSON.parse(text).error;
      return new StackError(reply.status, e.code, e.message, e.retryable ?? false, e.retryAfterMs ?? 0, rid);
    } catch {
      return new StackError(reply.status, "http_error", text.slice(0, 200), reply.status >= 500, 0, rid);
    }
  }
  // #endregion

  async get(path: string): Promise<Json> {
    const { raw } = await this.send("GET", path, undefined, {}, T_MODELS);
    return JSON.parse(new TextDecoder().decode(raw));
  }

  /** `GET /v1/models` once, keyed by model id. Free. */
  models(): Promise<Record<string, Json>> {
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
  /** POST /v1/images with the raw bytes. Free; returns {id, width, height, bytes, contentType}. */
  async upload(data: Uint8Array, contentType = "image/jpeg"): Promise<Json> {
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
    const worst = estimate(await this.models(), path, body, model);
    this.reserve(worst);
    const headers: Record<string, string> = { "Content-Type": "application/json", "Idempotency-Key": opts.idempotencyKey ?? randomUUID() };
    const data = JSON.stringify(body);
    const timeout = opts.timeout ?? T_CALL;
    let reply: Reply;
    try {
      await this.sem.acquire();
      try {
        await this.take(model);
        try {
          reply = await this.send("POST", path, data, headers, timeout);
        } catch (err) {
          const handle: string | undefined = body.image?.id;
          const saved = handle === undefined ? undefined : this.uploads.get(handle);
          if (!(err instanceof StackError) || err.code !== "image_not_found" || !saved) throw err;
          // Evicted: upload again (same handle) and repeat once. A 404 is stored under the idempotency
          // key and was not charged, so the repeat needs a new key.
          await this.upload(saved.data, saved.contentType);
          headers["Idempotency-Key"] = randomUUID();
          reply = await this.send("POST", path, data, headers, timeout);
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
  /** POST /v1/batch/{endpoint} as Server-Sent Events. Yields [event, data] for progress, item and summary. */
  async *batch(endpoint: string, body: Json, opts: CallOptions = {}): AsyncGenerator<[string, Json]> {
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
        await this.take(model);
        const { stream } = await this.send("POST", path, JSON.stringify(body), headers, T_BATCH, true, true);
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

  // #region ledger
  private record(endpoint: string, result: Json, replayed: boolean, tag?: string): void {
    const billing = result.billing ?? {};
    const row: LedgerRow = {
      endpoint,
      model: result.model ?? null,
      list: billing.total ?? result.cost ?? 0,
      charged: replayed ? 0 : (result.cost ?? 0),
      cache: result.cache ?? null,
      queueMs: result.timing?.queueMs ?? 0,
      processingMs: result.timing?.processingMs ?? 0,
      replayed,
    };
    this.ledger.push([tag, row]);
    if (!replayed) this.spent += row.list;
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

  private async take(model: string | undefined): Promise<void> {
    const info = (await this.models())[model ?? ""] ?? {};
    const perMinute: number | undefined = info.perMinute;
    if (!this.throttle || !perMinute) return;
    const pool: string = info.pool || model;
    let bucket = this.buckets.get(pool);
    if (!bucket) this.buckets.set(pool, (bucket = new Bucket(perMinute)));
    await bucket.take();
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
