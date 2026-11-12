// Click-to-segment backend: node main.ts --port 8080 [--host 127.0.0.1] [--static ../web/dist]. Holds the API key; see contract/02-backend.yaml.
import { createServer } from "node:http";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

import { Backend } from "./backend.ts";
import * as sc from "./stackClient.ts";

const EXIT_NO_KEY = 2;

// #region server
const { values } = parseArgs({
  options: { port: { type: "string", default: "8080" }, host: { type: "string", default: "127.0.0.1" }, static: { type: "string" } },
});
// The page's files: --static (relative to the working directory), by default the build of the web component next to this directory.
const staticDir = values.static ? resolve(values.static) : resolve(import.meta.dirname, "../web/dist");
let client: sc.StackClient;
try {
  client = new sc.StackClient(); // key, base URL and credit cap from FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
} catch (err) {
  if (!(err instanceof sc.MissingApiKey)) throw err;
  console.error(err.message);
  process.exit(EXIT_NO_KEY);
}
const models = (process.env.SEGMENTER_MODELS || "sam-lite")
  .split(",")
  .map((m) => m.trim())
  .filter(Boolean);
const backend = new Backend(client, { models, staticDir });
const server = createServer(backend.handle);
server.listen(Number(values.port), values.host, () => console.log(`listening on http://${values.host}:${values.port}`));

for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
    server.closeIdleConnections();
    setTimeout(() => process.exit(0), 4000).unref(); // the contract allows 5 s
  });
}
// #endregion
