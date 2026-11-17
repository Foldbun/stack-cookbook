// Backend of the click-to-segment component (it holds the API key, if there is one). The HTTP contract is contract/02-backend.yaml; the browser
// talks only to this backend, and only this backend talks to the Foldbun Stack API.
import { createHash, randomUUID } from "node:crypto";
import { lstat, readFile } from "node:fs/promises";
import type { IncomingMessage, ServerResponse } from "node:http";
import { extname, join } from "node:path";

import * as sc from "./stackClient.ts";

/** The part of StackClient the backend uses (the tests pass a stand-in). */
export interface Stack {
  /** True without an API key: the free tier, which has no uploads. */
  readonly free?: boolean;
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
  freeImageBytes: 4 * 1024 * 1024, // the largest image the free tier takes inline
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

// What the backend says when the free tier refuses for good: its own text, never Stack's.
const FREE_REFUSED: Record<string, string> = {
  daily_limit_reached: "the daily limit of the free tier is used up (it resets at 00:00 UTC); start the backend with FOLDBUN_API_KEY to continue now",
  free_tier_unavailable: "the free tier is not available; start the backend with FOLDBUN_API_KEY",
};
const RETRY_CODES = new Set(["rate_limited", "free_busy"]); // the free tier's waits

/** Map an error of the client file (after its own retries) to the backend's error. Stack's message is not forwarded. */
export function fromStack(err: unknown): ApiError {
  if (err instanceof sc.BudgetExceeded) return new ApiError(503, "credit_cap", "the backend's credit cap is reached");
  if (err instanceof sc.MissingApiKey) {
    // a call that only a key can make
    return new ApiError(502, "upstream_refused", "this needs an API key: start the backend with FOLDBUN_API_KEY", false, 0, err.code);
  }
  if (err instanceof sc.StackError && !err.retryable && !RETRY_CODES.has(err.code)) {
    return new ApiError(502, "upstream_refused", FREE_REFUSED[err.code] ?? "Foldbun Stack refused the call", false, 0, err.code, err.requestId);
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
  handle: string | null; // the Stack handle; null in free mode, which has no uploads
  width: number;
  height: number;
  data?: Uint8Array; // free mode keeps the bytes and sends them inline with every click
}

// #region image-size
/**
 * [width, height] of a PNG, JPEG or WebP image as the API counts them (JPEG: EXIF orientation applied), else null.
 * Free mode has no upload that answers with the size, and the points are checked against it.
 */
export function imageSize(data: Uint8Array): [number, number] | null {
  const b = Buffer.from(data.buffer, data.byteOffset, data.byteLength);
  let w = 0;
  let h = 0;
  if (b.length >= 24 && b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    w = b.readUInt32BE(16);
    h = b.readUInt32BE(20);
  } else if (b.length >= 30 && b.toString("latin1", 0, 4) === "RIFF" && b.toString("latin1", 8, 12) === "WEBP") {
    const kind = b.toString("latin1", 12, 16);
    if (kind === "VP8 ") {
      w = b.readUInt16LE(26) & 0x3fff;
      h = b.readUInt16LE(28) & 0x3fff;
    } else if (kind === "VP8L") {
      const bits = b.readUInt32LE(21);
      w = (bits & 0x3fff) + 1;
      h = ((bits >>> 14) & 0x3fff) + 1;
    } else if (kind === "VP8X") {
      w = 1 + b.readUIntLE(24, 3);
      h = 1 + b.readUIntLE(27, 3);
    }
  } else if (b.length >= 2 && b[0] === 0xff && b[1] === 0xd8) {
    const [fw, fh, turned] = jpegSize(b);
    [w, h] = turned ? [fh, fw] : [fw, fh];
  }
  return w > 0 && h > 0 ? [w, h] : null;
}

const JPEG_FRAMES = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);

/** Walk the marker segments: the size from the frame header, and whether the EXIF orientation (5 to 8) turns the image. */
function jpegSize(b: Buffer): [number, number, boolean] {
  let w = 0;
  let h = 0;
  let turned = false;
  let i = 2;
  while (i + 4 <= b.length && b[i] === 0xff) {
    const marker = b[i + 1];
    if (marker === 0xff) {
      i += 1; // fill byte
      continue;
    }
    if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd8)) {
      i += 2; // markers without a length
      continue;
    }
    const length = b.readUInt16BE(i + 2);
    const body = b.subarray(i + 4, i + 2 + length);
    if (marker === 0xe1 && body.toString("latin1", 0, 6) === "Exif\0\0") turned = exifTurns(body.subarray(6));
    if (JPEG_FRAMES.has(marker) && body.length >= 5) {
      h = body.readUInt16BE(1);
      w = body.readUInt16BE(3);
      break;
    }
    if (marker === 0xda) break; // the scan starts: no frame header came first
    i += 2 + length;
  }
  return [w, h, turned];
}

/** True when the orientation tag of the first IFD turns the image by a quarter (values 5 to 8). */
function exifTurns(tiff: Buffer): boolean {
  if (tiff.length < 8) return false;
  const order = tiff.toString("latin1", 0, 2);
  if (order !== "II" && order !== "MM") return false;
  const le = order === "II";
  const u16 = (at: number) => (le ? tiff.readUInt16LE(at) : tiff.readUInt16BE(at));
  const offset = le ? tiff.readUInt32LE(4) : tiff.readUInt32BE(4);
  if (offset + 2 > tiff.length) return false;
  const count = u16(offset);
  for (let k = 0; k < count; k++) {
    const at = offset + 2 + 12 * k;
    if (at + 12 > tiff.length) return false;
    if (u16(at) === 0x0112) return [5, 6, 7, 8].includes(u16(at + 8));
  }
  return false;
}
// #endregion

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
  private readonly free: boolean;

  constructor(stack: Stack, opts: BackendOptions = {}) {
    this.stack = stack;
    this.models = opts.models?.length ? opts.models : ["sam-lite"];
    this.now = opts.now ?? Date.now;
    this.log = opts.log ?? ((line) => console.log(line));
    this.staticDir = opts.staticDir;
    this.free = Boolean(stack.free); // the client decides: no key, no uploads
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
    let size: [number, number] | null = null;
    if (this.free) {
      // no upload will answer with the size, so read it from the bytes
      if (data.length > LIMITS.freeImageBytes) {
        throw new ApiError(413, "payload_too_large", `at most ${LIMITS.freeImageBytes} bytes without an API key (the free tier takes images up to 4 MiB)`);
      }
      size = imageSize(data);
      if (!size) throw invalid("the body is not a readable jpeg, png or webp image");
    }
    const imageId = "im_" + createHash("sha256").update(data).digest("hex").slice(0, 32);
    let pending = this.images.get(imageId);
    if (pending) {
      this.images.delete(imageId); // most recently used goes last
    } else {
      // With a key: one Stack upload per distinct content; the client file keeps the bytes and uploads again if Stack
      // evicts them. Free tier: there are no uploads; this cache keeps the bytes and every click sends them inline.
      pending = size
        ? Promise.resolve({ handle: null, width: size[0], height: size[1], data })
        : this.stack.upload(data, type).then((u) => ({ handle: u.id, width: u.width, height: u.height }));
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
    const image = img.handle === null ? sc.imageBase64(img.data!) : sc.imageHandle(img.handle);
    const call: sc.Json = { model, image, points };
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
