// First call to the Foldbun Stack API: check the key, upload an image, read its text, show the cost.
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { parseArgs } from "node:util";

import * as sc from "./stackClient.ts";

const EXIT_FAILED = 1;
const EXIT_NO_KEY = 2;
const EXIT_TIER = 3;
const EXIT_BUDGET = 4;

interface Item {
  id: string;
  image: string;
  imageInput?: string;
}

async function readText(client: sc.StackClient, item: Item, base: string) {
  const data = await readFile(join(base, item.image));
  const contentType = item.image.endsWith(".webp") ? "image/webp" : item.image.endsWith(".png") ? "image/png" : "image/jpeg";
  // #region image-input
  let image;
  if (item.imageInput === "base64") {
    image = sc.imageBase64(data); // inline: simplest for a single call
  } else {
    const uploaded = await client.upload(data, contentType); // free; the handle can be reused for 7 days at most
    image = sc.imageHandle(uploaded.id);
  }
  // #endregion
  // #region first-call
  const result = await client.post("/v1/ocr", { model: "ppocr-v6", image }, { tag: item.id });
  for (const line of result.lines) {
    console.log(`  ${line.confidence.toFixed(2)}  ${line.text}`);
  }
  const billing = result.billing;
  const covered = billing.pool ? `covered by the ${billing.pool} pool (${billing.allowance.used}/${billing.allowance.limit} today)` : "billed";
  console.log(`  list price ${billing.total} credits, charged ${result.cost} (${covered})`);
  // #endregion
  const lines = result.lines.map((ln: sc.Json) => ({ text: ln.text, confidence: ln.confidence, bbox: ln.bbox }));
  return { image: { width: result.image.width, height: result.image.height }, lines };
}

async function main(): Promise<number> {
  const { values: args } = parseArgs({
    options: {
      manifest: { type: "string", default: join(import.meta.dirname, "../assets/smoke/manifest.json") },
      out: { type: "string", default: "out" },
      concurrency: { type: "string", default: "4" },
      limit: { type: "string" },
      resume: { type: "boolean", default: false }, // accepted for the common interface; every call here is cheap
    },
  });
  const manifestPath = resolve(args.manifest);
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  const started = new Date();
  const t0 = performance.now();
  // #region client
  let client: sc.StackClient;
  try {
    client = new sc.StackClient({ maxConcurrency: Number(args.concurrency) }); // FOLDBUN_API_KEY, FOLDBUN_API_BASE
  } catch (err) {
    if (err instanceof sc.MissingApiKey) {
      console.error(err.message);
      return EXIT_NO_KEY;
    }
    throw err;
  }
  const models = await client.models(); // free: checks the key and lists prices
  console.log(`key accepted; ppocr-v6 available: ${models["ppocr-v6"].available}`);
  // #endregion
  const items = [];
  let code = 0;
  for (const item of (manifest.items as Item[]).slice(0, args.limit === undefined ? undefined : Number(args.limit))) {
    console.log(item.id);
    let output = null;
    let status = "ok";
    let error;
    try {
      output = await readText(client, item, dirname(manifestPath));
    } catch (err) {
      if (!(err instanceof sc.StackError)) throw err;
      console.error(`  ${err.describe()}`);
      if (err.code === "model_locked" || err.code === "budget_exceeded") {
        code = err.code === "model_locked" ? EXIT_TIER : EXIT_BUDGET;
        break;
      }
      status = "error";
      error = { status: err.status, code: err.code, message: err.message };
    }
    items.push({ id: item.id, status, calls: client.calls(item.id), output, ...(error ? { error } : {}) });
  }
  const result = {
    usecase: manifest.usecase,
    language: "typescript",
    client: `stackClient ${sc.VERSION}`,
    startedAt: started.toISOString().replace(/\.\d+Z$/, "Z"),
    elapsedMs: Math.floor(performance.now() - t0),
    items,
    ledger: client.summary(),
  };
  await mkdir(args.out, { recursive: true });
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
