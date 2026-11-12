// The pure parts of <stack-segmenter>: no DOM, so `node --test` runs them without a browser.

export interface XY {
  x: number;
  y: number;
}
export interface Point extends XY {
  positive: boolean;
}
export interface Size {
  width: number;
  height: number;
}
/** One separate piece of the object as the backend returns it: flat [x0, y0, x1, y1, ...] rings. */
export interface PolygonPart {
  outer: number[];
  holes?: number[][];
}
export interface RefineResult {
  polygon: PolygonPart[];
  bbox: number[];
  area: number;
  score: number;
  model: string;
  costCredits: number;
  listCredits: number;
  cache: "hit" | "miss";
  latencyMs: number;
}
export interface Failure {
  status: number;
  code: string;
  retryable: boolean;
  retryAfterMs: number;
}
export interface Segment {
  imageId: string;
  width: number;
  height: number;
  model: string;
  score: number;
  points: Point[];
  polygon: PolygonPart[];
}

/** The Stack API accepts at most 20 points per call. */
export const MAX_POINTS = 20;
export const MAX_RETRIES = 3;

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const round = (v: number, digits: number) => Math.round(v * 10 ** digits) / 10 ** digits;

// ---- Coordinates -----------------------------------------------------------------------------------------------

/**
 * A position inside the shown image (CSS pixels from its top-left corner) to pixels of the image itself
 * (naturalWidth x naturalHeight, EXIF orientation applied), clamped to the image.
 */
export function toNatural(shown: XY, size: Size, natural: Size): XY {
  return {
    x: round(clamp((shown.x / size.width) * natural.width, 0, natural.width), 2),
    y: round(clamp((shown.y / size.height) * natural.height, 0, natural.height), 2),
  };
}

/** The other direction: a point in image pixels to CSS pixels inside the shown image. */
export function toShown(natural: XY, size: Size, naturalSize: Size): XY {
  return { x: (natural.x / naturalSize.width) * size.width, y: (natural.y / naturalSize.height) * size.height };
}

// ---- The clicks ------------------------------------------------------------------------------------------------

/** The clicks on the current object, oldest first. */
export class PointList {
  private items: Point[] = [];

  /** False when the list is full (the API takes 20 points). */
  add(x: number, y: number, positive: boolean): boolean {
    if (this.items.length >= MAX_POINTS) return false;
    this.items.push({ x, y, positive });
    return true;
  }
  undo(): Point | undefined {
    return this.items.pop();
  }
  clear(): void {
    this.items = [];
  }
  get length(): number {
    return this.items.length;
  }
  /** At least one positive point is needed before the backend can segment. */
  get hasPositive(): boolean {
    return this.items.some((p) => p.positive);
  }
  /** A copy, so a request in flight keeps the list it was sent with. */
  get points(): Point[] {
    return this.items.map((p) => ({ ...p }));
  }
}

// ---- Requests --------------------------------------------------------------------------------------------------

/** The body of POST /api/refine: every point of the object, every time (the backend keeps no click state). */
export function buildRefine(imageId: string, points: Point[], model?: string) {
  return { imageId, points: points.map(({ x, y, positive }) => ({ x, y, positive })), ...(model ? { model } : {}) };
}

/**
 * Serialises requests and drops stale answers. Every change of the points calls change(); next() hands out the
 * version to send, but only when nothing is in flight, and done() says whether the answer still matches the points.
 * Clicks made during a request therefore collapse into one follow-up request that carries all of them.
 */
export class Scheduler {
  private version = 0;
  private sent = 0;
  private busy = false;

  /** The points changed. With `send` false (nothing to segment) a request in flight only becomes stale. */
  change(send = true): void {
    this.version++;
    if (!send) this.sent = this.version;
  }
  /** The version to send now, or null (nothing to send, or a request is in flight). */
  next(): number | null {
    if (this.busy || this.sent === this.version) return null;
    this.busy = true;
    this.sent = this.version;
    return this.version;
  }
  /** The request for `version` ended. True when its answer is still current. */
  done(version: number): boolean {
    this.busy = false;
    return version === this.version;
  }
}

/** Milliseconds to wait before repeating a 429 / 503 that the backend marked retryable; null when not to retry. */
export function retryDelay(err: Partial<Failure>, attempt: number): number | null {
  if (attempt >= MAX_RETRIES || !err.retryable || (err.status !== 429 && err.status !== 503)) return null;
  return clamp(err.retryAfterMs ?? 0, 500, 10_000);
}

/** One line for the person, chosen by the error code only: the backend's own text is never shown. */
export function errorText(err: Partial<Failure>): string {
  switch (err.code) {
    case "network":
      return "Cannot reach the backend. Check the backend attribute and that it is running.";
    case "rate_limited":
    case "upstream_busy":
      return "The service is busy. Wait a moment and click again.";
    case "credit_cap":
      return "The spending limit of this backend is reached.";
    case "upstream_refused":
      return "The segmentation service refused the request.";
    case "unsupported_media_type":
      return "Use a JPEG, PNG or WebP image.";
    case "payload_too_large":
      return "The image is larger than 20 MiB.";
    case "invalid_session":
      return "The backend rejected the session. Reload the page.";
    case "model_not_allowed":
      return "This backend does not serve that model.";
    case "not_found":
      return "The backend has no such endpoint. Check the backend attribute.";
    case "bad_image":
      return "This file could not be read as an image.";
    case "invalid_request":
    case "image_not_found":
      return "The backend rejected the request. Choose the image again.";
    default:
      return "Something went wrong. Try again.";
  }
}

// ---- Drawing and export ----------------------------------------------------------------------------------------

/** One decimal is enough for a screen and keeps the path short. */
const n1 = (v: number) => String(round(v, 1));

/**
 * SVG path data for the polygon, in CSS pixels of the shown image. Outer rings and holes go into one path
 * (fill-rule evenodd cuts the holes out). Vertices are the centres of outline pixels, hence the half pixel.
 */
export function polygonToPath(parts: PolygonPart[], size: Size, natural: Size): string {
  const rings = parts.flatMap((p) => [p.outer, ...(p.holes ?? [])]);
  return rings
    .filter((ring) => ring.length >= 6)
    .map((ring) => {
      const cmds: string[] = [];
      for (let i = 0; i + 1 < ring.length; i += 2) {
        const s = toShown({ x: ring[i] + 0.5, y: ring[i + 1] + 0.5 }, size, natural);
        cmds.push(`${i === 0 ? "M" : "L"}${n1(s.x)} ${n1(s.y)}`);
      }
      return cmds.join("") + "Z";
    })
    .join("");
}

/** What the `segment` event carries and the Export button saves: the polygon in pixels of the original image. */
export function toSegment(imageId: string, natural: Size, points: Point[], r: RefineResult): Segment {
  return { imageId, width: natural.width, height: natural.height, model: r.model, score: r.score, points, polygon: r.polygon };
}

/** Credits for the stats line: up to four decimals, no trailing zeros. */
export const credits = (v: number): string => String(round(v, 4));
