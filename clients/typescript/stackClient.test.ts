// The TypeScript client against the fake API. The scenarios come from harness/scenarios.json, which the
// Python and Go client tests use as well. Run by `harness check` (tests/test_client_conformance.py), which
// starts the fake API and sets FAKE_API_URL; by hand: FAKE_API_URL=http://127.0.0.1:8787 node --test
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, it } from "node:test";
import { crc32, deflateSync } from "node:zlib";

import * as sc from "./stackClient.ts";

const FAKE = process.env.FAKE_API_URL;
if (!FAKE) throw new Error("FAKE_API_URL is not set: run `uv run python -m harness check`, or start `python -m harness fakeapi` and export it");
const KEY = "fbs_fake";
const SCENARIOS: Record<string, unknown> = JSON.parse(readFileSync(new URL("../../harness/scenarios.json", import.meta.url), "utf8"));

/** A distinct small PNG per salt (same bytes as `tiny_png` of the fake API). */
function png(salt = 0, w = 64, h = 48): Uint8Array {
  w += salt;
  const chunk = (type: string, data: Buffer): Buffer => {
    const body = Buffer.concat([Buffer.from(type, "latin1"), data]);
    const out = Buffer.alloc(8 + data.length + 4);
    out.writeUInt32BE(data.length, 0);
    body.copy(out, 4);
    out.writeUInt32BE(crc32(body), 8 + data.length);
    return out;
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr.set([8, 6, 0, 0, 0], 8);
  const raw = Buffer.alloc(h * (1 + 4 * w)); // filter byte 0 and transparent black pixels
  return new Uint8Array(
    Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]),
  );
}

interface Entry {
  path: string;
  status: number;
  userAgent: string | null;
  idempotencyKey: string | null;
  accept: string | null;
}

async function fakeLog(): Promise<{ log: Entry[]; violations: string[] }> {
  // The fake API writes its log entry after it has sent the response: let the last one land.
  await new Promise((resolve) => setTimeout(resolve, 100));
  return (await fetch(FAKE + "/_fake/log")).json() as Promise<{ log: Entry[]; violations: string[] }>;
}

async function scenario(nameOrRules: string | object): Promise<void> {
  const sc = typeof nameOrRules === "string" ? SCENARIOS[nameOrRules] : nameOrRules;
  await fetch(FAKE + "/_fake/reset", { method: "POST", body: JSON.stringify(sc) });
}

async function calls(path: string): Promise<Entry[]> {
  return (await fakeLog()).log.filter((e) => e.path === path);
}

const client = (opts: sc.ClientOptions = {}) => new sc.StackClient({ apiKey: KEY, baseUrl: FAKE, ...opts });

async function rejects(p: Promise<unknown>, ctor: new (...a: never[]) => Error = sc.StackError): Promise<sc.StackError> {
  try {
    await p;
  } catch (err) {
    assert.ok(err instanceof ctor, `unexpected error ${String(err)}`);
    return err as sc.StackError;
  }
  assert.fail("expected a rejection");
}

describe("TypeScript client", () => {
  beforeEach(() => scenario({}));
  afterEach(async () => assert.deepEqual((await fakeLog()).violations, []));

  it("refuses to start without a key", () => {
    const saved = process.env.FOLDBUN_API_KEY;
    delete process.env.FOLDBUN_API_KEY;
    try {
      assert.throws(() => new sc.StackClient(), (e: unknown) => e instanceof sc.MissingApiKey && e.message === sc.MISSING_KEY);
    } finally {
      if (saved !== undefined) process.env.FOLDBUN_API_KEY = saved;
    }
  });

  it("reads its configuration from the environment", async () => {
    const saved = { ...process.env };
    Object.assign(process.env, { FOLDBUN_API_KEY: KEY, FOLDBUN_API_BASE: FAKE, FOLDBUN_MAX_CREDITS: "1.5" });
    try {
      const c = new sc.StackClient();
      assert.equal(c.maxCredits, 1.5);
      assert.ok("ppocr-v6" in (await c.models()));
    } finally {
      process.env = saved;
    }
  });

  it("uploads, calls ocr and keeps a ledger", async () => {
    const c = client();
    const first = await c.upload(png(), "image/png");
    const again = await c.upload(png(), "image/png");
    assert.deepEqual(first, again);
    assert.ok(first.id.startsWith("img_") && first.width === 64 && first.height === 48);
    const r1 = await c.post("/v1/ocr", { image: sc.imageHandle(first.id) }, { tag: "a" });
    const r2 = await c.post("/v1/ocr", { image: sc.imageBase64(png()) }, { tag: "b" });
    assert.equal(r1.image.id, first.id);
    assert.equal(r2.image.id, first.id);
    assert.equal(r1.billing.pool, "free");
    assert.equal(r1.cost, 0);
    assert.deepEqual(c.calls("a"), [
      { endpoint: "/v1/ocr", model: "ppocr-v6", list: 0.25, charged: 0, cache: null, queueMs: 4, processingMs: 700, replayed: false },
    ]);
    assert.deepEqual(c.summary(), { calls: 2, listTotal: 0.5, chargedTotal: 0 });
    const log = (await fakeLog()).log;
    assert.ok(log.every((e) => e.userAgent === sc.USER_AGENT));
    assert.deepEqual(log.filter((e) => e.path === "/v1/images").map((e) => e.status), [201, 200]);
    const keys = log.filter((e) => e.path === "/v1/ocr").map((e) => e.idempotencyKey);
    assert.equal(new Set(keys).size, 2);
    assert.ok(keys.every(Boolean));
  });

  it("bills a model and reports the encode cache", async () => {
    const c = client();
    const h = (await c.upload(png(), "image/png")).id;
    const body = { model: "sam3", image: { id: h }, prompts: [{ text: "cat" }, { text: "dog" }] };
    const a = await c.post("/v1/segment", body);
    const b = await c.post("/v1/segment", body);
    assert.deepEqual([a.cache, a.imageFee, a.cost], ["miss", 0.5, 0.6]);
    assert.deepEqual([b.cache, b.imageFee, b.cost], ["hit", 0, 0.1]);
    assert.deepEqual(c.calls().map((r) => r.cache), ["miss", "hit"]);
    assert.equal(c.summary().chargedTotal, 0.7);
  });

  it("honours retryAfterMs and reuses the idempotency key", async () => {
    await scenario("retry-model-busy");
    const c = client();
    const t = performance.now();
    await c.post("/v1/ocr", { image: sc.imageBase64(png()) });
    assert.ok(performance.now() - t >= 1200);
    const attempts = await calls("/v1/ocr");
    assert.deepEqual(attempts.map((e) => e.status), [503, 200]);
    assert.equal(attempts[0]!.idempotencyKey, attempts[1]!.idempotencyKey);
  });

  it("waits at least one second between retries", async () => {
    await scenario("rate-limited");
    const c = client();
    const h = (await c.upload(png(), "image/png")).id;
    const t = performance.now();
    await c.post("/v1/segment", { model: "sam-lite", image: { id: h }, prompts: [{ box: [1, 1, 20, 20] }] });
    assert.ok(performance.now() - t >= 2000);
    assert.deepEqual((await calls("/v1/segment")).map((e) => e.status), [429, 429, 200]);
  });

  it("raises a typed error for a non-retryable failure", async () => {
    await scenario("not-retryable");
    const c = client();
    const e = await rejects(c.post("/v1/ocr", { image: sc.imageBase64(png()) }));
    assert.deepEqual([e.status, e.code, e.retryable], [502, "bad_model_output", false]);
    assert.equal(e.requestId?.length, 16);
    assert.equal((await calls("/v1/ocr")).length, 1);
  });

  it("replays an idempotent call and detects a conflict", async () => {
    const c = client();
    const h = (await c.upload(png(), "image/png")).id;
    const body = { model: "sam3", image: { id: h }, prompts: [{ text: "cat" }] };
    const first = await c.post("/v1/segment", body, { idempotencyKey: "same-key" });
    const again = await c.post("/v1/segment", body, { idempotencyKey: "same-key" });
    assert.deepEqual(first, again);
    const rows = c.calls();
    assert.deepEqual(rows.map((r) => r.replayed), [false, true]);
    assert.equal(rows[1]!.charged, 0);
    assert.equal(c.summary().chargedTotal, 0.55);
    const e = await rejects(c.post("/v1/segment", { ...body, prompts: [{ text: "dog" }] }, { idempotencyKey: "same-key" }));
    assert.equal(e.code, "idempotency_conflict");
  });

  it("uploads again after an eviction", async () => {
    await scenario("image-evicted");
    const c = client();
    const h = (await c.upload(png(), "image/png")).id;
    await c.post("/v1/ocr", { image: { id: h } });
    await c.post("/v1/ocr", { image: { id: h } });
    const ocr = await calls("/v1/ocr");
    assert.deepEqual(ocr.map((e) => e.status), [200, 404, 200]);
    assert.notEqual(ocr[1]!.idempotencyKey, ocr[2]!.idempotencyKey);
    assert.equal((await calls("/v1/images")).length, 2);
  });

  it("refuses before sending when the credit cap would be passed", async () => {
    const c = client({ maxCredits: 0.3 });
    await c.post("/v1/ocr", { image: sc.imageBase64(png()) });
    const e = await rejects(c.post("/v1/ocr", { image: sc.imageBase64(png()) }), sc.BudgetExceeded);
    assert.equal(e.message, "call refused: up to 0.25 credits would pass FOLDBUN_MAX_CREDITS=0.3 (spent 0.25)");
    assert.equal((await calls("/v1/ocr")).length, 1);
    const h = (await c.upload(png(), "image/png")).id;
    await rejects(c.post("/v1/segment", { model: "sam3", image: { id: h }, prompts: [{ text: "x" }] }), sc.BudgetExceeded);
  });

  it("estimates the worst-case price like the billing examples", async () => {
    const models = await client().models();
    const seg = (model: string, prompts: object[]) => sc.estimate(models, "/v1/segment", { prompts }, model);
    const text = { text: "a" };
    const box = { box: [0, 0, 1, 1] };
    const point = (x: number) => ({ point: { x, y: x } });
    assert.equal(seg("sam3", [text, text, text]), 0.65);
    assert.equal(seg("sam3", [text, text, box, box, box]), 0.6);
    assert.equal(seg("sam3", [box, box, box, box]), 0.55);
    assert.equal(seg("sam3", [text, point(1), point(2)]), 0.59);
    assert.equal(seg("sam-lite", [box, box, box, box]), 0.24);
    assert.equal(sc.estimate(models, "/v1/embeddings", { texts: ["a", "b"] }, "siglip2"), 0.02);
    assert.equal(sc.estimate(models, "/v1/documents/parse", {}, "paddleocr-vl"), 2);
  });

  it("reports what a public account may not call", async () => {
    const c = client({ apiKey: "fbs_fake_public" });
    const models = await c.models();
    assert.equal(models["sam3"].available, false);
    assert.equal(models["ppocr-v6"].perMinute, 10);
    const h = (await c.upload(png(), "image/png")).id;
    const e = await rejects(c.post("/v1/segment", { model: "sam3", image: { id: h }, prompts: [{ text: "cat" }] }));
    assert.deepEqual([e.status, e.code], [403, "model_locked"]);
    const r = await c.post("/v1/ocr", { image: { id: h } });
    assert.equal(r.billing.pool, "public");
    assert.deepEqual(r.billing.allowance, { used: 1, limit: 100 });
  });

  it("raises unauthorized for a wrong key", async () => {
    const e = await rejects(client({ apiKey: "not-a-key" }).models());
    assert.deepEqual([e.status, e.code], [401, "unauthorized"]);
  });

  const batchBody = async (c: sc.StackClient, n: number) => ({
    model: "sam3",
    items: await Promise.all(Array.from({ length: n }, async (_, i) => ({ image: { id: (await c.upload(png(i), "image/png")).id }, prompts: [{ text: "cat" }] }))),
  });
  const collect = async (gen: AsyncGenerator<[string, any]>) => { // eslint-disable-line @typescript-eslint/no-explicit-any
    const out: Array<[string, any]> = []; // eslint-disable-line @typescript-eslint/no-explicit-any
    for await (const ev of gen) out.push(ev);
    return out;
  };

  it("streams batch events and records partial failures", async () => {
    await scenario("batch-partial");
    const c = client();
    const events = await collect(c.batch("segment", await batchBody(c, 4), { tag: "b1" }));
    const names = events.map(([e]) => e);
    assert.ok(names[0] === "progress" && names.at(-1) === "summary");
    assert.equal(names.filter((e) => e === "item").length, 4);
    const items = events.filter(([e]) => e === "item").map(([, d]) => d);
    assert.deepEqual(items.map((i) => i.status), ["ok", "error", "ok", "expired"]);
    assert.equal(items[1].error.code, "bad_model_output");
    assert.deepEqual(events.at(-1)![1].counts, { ok: 2, error: 1, expired: 1 });
    assert.deepEqual(c.calls("b1").map((r) => r.endpoint), ["/v1/batch/segment", "/v1/batch/segment"]);
    assert.equal((await calls("/v1/batch/segment"))[0]!.accept, "text/event-stream");
  });

  it("replays the finished items when a dropped batch is repeated", async () => {
    await scenario("batch-dropped");
    const c = client();
    const body = await batchBody(c, 3);
    const first = await collect(c.batch("segment", body, { idempotencyKey: "batch-key" }));
    assert.deepEqual(first.map(([e]) => e), ["progress", "item", "item"]);
    const second = (await collect(c.batch("segment", body, { idempotencyKey: "batch-key" }))).filter(([e]) => e === "item").map(([, d]) => d);
    assert.deepEqual(second.map((d) => Boolean(d.replayed)), [true, true, false]);
    assert.deepEqual(c.calls().map((r) => r.replayed), [false, false, true, true, false]);
    assert.equal(c.summary().chargedTotal, 1.65);
  });

  it("refuses a batch that would pass the credit cap", async () => {
    const c = client({ maxCredits: 1.0 });
    await rejects(collect(c.batch("segment", await batchBody(c, 2))), sc.BudgetExceeded);
    assert.equal((await calls("/v1/batch/segment")).length, 0);
  });

  it("paces calls with a token bucket", async () => {
    const b = new sc.Bucket(60);
    for (let i = 0; i < 60; i++) await b.take();
    const t = performance.now();
    await b.take();
    const waited = performance.now() - t;
    assert.ok(waited >= 800 && waited <= 1500, `waited ${waited} ms`);
  });

  it("formats numbers in refusals like the other clients", async () => {
    const c = client({ maxCredits: 0.1 });
    const e = await rejects(c.post("/v1/ocr", { image: sc.imageBase64(png()) }), sc.BudgetExceeded);
    assert.match(e.message, /^call refused: up to 0\.25 credits would pass FOLDBUN_MAX_CREDITS=0\.1 \(spent 0\)$/);
  });
});
