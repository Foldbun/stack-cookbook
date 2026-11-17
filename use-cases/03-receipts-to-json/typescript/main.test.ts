import assert from "node:assert/strict";
import { test } from "node:test";

import { costLine, imageInput } from "./main.ts";
import * as sc from "./stackClient.ts";

test("image input: inline in free mode, an uploaded handle with a key", async () => {
  const free = { free: true } as unknown as sc.StackClient; // upload would throw: there is none
  assert.deepEqual(await imageInput(free, new TextEncoder().encode("abc"), "a.jpg"), { base64: "YWJj" }); // no upload: that needs a key

  const keyed = {
    free: false,
    upload: async (data: Uint8Array, type: string) => {
      assert.equal(type, "image/png");
      assert.equal(data.length, 3);
      return { id: "img_1" };
    },
  } as unknown as sc.StackClient;
  assert.deepEqual(await imageInput(keyed, new TextEncoder().encode("abc"), "a.png"), { id: "img_1" });
});

test("cost line labels the free tier", () => {
  const stub = (free: boolean) =>
    ({ free, summary: () => ({ calls: 1, listTotal: 0.25, chargedTotal: free ? 0 : 0.25 }) }) as unknown as sc.StackClient;
  assert.equal(costLine(stub(true)), "list price 0.25 credits (free tier, not charged)");
  assert.equal(costLine(stub(false)), "list price 0.25 credits, charged 0.25");
});
