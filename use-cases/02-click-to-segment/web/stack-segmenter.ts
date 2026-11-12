// <stack-segmenter>: click on an object, get its outline. A native Web Component with no dependency. It talks only to
// the backend of this use case (contract/02-backend.yaml), which holds the API key; the browser never sees it.
import {
  PointList,
  Scheduler,
  buildRefine,
  credits,
  errorText,
  polygonToPath,
  retryDelay,
  toNatural,
  toSegment,
  toShown,
  type Failure,
  type Point,
  type RefineResult,
  type Size,
} from "./logic.ts";

const TYPES = ["image/jpeg", "image/png", "image/webp"];
const STEP = 0.02; // keyboard cursor step, as a fraction of the image (Shift: five times)

const STYLE = `
:host { display: block; font: 14px/1.4 system-ui, sans-serif; color: var(--seg-text, CanvasText); color-scheme: light dark; }
.bar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
button { font: inherit; padding: 6px 12px; cursor: pointer; color: var(--seg-text, CanvasText); background: var(--seg-surface, Canvas);
  border: 1px solid var(--seg-border, GrayText); border-radius: var(--seg-radius, 2px); }
button:hover:not(:disabled) { background: var(--seg-hover, color-mix(in srgb, currentColor 8%, transparent)); }
button:disabled { opacity: .5; cursor: default; }
:focus-visible { outline: 2px solid var(--seg-accent, Highlight); outline-offset: 2px; }
.stage { display: grid; place-items: center; min-height: 160px; padding: 8px; background: var(--seg-surface, Canvas);
  border: 1px dashed var(--seg-border, GrayText); border-radius: var(--seg-radius, 2px); }
.stage.drag { border-color: var(--seg-accent, Highlight); }
.frame { position: relative; display: none; max-width: 100%; line-height: 0; cursor: crosshair; touch-action: manipulation; }
.frame.on { display: block; }
img { display: block; max-width: 100%; height: auto; -webkit-user-drag: none; user-select: none; }
svg { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; overflow: visible; }
.shape { fill: var(--seg-fill, color-mix(in srgb, var(--seg-accent, Highlight) 35%, transparent)); fill-rule: evenodd;
  stroke: var(--seg-accent, Highlight); stroke-width: 2; stroke-linejoin: round; }
.pos { fill: var(--seg-positive, #1a7f37); } .neg { fill: var(--seg-negative, #cf222e); }
.dot { stroke: #fff; stroke-width: 2; } .cross { fill: none; stroke: var(--seg-accent, Highlight); stroke-width: 2; stroke-dasharray: 3 2; }
.hint { margin: 0; color: var(--seg-muted, GrayText); }
.hint.on { display: none; }
.status { min-height: 1.4em; margin: 8px 0; }
.status.error { color: var(--seg-negative, #cf222e); }
dl { display: flex; flex-wrap: wrap; gap: 4px 24px; margin: 0; }
dl div { display: flex; gap: 6px; } dt { color: var(--seg-muted, GrayText); } dd { margin: 0; font-variant-numeric: tabular-nums; }
`;

const TEMPLATE = `
<div class="bar" role="toolbar" aria-label="Segmentation controls">
  <button type="button" class="choose">Choose image</button>
  <button type="button" class="undo" disabled title="Ctrl/Cmd+Z">Undo</button>
  <button type="button" class="clear" disabled>Clear</button>
  <button type="button" class="export" disabled>Export JSON</button>
  <input type="file" accept="image/jpeg,image/png,image/webp" hidden aria-label="Image file">
</div>
<div class="stage" tabindex="0" role="group"
  aria-label="Image. Click or press Enter to mark the object, right click, Alt+click or Shift+Enter to exclude. Arrow keys move the cursor.">
  <p class="hint">Drop an image here or choose one.</p>
  <div class="frame"><img alt="The image to segment"><svg aria-hidden="true"></svg></div>
</div>
<p class="status" role="status" aria-live="polite"></p>
<dl aria-label="Result">
  <div><dt>Score</dt><dd class="score">-</dd></div><div><dt>Points</dt><dd class="npoints">0</dd></div>
  <div><dt>Cost</dt><dd class="cost">-</dd></div><div><dt>Cache</dt><dd class="cache">-</dd></div>
  <div><dt>Session</dt><dd class="session">-</dd></div>
</dl>`;

/** An error answer of the backend (or no answer at all, status 0). */
class BackendError extends Error implements Failure {
  status: number;
  code: string;
  retryable: boolean;
  retryAfterMs: number;
  constructor(status: number, code: string, retryable = false, retryAfterMs = 0) {
    super(code);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.retryAfterMs = retryAfterMs;
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
// A new session per page load. randomUUID needs a secure context, so fall back for plain http on a LAN.
const newSession = () => crypto.randomUUID?.() ?? Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");

/**
 * Attributes: `backend` (base URL, default same origin), `model` (optional), `src` (an image URL; it must be
 * readable by this page). Event: `segment`, detail `{imageId, width, height, model, score, points, polygon}`.
 * CSS custom properties: --seg-accent, --seg-positive, --seg-negative, --seg-fill, --seg-text, --seg-muted,
 * --seg-surface, --seg-border, --seg-hover, --seg-radius.
 */
export class StackSegmenter extends HTMLElement {
  static observedAttributes = ["src"];
  readonly sessionId = newSession();

  private $: <T extends Element>(sel: string) => T;
  private points = new PointList();
  private sched = new Scheduler();
  private blob: Blob | null = null;
  private url = "";
  private imageId = "";
  private natural: Size = { width: 0, height: 0 };
  private result: RefineResult | null = null;
  private cursor: { x: number; y: number } | null = null; // keyboard cursor, in fractions of the image
  private gen = 0; // counts loaded images, so a slow load never overwrites a newer one
  private srcLoaded = "";

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `<style>${STYLE}</style>${TEMPLATE}`;
    this.$ = (sel) => root.querySelector(sel)!;
    const input = this.$<HTMLInputElement>("input");
    this.$(".choose").addEventListener("click", () => input.click());
    input.addEventListener("change", () => input.files?.[0] && void this.load(input.files[0]));
    this.$(".undo").addEventListener("click", () => this.undo());
    this.$(".clear").addEventListener("click", () => this.clear());
    this.$(".export").addEventListener("click", () => this.download());
    this.bindUpload(this.$(".stage"));
    this.bindClicks(this.$(".frame"));
    new ResizeObserver(() => this.draw()).observe(this.$("img"));
  }

  get backend(): string {
    return (this.getAttribute("backend") ?? "").replace(/\/+$/, "");
  }
  set backend(v: string) {
    this.setAttribute("backend", v);
  }
  get model(): string {
    return this.getAttribute("model") ?? "";
  }
  set model(v: string) {
    this.setAttribute("model", v);
  }
  get src(): string {
    return this.getAttribute("src") ?? "";
  }
  set src(v: string) {
    this.setAttribute("src", v);
  }

  connectedCallback(): void {
    this.loadSrc();
  }

  attributeChangedCallback(): void {
    if (this.isConnected) this.loadSrc();
  }

  private loadSrc(): void {
    if (this.src && this.src !== this.srcLoaded) void this.load(this.src);
  }

  // #region upload
  /** Dropping an image on the stage loads it. */
  private bindUpload(stage: Element): void {
    stage.addEventListener("dragover", (e) => {
      e.preventDefault();
      stage.classList.add("drag");
    });
    stage.addEventListener("dragleave", () => stage.classList.remove("drag"));
    stage.addEventListener("drop", (e) => {
      e.preventDefault();
      stage.classList.remove("drag");
      const file = (e as DragEvent).dataTransfer?.files[0];
      if (file) void this.load(file);
    });
  }

  /** Show an image (a File, a Blob or a URL) and upload it once; clicks work as soon as the upload is done. */
  async load(source: Blob | string): Promise<void> {
    const gen = ++this.gen;
    if (typeof source === "string") this.srcLoaded = source;
    this.say("Loading the image...");
    try {
      const blob = typeof source === "string" ? await (await fetch(source)).blob() : source;
      if (gen !== this.gen) return;
      if (!TYPES.includes(blob.type)) throw new BackendError(415, "unsupported_media_type");
      this.points.clear();
      this.sched.change(false);
      this.result = null;
      this.imageId = "";
      this.blob = blob;
      URL.revokeObjectURL(this.url);
      this.url = URL.createObjectURL(blob);
      const img = this.$<HTMLImageElement>("img");
      img.src = this.url;
      await img.decode().catch(() => Promise.reject(new BackendError(0, "bad_image")));
      if (gen !== this.gen) return;
      this.$(".frame").classList.add("on");
      this.$(".hint").classList.add("on");
      this.say("Uploading...");
      await this.upload();
      if (gen !== this.gen) return;
      this.say("Click on the object. Right click or Alt+click marks what to leave out.");
    } catch (err) {
      if (gen === this.gen) this.fail(err instanceof BackendError ? err : new BackendError(0, typeof source === "string" ? "network" : "bad_image"));
    }
    this.sync();
  }

  /** POST the raw bytes; the backend uploads to Stack once per distinct image and answers with an imageId. */
  private async upload(): Promise<void> {
    const r = await this.call<{ imageId: string; width: number; height: number }>("/api/images", {
      method: "POST",
      headers: { "Content-Type": this.blob!.type },
      body: this.blob,
    });
    this.imageId = r.imageId;
    this.natural = { width: r.width, height: r.height };
  }
  // #endregion

  // #region request
  /** One call to the backend: the session header on every request, 429 / 503 repeated after retryAfterMs. */
  private async call<T>(path: string, init: RequestInit = {}): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      try {
        let res: Response;
        try {
          res = await fetch(this.backend + path, { ...init, headers: { ...init.headers, "X-Session-Id": this.sessionId } });
        } catch {
          throw new BackendError(0, "network");
        }
        if (res.ok) return (await res.json()) as T;
        const e = ((await res.json().catch(() => ({}))) as { error?: Partial<Failure> }).error ?? {};
        throw new BackendError(res.status, e.code ?? "error", e.retryable ?? false, e.retryAfterMs ?? 0);
      } catch (err) {
        const wait = err instanceof BackendError ? retryDelay(err, attempt) : null;
        if (wait === null) throw err;
        this.say(`The service is busy. Trying again in ${Math.ceil(wait / 1000)} s...`);
        await sleep(wait);
      }
    }
  }

  /** Every request carries all points. When the backend lost the image (404), upload it again once. */
  private async refine(): Promise<RefineResult> {
    const post = () => this.call<RefineResult>("/api/refine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildRefine(this.imageId, this.points.points, this.model || undefined)),
    });
    try {
      return await post();
    } catch (err) {
      if (!(err instanceof BackendError) || err.code !== "image_not_found") throw err;
      await this.upload();
      return post();
    }
  }

  /** Sends when nothing is in flight. Clicks made meanwhile collapse into the next request; stale answers are dropped. */
  private async pump(): Promise<void> {
    const version = this.sched.next();
    if (version === null) return;
    const sent = this.points.points;
    try {
      const r = await this.refine();
      if (this.sched.done(version)) await this.show(r, sent);
    } catch (err) {
      if (this.sched.done(version)) this.fail(err);
    }
    void this.pump();
  }
  // #endregion

  // #region click
  /** Left click: a positive point. Right click or Alt+click: a negative one. */
  private bindClicks(frame: Element): void {
    frame.addEventListener("click", (e) => this.clickAt((e as MouseEvent).clientX, (e as MouseEvent).clientY, !(e as MouseEvent).altKey));
    frame.addEventListener("contextmenu", (e) => {
      e.preventDefault(); // not the browser menu
      this.clickAt((e as MouseEvent).clientX, (e as MouseEvent).clientY, false);
    });
    this.addEventListener("keydown", (e) => this.onKey(e));
    const stage = this.$(".stage");
    for (const ev of ["focus", "blur"]) stage.addEventListener(ev, () => this.draw());
  }

  /** Screen position to pixels of the image, whatever size the image is shown at. */
  private clickAt(clientX: number, clientY: number, positive: boolean): void {
    if (!this.imageId) return;
    const box = this.$("img").getBoundingClientRect();
    const p = toNatural({ x: clientX - box.left, y: clientY - box.top }, box, this.natural);
    this.add(p.x, p.y, positive);
  }

  private add(x: number, y: number, positive: boolean): void {
    if (!this.points.add(x, y, positive)) return this.say("At most 20 points. Undo one or clear.");
    this.changed();
  }

  undo(): void {
    if (this.points.undo()) this.changed();
  }

  clear(): void {
    this.points.clear();
    this.changed();
  }

  /** The points changed: segment again, or, with no positive point left, clear the outline. */
  private changed(): void {
    const send = this.points.hasPositive;
    this.sched.change(send);
    if (!send) this.result = null;
    this.sync();
    void this.pump();
  }

  /** Enter or Space marks at the keyboard cursor (Shift or Alt+Enter: a negative point); arrows move it; Ctrl/Cmd+Z undoes. */
  private onKey(e: KeyboardEvent): void {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      return this.undo();
    }
    if (e.composedPath()[0] !== this.$(".stage")) return;
    const dir: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    const c = (this.cursor ??= { x: 0.5, y: 0.5 });
    if (dir[e.key]) {
      const s = STEP * (e.shiftKey ? 5 : 1);
      c.x = Math.min(1, Math.max(0, c.x + dir[e.key][0] * s));
      c.y = Math.min(1, Math.max(0, c.y + dir[e.key][1] * s));
    } else if ((e.key === "Enter" || e.key === " ") && this.imageId) {
      const p = toNatural(c, { width: 1, height: 1 }, this.natural);
      this.add(p.x, p.y, !(e.shiftKey || e.altKey));
    } else return;
    e.preventDefault();
    this.draw();
  }
  // #endregion

  // #region draw
  /** Show an answer: remember it, update the numbers, draw, and tell the page. */
  private async show(r: RefineResult, sent: Point[]): Promise<void> {
    this.result = r;
    this.say(r.polygon.length ? "Click again to refine, or export the outline." : "No outline for these clicks. Add or undo a point.");
    this.sync();
    this.dispatchEvent(new CustomEvent("segment", { detail: toSegment(this.imageId, this.natural, sent, r), bubbles: true, composed: true }));
    try {
      const u = await this.call<{ clicks: number; costCredits: number }>("/api/usage");
      this.$(".session").textContent = `${u.clicks} calls, ${credits(u.costCredits)} credits`;
    } catch {
      /* the session totals are optional */
    }
  }

  /** Redraw the overlay in CSS pixels of the shown image; runs after every change and every resize. */
  private draw(): void {
    const img = this.$<HTMLImageElement>("img");
    const size = { width: img.clientWidth, height: img.clientHeight };
    const svg = this.$<SVGElement>("svg");
    if (!this.imageId || !size.width) return void (svg.innerHTML = "");
    svg.setAttribute("viewBox", `0 0 ${size.width} ${size.height}`);
    const path = this.result ? `<path class="shape" d="${polygonToPath(this.result.polygon, size, this.natural)}"/>` : "";
    const dots = this.points.points.map((p) => {
      const s = toShown(p, size, this.natural);
      return `<circle class="dot ${p.positive ? "pos" : "neg"}" cx="${s.x.toFixed(1)}" cy="${s.y.toFixed(1)}" r="6"/>`;
    });
    const k = this.cursor && this.shadowRoot!.activeElement === this.$(".stage") ? this.cursor : null;
    const cross = k ? `<circle class="cross" cx="${(k.x * size.width).toFixed(1)}" cy="${(k.y * size.height).toFixed(1)}" r="10"/>` : "";
    svg.innerHTML = path + dots.join("") + cross;
  }

  /** Numbers, button states and overlay, from the current state. */
  private sync(): void {
    const r = this.result;
    this.$(".score").textContent = r ? r.score.toFixed(3) : "-";
    this.$(".npoints").textContent = String(this.points.length);
    this.$(".cost").textContent = r ? `${credits(r.costCredits)} credits` : "-";
    this.$(".cache").textContent = r ? `${r.cache}, ${r.latencyMs} ms` : "-";
    this.$<HTMLButtonElement>(".undo").disabled = this.$<HTMLButtonElement>(".clear").disabled = this.points.length === 0;
    this.$<HTMLButtonElement>(".export").disabled = !r?.polygon.length;
    this.draw();
  }

  private say(text: string, error = false): void {
    const el = this.$(".status");
    el.textContent = text;
    el.classList.toggle("error", error);
  }

  private fail(err: unknown): void {
    this.say(errorText(err instanceof BackendError ? err : {}), true);
  }
  // #endregion

  // #region export
  /** The outline of the current object as JSON (pixels of the original image), or null before the first answer. */
  toJSON(): ReturnType<typeof toSegment> | null {
    return this.result ? toSegment(this.imageId, this.natural, this.points.points, this.result) : null;
  }

  private download(): void {
    const seg = this.toJSON();
    if (!seg) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(seg, null, 2)], { type: "application/json" }));
    a.download = "segment.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  // #endregion
}

if (!customElements.get("stack-segmenter")) customElements.define("stack-segmenter", StackSegmenter);
