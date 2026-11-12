// node --test: the pure parts of the component, no browser.
import assert from "node:assert/strict";
import { test } from "node:test";

import { MAX_POINTS, PointList, Scheduler, buildRefine, credits, errorText, polygonToPath, retryDelay, toNatural, toSegment, toShown } from "./logic.ts";

const natural = { width: 4000, height: 3000 };

test("coordinates map from the shown image to the original and back", () => {
  const shown = { width: 400, height: 300 };
  assert.deepEqual(toNatural({ x: 200, y: 150 }, shown, natural), { x: 2000, y: 1500 });
  assert.deepEqual(toNatural({ x: 0, y: 0 }, shown, natural), { x: 0, y: 0 });
  assert.deepEqual(toShown({ x: 2000, y: 1500 }, shown, natural), { x: 200, y: 150 });
  // after a resize the same shown position is another natural position, the same natural point another shown position
  const smaller = { width: 200, height: 150 };
  assert.deepEqual(toNatural({ x: 100, y: 75 }, smaller, natural), { x: 2000, y: 1500 });
  assert.deepEqual(toShown({ x: 2000, y: 1500 }, smaller, natural), { x: 100, y: 75 });
  // a position that is not a whole pixel, and an upscaled display
  assert.deepEqual(toNatural({ x: 33.3, y: 10 }, { width: 100, height: 75 }, { width: 50, height: 30 }), { x: 16.65, y: 4 });
  assert.deepEqual(toShown({ x: 25, y: 15 }, { width: 100, height: 60 }, { width: 50, height: 30 }), { x: 50, y: 30 });
});

test("a click outside the image is clamped onto it", () => {
  assert.deepEqual(toNatural({ x: -5, y: 400 }, { width: 400, height: 300 }, natural), { x: 0, y: 3000 });
});

test("the point list keeps order, undoes, clears and stops at 20", () => {
  const list = new PointList();
  assert.equal(list.hasPositive, false);
  list.add(1, 2, false);
  assert.equal(list.hasPositive, false);
  list.add(3, 4, true);
  assert.deepEqual(list.points, [
    { x: 1, y: 2, positive: false },
    { x: 3, y: 4, positive: true },
  ]);
  assert.equal(list.hasPositive, true);
  assert.deepEqual(list.undo(), { x: 3, y: 4, positive: true });
  assert.equal(list.length, 1);
  const copy = list.points;
  list.clear();
  assert.equal(list.length, 0);
  assert.equal(copy.length, 1, "a copy taken earlier is not changed");
  assert.equal(list.undo(), undefined);
  for (let i = 0; i < MAX_POINTS; i++) assert.equal(list.add(i, i, true), true);
  assert.equal(list.add(0, 0, true), false);
  assert.equal(list.length, MAX_POINTS);
});

test("the refine request carries every point with an explicit positive flag", () => {
  const body = buildRefine("im_0c3f8e2a91b54d7e8f60a1b2c3d4e5f6", [
    { x: 412, y: 230.5, positive: true },
    { x: 80, y: 40, positive: false },
  ]);
  assert.deepEqual(body, {
    imageId: "im_0c3f8e2a91b54d7e8f60a1b2c3d4e5f6",
    points: [
      { x: 412, y: 230.5, positive: true },
      { x: 80, y: 40, positive: false },
    ],
  });
  assert.equal("model" in body, false);
  assert.equal(buildRefine("im_0c3f8e2a91b54d7e8f60a1b2c3d4e5f6", [], "sam3").model, "sam3");
});

test("requests are serialised: clicks during a request collapse into one follow-up, stale answers are dropped", () => {
  const s = new Scheduler();
  assert.equal(s.next(), null, "nothing to send before the first change");
  s.change();
  const first = s.next();
  assert.notEqual(first, null);
  assert.equal(s.next(), null, "one request at a time");
  s.change(); // a click while the first request is in flight
  s.change(); // and another one
  assert.equal(s.next(), null, "still in flight");
  assert.equal(s.done(first!), false, "the answer to the first request is stale");
  const second = s.next();
  assert.notEqual(second, null, "one follow-up carries both clicks");
  assert.equal(s.next(), null);
  assert.equal(s.done(second!), true, "nothing changed since: the answer is current");
  assert.equal(s.next(), null, "and nothing is left to send");
});

test("with no positive point left, an answer in flight is stale and nothing new is sent", () => {
  const s = new Scheduler();
  s.change();
  const v = s.next()!;
  s.change(false); // undo of the last positive point
  assert.equal(s.done(v), false);
  assert.equal(s.next(), null);
  s.change(); // a new click afterwards sends again
  assert.notEqual(s.next(), null);
});

test("429 and 503 are repeated after retryAfterMs, a few times, and only when retryable", () => {
  const busy = { status: 503, code: "upstream_busy", retryable: true, retryAfterMs: 2000 };
  assert.equal(retryDelay(busy, 0), 2000);
  assert.equal(retryDelay({ ...busy, retryAfterMs: 120_000 }, 0), 10_000, "capped");
  assert.equal(retryDelay({ ...busy, retryAfterMs: 0 }, 0), 500, "never a tight loop");
  assert.equal(retryDelay({ status: 429, code: "rate_limited", retryable: true, retryAfterMs: 1000 }, 1), 1000);
  assert.equal(retryDelay(busy, 3), null, "gives up after three repeats");
  assert.equal(retryDelay({ ...busy, retryable: false, code: "credit_cap" }, 0), null);
  assert.equal(retryDelay({ status: 502, code: "upstream_refused", retryable: true, retryAfterMs: 1000 }, 0), null);
  assert.equal(retryDelay({ status: 0, code: "network", retryable: false, retryAfterMs: 0 }, 0), null);
});

test("error text depends on the code only and never repeats server wording", () => {
  assert.match(errorText({ code: "credit_cap" }), /spending limit/);
  assert.equal(errorText({ code: "something_new" }), errorText({}));
  assert.doesNotMatch(errorText({ code: "upstream_refused", status: 502 }), /balance|tier|account/i);
});

test("the polygon becomes one path, holes included, in shown pixels", () => {
  const parts = [
    { outer: [0, 0, 9, 0, 9, 9, 0, 9], holes: [[2, 2, 2, 6, 6, 6, 6, 2]] },
    { outer: [20, 20, 29, 20, 29, 29] },
  ];
  // a 30 x 30 pixel image shown at 100 x 100: vertices sit at pixel centres (+0.5) times 100/30
  const d = polygonToPath(parts, { width: 100, height: 100 }, { width: 30, height: 30 });
  const rings = d.split("Z").filter(Boolean);
  assert.equal(rings.length, 3, "outer, hole and second part: three closed rings");
  assert.match(rings[0], /^M1\.7 1\.7L31\.7 1\.7L31\.7 31\.7L1\.7 31\.7$/);
  assert.match(rings[1], /^M8\.3 8\.3L8\.3 21\.7L21\.7 21\.7L21\.7 8\.3$/);
  assert.equal(rings[2].split("L").length, 3);
  assert.equal(polygonToPath([], { width: 100, height: 100 }, natural), "");
  assert.equal(polygonToPath([{ outer: [1, 2, 3, 4] }], { width: 100, height: 100 }, natural), "", "a ring needs three vertices");
});

test("the exported segment holds the polygon in pixels of the original", () => {
  const result = { polygon: [{ outer: [1, 1, 5, 1, 5, 5] }], bbox: [1, 1, 5, 5], area: 8, score: 0.93, model: "sam-lite", costCredits: 0.01, listCredits: 0.21, cache: "miss" as const, latencyMs: 80 };
  const seg = toSegment("im_0c3f8e2a91b54d7e8f60a1b2c3d4e5f6", natural, [{ x: 3, y: 3, positive: true }], result);
  assert.deepEqual(seg, {
    imageId: "im_0c3f8e2a91b54d7e8f60a1b2c3d4e5f6",
    width: 4000,
    height: 3000,
    model: "sam-lite",
    score: 0.93,
    points: [{ x: 3, y: 3, positive: true }],
    polygon: [{ outer: [1, 1, 5, 1, 5, 5] }],
  });
  assert.equal(credits(0.21), "0.21");
  assert.equal(credits(0.010000001), "0.01");
  assert.equal(credits(0), "0");
});
