// node --test: the backend against a stand-in for the Stack client (no network, no key).
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { createServer, request, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

import { Backend, RateLimit, fromStack, type Stack } from "./backend.ts";
import * as sc from "./stackClient.ts";

class FakeStack implements Stack {
  uploads = 0;
  bodies: sc.Json[] = [];
  fail: Error | null = null;
  private rows: Array<[string | undefined, sc.LedgerRow]> = [];

  async upload(): Promise<sc.Json> {
    this.uploads++;
    return { id: "img_" + "a".repeat(64), width: 100, height: 80 };
  }

  async post(path: string, body: sc.Json, opts: sc.CallOptions = {}): Promise<sc.Json> {
    if (this.fail) throw this.fail;
    this.bodies.push(body);
    const cache = this.bodies.length === 1 ? "miss" : "hit";
    const list = cache === "miss" ? 0.21 : 0.01;
    this.rows.push([opts.tag, { endpoint: path, model: body.model, list, charged: 0, cache, queueMs: 4, processingMs: 60, replayed: false }]);
    const polygon = [{ outer: [10, 10, 50, 10, 50, 40, 10, 40] }];
    return { model: body.model, cost: 0, cache, billing: { total: list }, instances: [{ score: 0.9, bbox: [10, 10, 51, 41], area: 1271, polygon }] };
  }

  calls(tag?: string): sc.LedgerRow[] {
    return this.rows.filter(([t]) => tag === undefined || t === tag).map(([, r]) => r);
  }
}

const stack = new FakeStack();
let clock = 0;
let server: Server;
let base = "";

before(async () => {
  server = createServer(new Backend(stack, { now: () => clock, log: () => {} }).handle);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => server.close());

async function api(path: string, session: string | null, init: RequestInit = {}): Promise<[number, sc.Json, Headers]> {
  const headers = new Headers(init.headers);
  if (session) headers.set("X-Session-Id", session);
  const res = await fetch(base + path, { ...init, headers });
  return [res.status, await res.json(), res.headers];
}

const jpeg = (salt: number) => new Uint8Array([0xff, 0xd8, 0xff, salt]);
const upload = (session: string, salt = 1) => api("/api/images", session, { method: "POST", body: jpeg(salt), headers: { "Content-Type": "image/jpeg" } });
const refine = (session: string, body: sc.Json) => api("/api/refine", session, { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });

test("health needs no session", async () => {
  const [status, body] = await api("/api/health", null);
  assert.equal(status, 200);
  assert.deepEqual(body, { status: "ok", language: "typescript", client: `stackClient ${sc.VERSION}` });
});

test("every other route needs a well-formed X-Session-Id", async () => {
  assert.equal((await api("/api/usage", null))[1].error.code, "invalid_session");
  assert.equal((await api("/api/usage", "bad id!"))[1].error.code, "invalid_session");
  assert.equal((await api("/api/nothing", "s"))[1].error.code, "not_found");
  assert.equal((await api("/api/refine", "s"))[0], 405);
});

test("an image is uploaded to Stack once per content, in any session", async () => {
  const [s1, a] = await upload("up-1");
  const [s2, b] = await upload("up-2");
  assert.equal(s1, 200);
  assert.equal(s2, 200);
  assert.deepEqual(a, b);
  assert.match(a.imageId, /^im_[0-9a-f]{32}$/);
  assert.deepEqual([a.width, a.height], [100, 80]);
  assert.equal(stack.uploads, 1);
  const [s3, err] = await api("/api/images", "up-1", { method: "POST", body: "x", headers: { "Content-Type": "text/plain" } });
  assert.equal(s3, 415);
  assert.equal(err.error.code, "unsupported_media_type");
});

test("refine sends all points with explicit positive and returns the polygon, cost and usage", async () => {
  const [, img] = await upload("click", 2);
  stack.bodies.length = 0;
  const points = [{ x: 20, y: 20 }, { x: 70, y: 60, positive: false }];
  const [status, r] = await refine("click", { imageId: img.imageId, points });
  assert.equal(status, 200);
  assert.deepEqual(stack.bodies[0], {
    model: "sam-lite",
    image: { id: "img_" + "a".repeat(64) },
    points: [
      { x: 20, y: 20, positive: true },
      { x: 70, y: 60, positive: false },
    ],
  });
  assert.deepEqual(r.polygon, [{ outer: [10, 10, 50, 10, 50, 40, 10, 40] }]);
  assert.equal(r.cache, "miss");
  assert.equal(r.listCredits, 0.21);
  assert.ok(Number.isInteger(r.latencyMs));
  await refine("click", { imageId: img.imageId, points });
  const [, usage] = await api("/api/usage", "click");
  assert.equal(usage.clicks, 2);
  assert.equal(usage.uploads, 1);
  assert.equal(usage.listCredits, 0.22);
  assert.deepEqual([usage.cacheHits, usage.cacheMisses], [1, 1]);
  assert.equal(usage.calls.length, 2);
  assert.equal((await api("/api/usage", "fresh"))[1].calls.length, 0);
});

test("refine checks the request before calling Stack", async () => {
  const [, img] = await upload("check", 3);
  const cases: Array<[sc.Json, string]> = [
    [{ imageId: "im_" + "0".repeat(32), points: [{ x: 1, y: 1 }] }, "image_not_found"],
    [{ imageId: img.imageId, points: [{ x: 1, y: 1, positive: false }] }, "invalid_request"],
    [{ imageId: img.imageId, points: [{ x: 101, y: 1 }] }, "invalid_request"],
    [{ imageId: img.imageId, points: Array.from({ length: 21 }, () => ({ x: 1, y: 1 })) }, "invalid_request"],
    [{ imageId: img.imageId, points: [], box: [10, 10, 5, 20] }, "invalid_request"],
    [{ imageId: img.imageId, points: [{ x: 1, y: 1 }], model: "sam3" }, "model_not_allowed"],
    [{ imageId: img.imageId, points: [{ x: 1, y: 1 }], extra: 1 }, "invalid_request"],
  ];
  const before = stack.bodies.length;
  for (const [body, code] of cases) {
    const [status, r] = await refine("check", body);
    assert.equal(r.error.code, code, JSON.stringify(body));
    assert.equal(status, code === "image_not_found" ? 404 : 400);
  }
  assert.equal(stack.bodies.length, before);
});

test("a session is limited to a burst of 10 refines, with Retry-After", async () => {
  const [, img] = await upload("burst", 4);
  for (let i = 0; i < 10; i++) assert.equal((await refine("burst", { imageId: img.imageId, points: [{ x: 5, y: 5 }] }))[0], 200);
  const [status, r, headers] = await refine("burst", { imageId: img.imageId, points: [{ x: 5, y: 5 }] });
  assert.equal(status, 429);
  assert.equal(r.error.code, "rate_limited");
  assert.equal(r.error.retryAfterMs, 1000);
  assert.equal(headers.get("retry-after"), "1");
  clock += 1000;
  assert.equal((await refine("burst", { imageId: img.imageId, points: [{ x: 5, y: 5 }] }))[0], 200);
});

/** A backend of its own, with its own clock, for tests that look inside the tables. */
async function lonely(): Promise<{ backend: Backend; base: string; clock: { now: number }; close: () => Promise<void> }> {
  const clock = { now: 0 };
  const backend = new Backend(new FakeStack(), { now: () => clock.now, log: () => {} });
  const srv = createServer(backend.handle);
  await new Promise<void>((resolve) => srv.listen(0, "127.0.0.1", resolve));
  return { backend, base: `http://127.0.0.1:${(srv.address() as AddressInfo).port}`, clock, close: () => new Promise<void>((resolve) => srv.close(() => resolve())) };
}
const sessionIds = (b: Backend): string[] => [...(b as unknown as { sessions: Map<string, unknown> }).sessions.keys()];

test("there are at most 1,000 sessions: the one idle the longest goes first, never the one being served", async () => {
  const { backend, base, clock, close } = await lonely();
  try {
    const touch = (id: string) => fetch(`${base}/api/usage`, { headers: { "X-Session-Id": id } }).then((r) => r.json() as Promise<sc.Json>);
    clock.now += 1;
    await fetch(`${base}/api/images`, { method: "POST", body: jpeg(1), headers: { "X-Session-Id": "s0", "Content-Type": "image/jpeg" } }); // s0 has a counter to lose
    for (let i = 1; i < 1000; i++) {
      clock.now += 1;
      await touch(`s${i}`);
    }
    assert.equal(sessionIds(backend).length, 1000, "1,000 sessions are allowed");
    clock.now += 1;
    assert.equal((await touch("s0")).uploads, 1, "the oldest session, being served, is not the one that goes"); // 1,000 sessions: nothing to drop
    assert.equal(sessionIds(backend).length, 1000);
    assert.ok(sessionIds(backend).includes("s0"));
    clock.now += 1;
    await touch("s1000"); // 1,001: the one idle the longest is s1, because s0 was just used
    const ids = sessionIds(backend);
    assert.equal(ids.length, 1000);
    assert.ok(!ids.includes("s1") && ids.includes("s0") && ids.includes("s2") && ids.at(-1) === "s1000");
    // idle for more than 30 minutes ends a session whatever the count
    clock.now += 30 * 60_000 + 1;
    await touch("late");
    assert.deepEqual(sessionIds(backend), ["late"]);
  } finally {
    await close();
  }
});

test("the image cache keeps at most 256 images, and a refine is a use of the image", async () => {
  const { base, close } = await lonely();
  try {
    const call = (path: string, session: string, body: string | Uint8Array, type: string) =>
      fetch(base + path, { method: "POST", body, headers: { "X-Session-Id": session, "Content-Type": type } }).then(async (r) => [r.status, await r.json()] as [number, sc.Json]);
    const up = async (n: number) => (await call("/api/images", `u${n}`, new Uint8Array([0xff, 0xd8, 0xff, n >> 8, n & 255]), "image/jpeg"))[1].imageId as string;
    const click = async (id: string, n: number) => (await call("/api/refine", `r${n}`, JSON.stringify({ imageId: id, points: [{ x: 5, y: 5 }] }), "application/json"))[0];
    const ids: string[] = [];
    for (let n = 0; n < 256; n++) ids.push(await up(n));
    assert.equal(await click(ids[0], 0), 200); // image 0 was the least recently used until now
    ids.push(await up(256)); // 257 images: the least recently used goes, which is image 1
    assert.equal(await click(ids[1], 1), 404);
    assert.equal(await click(ids[0], 2), 200);
    assert.equal(await click(ids[256], 3), 200);
    ids.push(await up(257)); // image 2 goes next
    assert.equal(await click(ids[2], 4), 404);
  } finally {
    await close();
  }
});

test("a rejected request (400, 413, 415) consumes a rate-limit token of its session", async () => {
  const [, img] = await upload("tokens-img", 6);
  const bad = (body: string | Uint8Array) => api("/api/refine", "tokens", { method: "POST", body, headers: { "Content-Type": "application/json" } });
  const statuses: number[] = [];
  for (let i = 0; i < 8; i++) statuses.push((await bad("not json"))[0]);
  statuses.push((await bad("x".repeat(64 * 1024 + 1)))[0]);
  statuses.push((await bad(JSON.stringify({ imageId: img.imageId, points: [] })))[0]);
  assert.deepEqual(statuses, [400, 400, 400, 400, 400, 400, 400, 400, 413, 400]);
  assert.equal((await bad("not json"))[1].error.code, "rate_limited", "the burst of 10 is used up by rejected requests");
  const wrong = (type: string, body: string | Uint8Array) => api("/api/images", "tokens-up", { method: "POST", body, headers: { "Content-Type": type } });
  const ups = [(await wrong("text/plain", "x"))[0], (await wrong("image/png", new Uint8Array(0)))[0], (await wrong("image/png", new Uint8Array(20 * 1024 * 1024 + 1)))[0]];
  ups.push((await wrong("text/plain", "x"))[0], (await wrong("text/plain", "x"))[0]);
  assert.deepEqual(ups, [415, 400, 413, 415, 415]);
  assert.equal((await wrong("text/plain", "x"))[1].error.code, "rate_limited", "the burst of 5 is used up by rejected uploads");
});

test("an empty image body is 400 invalid_request and is not sent to Stack", async () => {
  const before = stack.uploads;
  const [status, r] = await api("/api/images", "empty", { method: "POST", body: new Uint8Array(0), headers: { "Content-Type": "image/jpeg" } });
  assert.deepEqual([status, r.error.code], [400, "invalid_request"]);
  assert.equal(stack.uploads, before);
});

test("model null means not given", async () => {
  const [, img] = await upload("null-model", 7);
  const [status] = await refine("null-model", { imageId: img.imageId, points: [{ x: 5, y: 5 }], model: null });
  assert.equal(status, 200);
  assert.equal(stack.bodies.at(-1)!.model, "sam-lite");
});

test("an unexpected internal error is 500 internal with no detail", async () => {
  const [, img] = await upload("internal", 8);
  stack.fail = new TypeError("secret detail");
  const [status, r] = await refine("internal", { imageId: img.imageId, points: [{ x: 5, y: 5 }] });
  stack.fail = null;
  assert.equal(status, 500);
  assert.deepEqual(r, { error: { code: "internal", message: "internal error", retryable: false, retryAfterMs: 0 } });
});

test("Stack errors map to upstream_busy, upstream_refused and credit_cap", async () => {
  const [, img] = await upload("errors", 5);
  stack.fail = new sc.StackError(403, "model_locked", "model sam-lite is not available to this account", false, 0, "req_1");
  const [status, r] = await refine("errors", { imageId: img.imageId, points: [{ x: 5, y: 5 }] });
  stack.fail = null;
  assert.equal(status, 502);
  assert.deepEqual(r.error, { code: "upstream_refused", message: "Foldbun Stack refused the call", retryable: false, retryAfterMs: 0, upstreamCode: "model_locked", requestId: "req_1" });
  const busy = fromStack(new sc.StackError(504, "upstream_timeout", "slow", true, 0));
  assert.deepEqual([busy.status, busy.code, busy.retryable, busy.retryAfterMs, busy.upstreamCode], [503, "upstream_busy", true, 1000, "upstream_timeout"]);
  assert.equal(fromStack(new sc.StackError(0, "network_error", "reset", true)).upstreamCode, "network_error");
  const cap = fromStack(new sc.BudgetExceeded("cap"));
  assert.deepEqual([cap.status, cap.code, cap.retryable], [503, "credit_cap", false]);
});

test("RateLimit refills at perMinute", () => {
  const bucket = new RateLimit(60, 2, 0);
  assert.equal(bucket.take(0), 0);
  assert.equal(bucket.take(0), 0);
  assert.equal(bucket.take(0), 1000);
  assert.equal(bucket.take(500), 500);
  assert.equal(bucket.take(1000), 0);
});

// ---- Static files: the page's own files, served by the backend so that page and API share one origin ----

/** A request with the path exactly as written (fetch would tidy `/../` away). */
function raw(port: number, path: string, method = "GET"): Promise<{ status: number; headers: Record<string, string | string[] | undefined>; body: string }> {
  return new Promise((resolve, reject) => {
    const req = request({ host: "127.0.0.1", port, path, method }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve({ status: res.statusCode!, headers: res.headers, body: Buffer.concat(chunks).toString("utf8") }));
    });
    req.on("error", reject);
    req.end();
  });
}

async function listen(staticDir: string | undefined): Promise<{ srv: Server; port: number }> {
  const srv = createServer(new Backend(stack, { now: () => clock, log: () => {}, staticDir }).handle);
  await new Promise<void>((resolve) => srv.listen(0, "127.0.0.1", resolve));
  return { srv, port: (srv.address() as AddressInfo).port };
}

test("static files: index at /, files by name, content types, no-cache, no CORS, nothing from /api/", async () => {
  const dir = await mkdtemp(join(tmpdir(), "static-"));
  const outside = await mkdtemp(join(tmpdir(), "outside-"));
  await Promise.all([
    writeFile(join(dir, "index.html"), "<!doctype html><title>demo</title>"),
    writeFile(join(dir, "app.js"), "export {};"),
    writeFile(join(dir, "style.css"), "a{}"),
    writeFile(join(dir, "logo.svg"), "<svg/>"),
    writeFile(join(dir, "a.png"), "png"),
    writeFile(join(dir, "b.jpg"), "jpg"),
    writeFile(join(dir, "c.webp"), "webp"),
    writeFile(join(dir, "data.bin"), "bin"),
    writeFile(join(dir, "m.mjs"), "mjs"),
    writeFile(join(dir, "d.json"), "{}"),
    writeFile(join(dir, "e.jpeg"), "jpeg"),
    writeFile(join(dir, "f.ico"), "ico"),
    writeFile(join(dir, "My File.js"), "spaced"),
    writeFile(join(dir, ".secret"), "hidden"),
    writeFile(join(outside, "secret.txt"), "outside"),
    mkdir(join(dir, "sub")).then(() => writeFile(join(dir, "sub", "x.html"), "sub")),
    mkdir(join(dir, "api")).then(() => writeFile(join(dir, "api", "x.js"), "api")),
  ]);
  await symlink(join(outside, "secret.txt"), join(dir, "link.txt"));
  const { srv, port } = await listen(dir);
  try {
    const types: Record<string, string> = {
      "/": "text/html; charset=utf-8",
      "/index.html": "text/html; charset=utf-8",
      "/app.js": "text/javascript; charset=utf-8",
      "/style.css": "text/css; charset=utf-8",
      "/logo.svg": "image/svg+xml",
      "/a.png": "image/png",
      "/b.jpg": "image/jpeg",
      "/c.webp": "image/webp",
      "/data.bin": "application/octet-stream",
      "/m.mjs": "text/javascript; charset=utf-8",
      "/d.json": "application/json",
      "/e.jpeg": "image/jpeg",
      "/f.ico": "image/x-icon",
    };
    for (const [path, type] of Object.entries(types)) {
      const r = await raw(port, path);
      assert.equal(r.status, 200, path);
      assert.equal(r.headers["content-type"], type, path);
      assert.equal(r.headers["cache-control"], "no-cache", path);
      assert.equal(r.headers["x-content-type-options"], "nosniff", path);
      assert.equal(r.headers["access-control-allow-origin"], undefined, path);
    }
    assert.equal((await raw(port, "/")).body, "<!doctype html><title>demo</title>");
    assert.equal((await raw(port, "/app.js?v=3")).body, "export {};", "a query string is ignored");
    assert.equal((await raw(port, "/My%20File.js")).body, "spaced");
    // the API is never served from files, and the JSON routes still work next to them
    for (const path of ["/api", "/api/", "/api/x.js", "/api/missing"]) {
      const r = await raw(port, path);
      assert.equal(r.status, 404, path);
      assert.equal(JSON.parse(r.body).error.code, "not_found", path);
    }
    assert.equal((await raw(port, "/api/health")).status, 200);
    // missing, not a regular file, traversal, hidden file, symbolic link: all 404 not_found in the contract's error shape
    const refused = [
      "/missing.js", "/sub", "/sub/", "/sub/x.html", "/../secret.txt", "/..", "/%2e%2e/secret.txt", "/%2E%2E%2Fsecret.txt", "/..%2f..%2fsecret.txt",
      "/.secret", "/%2esecret", "/link.txt", "//secret.txt", "/a.png%00.js", "/%zz", "/%5capp.js", "/sub%2fx.html",
    ];
    for (const path of refused) {
      const r = await raw(port, path);
      assert.equal(r.status, 404, path);
      assert.equal(JSON.parse(r.body).error.code, "not_found", path);
      assert.ok(!r.body.includes("outside"), path);
    }
    // HEAD is GET without a body; every other method is 405 with Allow, whether or not the name exists
    const head = await raw(port, "/app.js", "HEAD");
    assert.deepEqual([head.status, head.body, head.headers["content-length"], head.headers["content-type"]], [200, "", "10", "text/javascript; charset=utf-8"]);
    assert.equal((await raw(port, "/missing.js", "HEAD")).status, 404);
    for (const path of ["/", "/app.js", "/missing.js", "/sub", "/.secret"]) {
      for (const method of ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"]) {
        const r = await raw(port, path, method);
        assert.equal(r.status, 405, `${method} ${path}`);
        assert.equal(r.headers["allow"], "GET, HEAD", `${method} ${path}`);
        assert.equal(JSON.parse(r.body).error.code, "method_not_allowed", `${method} ${path}`);
      }
    }
  } finally {
    await new Promise((resolve) => srv.close(resolve));
    await Promise.all([rm(dir, { recursive: true, force: true }), rm(outside, { recursive: true, force: true })]);
  }
});

test("without a static directory, or with a missing one, every page path is 404 not_found (other methods 405)", async () => {
  for (const staticDir of [undefined, join(tmpdir(), "no-such-static-dir")]) {
    const { srv, port } = await listen(staticDir);
    try {
      for (const path of ["/", "/index.html"]) {
        const r = await raw(port, path);
        assert.equal(r.status, 404);
        assert.equal(JSON.parse(r.body).error.code, "not_found");
      }
      const post = await raw(port, "/", "POST");
      assert.deepEqual([post.status, post.headers["allow"], JSON.parse(post.body).error.code], [405, "GET, HEAD", "method_not_allowed"]);
    } finally {
      await new Promise((resolve) => srv.close(resolve));
    }
  }
});
