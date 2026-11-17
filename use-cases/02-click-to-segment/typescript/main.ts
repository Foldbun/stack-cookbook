// Click-to-segment backend: node main.ts --port 8080 [--host 127.0.0.1] [--static ../web/dist]. Works without a key (free tier) or with FOLDBUN_API_KEY; see contract/02-backend.yaml.
import { createServer } from "node:http";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

import { Backend } from "./backend.ts";
import * as sc from "./stackClient.ts";

// #region server
const { values } = parseArgs({
  options: { port: { type: "string", default: "8080" }, host: { type: "string", default: "127.0.0.1" }, static: { type: "string" } },
});
// The page's files: --static (relative to the working directory), by default the build of the web component next to this directory.
const staticDir = values.static ? resolve(values.static) : resolve(import.meta.dirname, "../web/dist");
// No key: the free tier, and the backend sends the image inline with every click. With FOLDBUN_API_KEY: it uploads each
// image once and sends its handle. The base URL, project and credit cap come from FOLDBUN_API_BASE, FOLDBUN_PROJECT_ID
// and FOLDBUN_MAX_CREDITS.
const client = new sc.StackClient();
const models = (process.env.SEGMENTER_MODELS || "sam-lite")
  .split(",")
  .map((m) => m.trim())
  .filter(Boolean);
const backend = new Backend(client, { models, staticDir });
const server = createServer(backend.handle);
server.listen(Number(values.port), values.host, () => {
  console.log(`listening on http://${values.host}:${values.port}`);
  console.log("mode: " + (client.free ? "free tier (images are sent inline with every click)" : "API key (images are uploaded once)"));
});

for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
    server.closeIdleConnections();
    setTimeout(() => process.exit(0), 4000).unref(); // the contract allows 5 s
  });
}
// #endregion
