// Key-holding backend of the click-to-segment component. The HTTP contract is contract/02-backend.yaml; the browser
// talks only to this backend, and only this backend talks to the Foldbun Stack API.
import { createHash, randomUUID } from "node:crypto";
import { lstat, readFile } from "node:fs/promises";
import type { IncomingMessage, ServerResponse } from "node:http";
import { extname, join } from "node:path";

import * as sc from "./stackClient.ts";

/** The part of StackClient the backend uses (the tests pass a stand-in). */
export interface Stack {
  upload(data: Uint8Array, contentType?: string): Promise<sc.Json>;
  post(path: string, body: sc.Json, opts?: sc.CallOptions): Promise<sc.Json>;
  calls(tag?: string): sc.LedgerRow[];
}

export interface BackendOptions {
  /** Models a request may name (`SEGMENTER_MODELS`); the first is the default. */
  models?: string[];
  /** Milliseconds clock, replaceable in tests. */
  now?: () => number;
  log?: (line: string) => void;
  /** Directory whose files `GET /` and `GET /<name>` serve (the built web component). Without it every such path is 404. */
  staticDir?: string;
}

export const LANGUAGE = "typescript";
export const LIMITS = {
  imageBytes: 20 * 1024 * 1024, // the Stack upload limit
  refineBytes: 64 * 1024,
  points: 20, // the Stack limit of POST /v1/refine
  images: 256,
  sessions: 1000,
  idleMs: 30 * 60_000,
};
const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
};
const SESSION_ID = /^[A-Za-z0-9_-]{1,64}$/;
const IMAGE_ID = /^im_[0-9a-f]{32}$/;

// #region errors
/** A backend error, sent as {"error": {code, message, retryable, retryAfterMs, upstreamCode?, requestId?}}. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly retryAfterMs: number;
  readonly upstreamCode: string | undefined;
  readonly requestId: string | undefined;
  readonly headers: Record<string, string> = {}; // extra response headers, e.g. Allow with a 405

  constructor(status: number, code: string, message: string, retryable = false, retryAfterMs = 0, upstreamCode?: string, requestId?: string | null) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.retryAfterMs = retryAfterMs;
    this.upstreamCode = upstreamCode;
    this.requestId = requestId ?? undefined;
  }

  body(): sc.Json {
    const { code, message, retryable, retryAfterMs, upstreamCode, requestId } = this;
    return { error: { code, message, retryable, retryAfterMs, ...(upstreamCode ? { upstreamCode } : {}), ...(requestId ? { requestId } : {}) } };
  }
}

/** Map an error of the client file (after its own retries) to the backend's error. Stack's message is not forwarded. */
export function fromStack(err: unknown): ApiError {
  if (err instanceof sc.BudgetExceeded) return new ApiError(503, "credit_cap", "the backend's credit cap is reached");
  if (err instanceof sc.StackError && !err.retryable) {
    return new ApiError(502, "upstream_refused", "Foldbun Stack refused the call", false, 0, err.code, err.requestId);
  }
  if (!(err instanceof sc.StackError)) {
    const internal = new ApiError(500, "internal", "internal error"); // not an answer or a network failure: a bug, no detail for the caller
    internal.cause = err;
    return internal;
  }
  return new ApiError(503, "upstream_busy", "Foldbun Stack is busy; try again", true, Math.max(1000, err.retryAfterMs), err.code, err.requestId);
}
// #endregion

// #region session-limit
/** Token bucket: `perMinute` sustained, `burst` at once. take() returns 0 when allowed, else the wait in ms. */
export class RateLimit {
  private tokens: number;
  private at: number;
  private readonly perMinute: number;
  private readonly burst: number;

  constructor(perMinute: number, burst: number, now: number) {
    this.perMinute = perMinute;
    this.burst = burst;
    this.tokens = burst;
    this.at = now;
  }

  take(now: number): number {
    this.tokens = Math.min(this.burst, this.tokens + ((now - this.at) * this.perMinute) / 60_000);
    this.at = now;
    if (this.tokens >= 1) {
      this.tokens -= 1;
      return 0;
    }
    return Math.ceil(((1 - this.tokens) * 60_000) / this.perMinute);
  }
}

interface Session {
  id: string;
  tag: string; // ledger tag of this session's calls in the client file
  seen: number;
  uploads: number;
  clicks: number;
  refine: RateLimit;
  upload: RateLimit;
}
// #endregion

interface Image {
  handle: string;
  width: number;
  height: number;
}

interface Point {
  x: number;
  y: number;
  positive: boolean;
}

export class Backend {
  private readonly stack: Stack;
  private readonly models: string[];
  private readonly now: () => number;
  private readonly log: (line: string) => void;
  private readonly staticDir: string | undefined;
  private readonly sessions = new Map<string, Session>(); // least recently used first
  private readonly images = new Map<string, Promise<Image>>(); // least recently used first

  constructor(stack: Stack, opts: BackendOptions = {}) {
    this.stack = stack;
    this.models = opts.models?.length ? opts.models : ["sam-lite"];
    this.now = opts.now ?? Date.now;
    this.log = opts.log ?? ((line) => console.log(line));
    this.staticDir = opts.staticDir;
  }

  /** The request listener for http.createServer. */
  handle = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const t = performance.now();
    let status = 200;
    let body: sc.Json;
    try {
      const path = (req.url ?? "/").split("?")[0];
      if (path !== "/api" && !path.startsWith("/api/")) {
        const file = await this.staticFile(req, path);
        res
          .writeHead(200, { "Content-Type": file.type, "Content-Length": file.data.length, "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff" })
          .end(req.method === "HEAD" ? undefined : file.data);
        this.log(`${req.method} ${req.url} 200 ${Math.round(performance.now() - t)}ms`);
        return;
      }
      body = await this.route(req);
    } catch (err) {
      const e = err instanceof ApiError ? err : new ApiError(500, "internal", "internal error");
      if (e.code === "internal") {
        const cause = err instanceof ApiError ? e.cause : err;
        this.log(`internal error: ${cause instanceof Error ? cause.stack : String(cause)}`);
      }
      status = e.status;
      body = e.body();
      if (e.retryAfterMs > 0) res.setHeader("Retry-After", String(Math.ceil(e.retryAfterMs / 1000)));
      for (const [name, value] of Object.entries(e.headers)) res.setHeader(name, value);
      req.resume(); // drop an unread body
    }
    res.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(body));
    this.log(`${req.method} ${req.url} ${status} ${Math.round(performance.now() - t)}ms`);
  };

  // #region static
  /**
   * The file for a request outside `/api`: `GET` and `HEAD` serve `/` (index.html) or `/<name>`, a regular file directly
   * inside the static directory (no sub-directories, no `..`, no symbolic links); any other method is `405` with
   * `Allow: GET, HEAD`; a name that is not served is `404 not_found`.
   */
  private async staticFile(req: IncomingMessage, path: string): Promise<{ data: Buffer; type: string }> {
    if (req.method !== "GET" && req.method !== "HEAD") {
      const err = new ApiError(405, "method_not_allowed", "use GET or HEAD");
      err.headers["Allow"] = "GET, HEAD";
      throw err;
    }
    const notFound = new ApiError(404, "not_found", `no route ${path}`);
    if (!this.staticDir) throw notFound;
    let name: string;
    try {
      name = path === "/" ? "index.html" : decodeURIComponent(path.slice(1));
    } catch {
      throw notFound; // malformed percent escape
    }
    if (!name || name.startsWith(".") || /[/\\\0]/.test(name)) throw notFound;
    const file = join(this.staticDir, name);
    try {
      if (!(await lstat(file)).isFile()) throw notFound;
      return { data: await readFile(file), type: CONTENT_TYPES[extname(name).toLowerCase()] ?? "application/octet-stream" };
    } catch {
      throw notFound;
    }
  }
  // #endregion

  private async route(req: IncomingMessage): Promise<sc.Json> {
    const path = (req.url ?? "/").split("?")[0];
    const routes: Record<string, [string, (req: IncomingMessage, s: Session) => Promise<sc.Json>]> = {
      "/api/images": ["POST", (r, s) => this.uploadImage(r, s)],
      "/api/refine": ["POST", (r, s) => this.refine(r, s)],
      "/api/usage": ["GET", async (_r, s) => this.usage(s)],
    };
    if (path === "/api/health") {
      if (req.method !== "GET") throw new ApiError(405, "method_not_allowed", "use GET");
      return { status: "ok", language: LANGUAGE, client: `stackClient ${sc.VERSION}` };
    }
    const route = routes[path];
    if (!route) throw new ApiError(404, "not_found", `no route ${path}`);
    if (req.method !== route[0]) throw new ApiError(405, "method_not_allowed", `use ${route[0]}`);
    return route[1](req, this.session(req.headers["x-session-id"]));
  }

  // #region sessions
  private session(header: string | string[] | undefined): Session {
    if (typeof header !== "string" || !SESSION_ID.test(header)) {
      throw new ApiError(400, "invalid_session", "send X-Session-Id: 1 to 64 characters from A-Z, a-z, 0-9, _ and -");
    }
    const now = this.now();
    for (const [id, s] of this.sessions) {
      if (now - s.seen <= LIMITS.idleMs) break; // the map is in order of use: the rest were used more recently
      this.sessions.delete(id); // idle for too long
    }
    let s = this.sessions.get(header);
    if (s) this.sessions.delete(header);
    else s = { id: header, tag: randomUUID(), seen: now, uploads: 0, clicks: 0, refine: new RateLimit(60, 10, now), upload: new RateLimit(10, 5, now) };
    s.seen = now;
    this.sessions.set(header, s); // the session of this request is now the most recently used
    while (this.sessions.size > LIMITS.sessions) this.sessions.delete(this.sessions.keys().next().value!); // more than 1,000: the one idle the longest goes, never this one
    return s;
  }

  private limit(bucket: RateLimit): void {
    const wait = bucket.take(this.now());
    if (wait) throw new ApiError(429, "rate_limited", "too many requests in this session", true, wait);
  }
  // #endregion

  // #region upload-once
  private async uploadImage(req: IncomingMessage, s: Session): Promise<sc.Json> {
    this.limit(s.upload);
    const type = (req.headers["content-type"] ?? "").split(";")[0].trim().toLowerCase();
    if (!IMAGE_TYPES.has(type)) throw new ApiError(415, "unsupported_media_type", "send the raw bytes as image/jpeg, image/png or image/webp");
    const data = await readBody(req, LIMITS.imageBytes);
    if (data.length === 0) throw invalid("the body is empty: send the raw bytes of the image");
    const imageId = "im_" + createHash("sha256").update(data).digest("hex").slice(0, 32);
    let pending = this.images.get(imageId);
    if (pending) {
      this.images.delete(imageId); // most recently used goes last
    } else {
      // one Stack upload per distinct content; the client file keeps the bytes and uploads again if Stack evicts them
      pending = this.stack.upload(data, type).then((u) => ({ handle: u.id, width: u.width, height: u.height }));
      pending.catch(() => this.images.delete(imageId));
    }
    this.images.set(imageId, pending);
    while (this.images.size > LIMITS.images) this.images.delete(this.images.keys().next().value!);
    let img: Image;
    try {
      img = await pending;
    } catch (err) {
      throw fromStack(err);
    }
    s.uploads++;
    return { imageId, width: img.width, height: img.height };
  }
  // #endregion

  // #region refine
  private async refine(req: IncomingMessage, s: Session): Promise<sc.Json> {
    this.limit(s.refine);
    const raw = await readBody(req, LIMITS.refineBytes);
    let body: sc.Json;
    try {
      body = JSON.parse(new TextDecoder().decode(raw));
    } catch {
      throw invalid("the body is not JSON");
    }
    const { imageId, points, box, model } = parseRefine(body, this.models);
    const pending = this.images.get(imageId);
    if (!pending) throw new ApiError(404, "image_not_found", "unknown imageId: upload the image again");
    this.images.delete(imageId); // a refine is a use of the image: most recently used goes last
    this.images.set(imageId, pending);
    const img = await pending.catch(() => {
      throw new ApiError(404, "image_not_found", "unknown imageId: upload the image again");
    });
    checkBounds(points, box, img);
    // Stateless: every click sends all points of the object. The body is the same in every language.
    const call: sc.Json = { model, image: sc.imageHandle(img.handle), points };
    if (box) call.box = box;
    const t = performance.now();
    let result: sc.Json;
    try {
      result = await this.stack.post("/v1/refine", call, { tag: s.tag });
    } catch (err) {
      if (err instanceof sc.StackError) this.log(`stack error: ${err.describe()}`);
      throw fromStack(err);
    }
    const latencyMs = Math.round(performance.now() - t);
    s.clicks++;
    // Stack returns the outline as a simplified polygon with holes by default: no mask decoding needed here.
    const inst = result.instances?.[0] ?? {};
    return {
      polygon: inst.polygon ?? [],
      bbox: inst.bbox ?? [0, 0, 0, 0],
      area: inst.area ?? 0,
      score: inst.score ?? 0,
      model: result.model ?? model,
      costCredits: result.cost ?? 0,
      listCredits: result.billing?.total ?? 0,
      cache: result.cache === "hit" ? "hit" : "miss",
      latencyMs,
    };
  }
  // #endregion

  // #region usage
  private usage(s: Session): sc.Json {
    const calls = this.stack.calls(s.tag);
    const billed = calls.filter((c) => !c.replayed);
    const sum = (f: (c: sc.LedgerRow) => number) => Math.round(billed.reduce((a, c) => a + f(c), 0) * 1e6) / 1e6;
    return {
      sessionId: s.id,
      uploads: s.uploads,
      clicks: s.clicks,
      costCredits: sum((c) => c.charged),
      listCredits: sum((c) => c.list),
      cacheHits: calls.filter((c) => c.cache === "hit").length,
      cacheMisses: calls.filter((c) => c.cache === "miss").length,
      calls,
    };
  }
  // #endregion
}

const invalid = (message: string) => new ApiError(400, "invalid_request", message);
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Validate the body of POST /api/refine (shape only; bounds need the image size). */
export function parseRefine(body: sc.Json, models: string[]): { imageId: string; points: Point[]; box?: number[]; model: string } {
  if (typeof body !== "object" || body === null || Array.isArray(body)) throw invalid("the body must be a JSON object");
  const extra = Object.keys(body).filter((k) => !["imageId", "points", "box", "model"].includes(k));
  if (extra.length) throw invalid(`unknown field ${extra[0]}`);
  if (typeof body.imageId !== "string" || !IMAGE_ID.test(body.imageId)) throw invalid("imageId is missing or malformed");
  if (!Array.isArray(body.points) || body.points.length > LIMITS.points) throw invalid(`points must be a list of at most ${LIMITS.points}`);
  const points = body.points.map((p: sc.Json): Point => {
    if (typeof p !== "object" || p === null || !finite(p.x) || !finite(p.y) || (p.positive !== undefined && typeof p.positive !== "boolean")) {
      throw invalid("each point is {x, y, positive?} with numbers x and y");
    }
    if (Object.keys(p).some((k) => !["x", "y", "positive"].includes(k))) throw invalid("each point is {x, y, positive?}");
    return { x: p.x, y: p.y, positive: p.positive ?? true };
  });
  let box: number[] | undefined;
  if (body.box !== undefined) {
    if (!Array.isArray(body.box) || body.box.length !== 4 || !body.box.every(finite)) throw invalid("box is [x1, y1, x2, y2]");
    box = body.box;
  }
  if (!box && !points.some((p: Point) => p.positive)) throw invalid("give at least one positive point or a box");
  const model = body.model ?? models[0];
  if (!models.includes(model)) throw new ApiError(400, "model_not_allowed", `this backend serves ${models.join(", ")}`);
  return { imageId: body.imageId, points, box, model };
}

/** Points in [0, width] x [0, height]; box with 0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height. */
export function checkBounds(points: Point[], box: number[] | undefined, img: { width: number; height: number }): void {
  for (const p of points) {
    if (p.x < 0 || p.y < 0 || p.x > img.width || p.y > img.height) throw invalid(`point (${p.x}, ${p.y}) is outside the ${img.width}x${img.height} image`);
  }
  if (box && !(box[0] >= 0 && box[1] >= 0 && box[0] < box[2] && box[1] < box[3] && box[2] <= img.width && box[3] <= img.height)) {
    throw invalid(`box is not inside the ${img.width}x${img.height} image`);
  }
}

/** The request body, refused with 413 above `limit` bytes. */
async function readBody(req: IncomingMessage, limit: number): Promise<Uint8Array> {
  if (Number(req.headers["content-length"] ?? 0) > limit) throw new ApiError(413, "payload_too_large", `at most ${limit} bytes`);
  const chunks: Buffer[] = [];
  let n = 0;
  for await (const chunk of req) {
    n += chunk.length; // past the limit, read on without keeping, so the answer can still be sent
    if (n <= limit) chunks.push(chunk);
  }
  if (n > limit) throw new ApiError(413, "payload_too_large", `at most ${limit} bytes`);
  return Buffer.concat(chunks);
}
