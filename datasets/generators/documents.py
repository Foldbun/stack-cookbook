"""Synthetic document pages with ground truth: reading-order blocks, table cells, formulas and page Markdown (CC0).

  uv run --project datasets python -m datasets.generators.documents --out <dir> [--seed N]

Six fictional "documents" of 2 to 3 pages (15 pages) are laid out and drawn directly with Pillow, then every page is
written twice: as a clean render and as a "photographed" variant (perspective, rotation, uneven light, blur, noise,
JPEG), 30 images in all. Body text is excerpted from public-domain books (datasets/generators/documents_corpus.json);
headings, byline, running header, page footer, the tables and the formulas are generated here. Every table says it holds
invented figures; the formulas are illustrations that are not quoted from the excerpts.

Page size is 1240 x 1754 px (A4 at 150 dpi). The photographed variant is cropped on a desk-coloured background and
scaled so that its long side is at most 1754 px as well. Fonts: Bitstream Vera Sans (regular, bold, italic, from the
reportlab package; its licence allows redistribution of the rendered pages).

Ground truth per page (the same blocks for both variants; the boxes of the photographed variant are the clean boxes
carried through the same geometry):
  blocks: [{order, type, text, bbox, level?, cells?, latex?}] in reading order, type one of header, title, text,
          caption, table, formula, footer; `cells` is the table as a matrix of strings; `latex` the formula source.
  markdown: the page as Markdown (tables as HTML tables, formulas as $$ ... $$), the blocks joined by a blank line.

Writes <out>/images/<id>.jpg and returns (items, truth, attribution rows). Ids: dNN-pK (clean), dNN-photo-pK.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import random
from pathlib import Path

import numpy as np
import reportlab
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1240, 1754
MX = 120                                   # side margin
TOP, BOTTOM = 160, H - 150                 # the area for body blocks
TEXT_W = W - 2 * MX
FONT_DIR = Path(reportlab.__file__).parent / "fonts"
CORPUS = json.loads((Path(__file__).parent / "documents_corpus.json").read_text(encoding="utf-8"))["sources"]
_fonts: dict = {}


def font(style: str, size: int) -> ImageFont.FreeTypeFont:
    name = {"r": "Vera", "b": "VeraBd", "i": "VeraIt", "bi": "VeraBI"}[style]
    if (name, size) not in _fonts:
        _fonts[(name, size)] = ImageFont.truetype(str(FONT_DIR / f"{name}.ttf"), size)
    return _fonts[(name, size)]


def wrap(text: str, f, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if f.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + [cur]


# ---- formulas: a tiny box layout, drawn at 3x and scaled down so the strokes are anti-aliased --------------------------------

class Fm:
    def __init__(self, w, asc, desc, draw):
        self.w, self.asc, self.desc, self.draw = w, asc, desc, draw


S3 = 3


def T(s, size, style="i"):
    f = font(style, size * S3)
    return Fm(f.getlength(s), size * S3 * 0.76, size * S3 * 0.22, lambda d, x, y: d.text((x, y), s, font=f, fill=0, anchor="ls"))


def H_(*boxes):
    asc, desc, w = max(b.asc for b in boxes), max(b.desc for b in boxes), sum(b.w for b in boxes)

    def draw(d, x, y):
        for b in boxes:
            b.draw(d, x, y)
            x += b.w
    return Fm(w, asc, desc, draw)


def SUP(base, s, size):
    sup = T(s, int(size * 0.65), "i" if s.isalpha() else "r")
    lift = size * S3 * 0.42
    return Fm(base.w + sup.w, max(base.asc, lift + sup.asc), base.desc,
              lambda d, x, y: (base.draw(d, x, y), sup.draw(d, x + base.w, y - lift)))


def SUB(base, s, size):
    sub = T(s, int(size * 0.65), "i" if s.isalpha() else "r")
    drop = size * S3 * 0.18
    return Fm(base.w + sub.w, base.asc, max(base.desc, drop + sub.desc),
              lambda d, x, y: (base.draw(d, x, y), sub.draw(d, x + base.w, y + drop)))


def FRAC(num, den, size):
    pad, gap, axis = 10 * S3, 6 * S3, size * S3 * 0.27
    w = max(num.w, den.w) + 2 * pad

    def draw(d, x, y):
        d.line([(x + 2, y - axis), (x + w - 2, y - axis)], fill=0, width=S3 * 2)
        num.draw(d, x + (w - num.w) / 2, y - axis - gap - num.desc)
        den.draw(d, x + (w - den.w) / 2, y - axis + gap + den.asc)
    return Fm(w, axis + gap + num.asc + num.desc, den.asc + den.desc + gap - axis, draw)


def SQRT(inner, size):
    lead, over = 16 * S3, 6 * S3
    top = inner.asc + over

    def draw(d, x, y):
        d.line([(x, y - top * 0.45), (x + lead * 0.35, y - top * 0.5), (x + lead * 0.6, y + inner.desc),
                (x + lead, y - top), (x + lead + inner.w + 6 * S3, y - top)], fill=0, width=S3 * 2, joint="curve")
        inner.draw(d, x + lead + 3 * S3, y)
    return Fm(lead + inner.w + 8 * S3, top + 2 * S3, inner.desc, draw)


def formula_box(key: str, size: int = 38):
    t = lambda s, st="i": T(s, size, st)
    sp = lambda base, s: SUP(t(base), s, size)
    eq = lambda: t(" = ", "r")
    lorentz_root = SQRT(H_(t("1", "r"), t(" − ", "r"), FRAC(sp("v", "2"), sp("c", "2"), int(size * 0.8))), size)
    forms = {
        "distance": (H_(t("d"), eq(), SQRT(H_(sp("x", "2"), t(" + ", "r"), sp("y", "2"), t(" + ", "r"), sp("z", "2")), size)),
                     r"d = \sqrt{x^2 + y^2 + z^2}"),
        "lorentz_x": (H_(t("x'"), eq(), FRAC(H_(t("x"), t(" − ", "r"), t("vt")), lorentz_root, size)),
                      r"x' = \frac{x - vt}{\sqrt{1 - \frac{v^2}{c^2}}}"),
        "lorentz_t": (H_(t("t'"), eq(), FRAC(H_(t("t"), t(" − ", "r"), FRAC(t("vx"), sp("c", "2"), int(size * 0.8))), lorentz_root, size)),
                      r"t' = \frac{t - \frac{vx}{c^2}}{\sqrt{1 - \frac{v^2}{c^2}}}"),
        "interval": (H_(sp("c", "2"), sp("t", "2"), t(" \u2212 ", "r"), sp("x", "2"), t(" \u2212 ", "r"), sp("y", "2"), t(" \u2212 ", "r"), sp("z", "2"), t(" = 0", "r")),
                     r"c^2 t^2 - x^2 - y^2 - z^2 = 0"),
        "energy": (H_(t("E"), eq(), t("m"), sp("c", "2")), r"E = mc^2"),
        "growth": (H_(SUB(t("N"), "t", size), eq(), SUB(t("N"), "0", size), t(" "), sp("r", "t")), r"N_t = N_0 r^t"),
    }
    return forms[key]


def render_formula(key: str, size: int = 38) -> tuple[Image.Image, str]:
    """The formula as an anti-aliased 'L' image (black on white) and its LaTeX."""
    box, latex = formula_box(key, size)
    pad = 6 * S3
    im = Image.new("L", (int(box.w + 2 * pad) + 1, int(box.asc + box.desc + 2 * pad) + 1), 255)
    box.draw(ImageDraw.Draw(im), pad, pad + box.asc)
    return im.resize((max(1, im.width // S3), max(1, im.height // S3)), Image.LANCZOS), latex


# ---- tables ------------------------------------------------------------------------------------------------------------

def table_data(kind: str, rng: random.Random) -> tuple[str, list[str], list[list[str]]]:
    n = lambda v: f"{v:,}"
    if kind == "pins":
        ops = ["Drawing the wire", "Straightening it", "Cutting it", "Pointing it", "Grinding the top", "Whitening", "Papering"]
        rows = []
        for op in ops[: rng.randint(5, 6)]:
            workers = rng.randint(1, 3)
            rows.append([op, str(workers), n(workers * rng.randint(380, 620))])
        return "Table 1. Output of a small pin workshop by operation (invented figures)", ["Operation", "Workers", "Pins per day"], rows
    if kind == "wages":
        trades = ["Weaver", "Smith", "Carter", "Miller", "Cooper", "Tailor"]
        rows = []
        for tr in trades[: rng.randint(4, 5)]:
            day, days = rng.randint(14, 30), rng.randint(250, 300)
            rows.append([tr, str(day), str(days), n(day * days)])
        return "Table 2. Day wages and yearly earnings by trade, in pence (invented figures)", ["Trade", "Day wage", "Days worked", "Yearly earnings"], rows
    if kind == "groups":
        names = ["Pigeons", "Poultry", "Cattle", "Sheep", "Dogs", "Roses"]
        rows = []
        for g in names[: rng.randint(4, 5)]:
            v = rng.randint(8, 40)
            rows.append([g, str(v), str(rng.randint(2, 9)), f"{rng.randint(20, 90)}%"])
        return "Table 3. Varieties named by breeders, by group (invented figures)", ["Group", "Varieties", "Breeders asked", "Agreeing"], rows
    if kind == "region":
        names = ["Coast", "Lowland", "Upland", "Island", "Marsh"]
        rows = []
        for r_ in names[: rng.randint(4, 5)]:
            sp = rng.randint(40, 160)
            rows.append([r_, str(sp), str(rng.randint(3, sp // 3)), f"{rng.randint(5, 35)}%"])
        return "Table 4. Plant species recorded per region (invented figures)", ["Region", "Species", "Peculiar to it", "Share"], rows
    if kind == "lens":
        rows = []
        for ratio in (0.10, 0.50, 0.80, 0.90, 0.99):
            f = math.sqrt(1 - ratio ** 2)
            rows.append([f"{ratio:.2f}", f"{f:.3f}", f"{100 * f:.1f}"])
        return "Table 5. Contraction of a rod of 100 cm moving along its length (computed)", ["v/c", "Factor", "Length (cm)"], rows
    if kind == "breeds":
        names = ["Tumbler", "Pouter", "Fantail", "Carrier", "Barb", "Jacobin"]
        rows = []
        for b in names[: rng.randint(4, 5)]:
            rows.append([b, str(rng.randint(3, 12)), str(rng.randint(10, 60)), f"{rng.randint(10, 80)}%"])
        return "Table 6. Characters that vary in domestic pigeon breeds (invented figures)", ["Breed", "Examined", "Characters", "Varying"], rows
    if kind == "market":
        names = ["Harbour town", "Market town", "Village", "Hill farm"]
        rows = []
        for m in names:
            d = rng.randint(2, 90)
            rows.append([m, str(d), str(d * rng.randint(2, 5)), str(rng.randint(1, 4))])
        return "Table 7. Distance to market and cost of carriage per ton (invented figures)", ["Place", "Miles", "Carriage (pence)", "Trades"], rows
    if kind == "library":
        rows = []
        for yr in range(1842, 1847):
            loans = rng.randint(150, 420)
            rows.append([str(yr), str(rng.randint(60, 140)), str(rng.randint(12, 40)), n(loans)])
        return "Table 8. A village library by year (invented figures)", ["Year", "Volumes", "Borrowers", "Loans"], rows
    raise KeyError(kind)


# ---- the page ----------------------------------------------------------------------------------------------------------

DOCS = [
    dict(id="d01", src="smith1", running="Wealth of Nations · Book I, Chapter 1", title="The Division of Labour",
         byline="Adam Smith, 1776 (excerpt)", pages=[
             [("h1",), ("byline",), ("h2", "1. Why a trade improves"), ("p", 0), ("p", 1), ("p", 2)],
             [("p", 3), ("h2", "2. The pin workshop"), ("table", "pins"), ("p", 4)],
             [("p", 6), ("table", "wages"), ("h2", "3. A larger scale"), ("p", 7)]]),
    dict(id="d02", src="darwin_intro", running="Origin of Species · Introduction", title="An Abstract and Its Limits",
         byline="Charles Darwin, 1859 (excerpt)", pages=[
             [("h1",), ("byline",), ("h2", "1. How the question arose"), ("p", 0), ("p", 1), ("p", 2)],
             [("p", 3), ("h2", "2. What the abstract cannot give"), ("p", 4), ("table", "groups"), ("p", 5)],
             [("p", 6), ("p", 7), ("table", "region")]]),
    dict(id="d03", src="walden", running="Walden · Reading", title="On Reading Well",
         byline="Henry David Thoreau, 1854 (excerpt)", pages=[
             [("h1",), ("byline",), ("h2", "1. The classics"), ("p", 0), ("p", 1)],
             [("p", 2), ("h2", "2. Books lent and read"), ("table", "library"), ("p", 3)]]),
    dict(id="d04", src="einstein", running="Relativity · Part I", title="Space, Time and the Rigid Body",
         byline="Albert Einstein, 1920 translation (excerpt)", pages=[
             [("h1",), ("byline",), ("h2", "1. Geometry as a physical science"), ("p", 0), ("p", 1), ("formula", "distance")],
             [("p", 4), ("h2", "2. Moving systems"), ("p", 5), ("formula", "lorentz_x"), ("formula", "lorentz_t"), ("p", 6)],
             [("p", 7), ("formula", "interval"), ("h2", "3. A rod in motion"), ("p", 8), ("table", "lens"), ("formula", "energy")]]),
    dict(id="d05", src="darwin_ch1", running="Origin of Species · Chapter I", title="Variation Under Domestication",
         byline="Charles Darwin, 1859 (excerpt)", pages=[
             [("h1",), ("byline",), ("h2", "1. Causes of variability"), ("p", 0), ("table", "breeds"), ("p", 1), ("p", 2)],
             [("p", 3), ("formula", "growth"), ("h2", "2. Geometrical increase"), ("p", 4), ("p", 5), ("p", 6)]]),
    dict(id="d06", src="smith2", running="Wealth of Nations · Book I, Chapter 2", title="The Principle of Exchange",
         byline="Adam Smith, 1776 (excerpt)", pages=[
             [("h1",), ("byline",), ("h2", "1. A propensity to truck"), ("p", 0), ("table", "market"), ("p", 2)],
             [("p", 3), ("h2", "2. The philosopher and the street porter"), ("p", 4), ("p", 5)]]),
]
CLEAN_QUALITY = 90
GREY = (70, 70, 70)


def cell_html(cells: list[list[str]]) -> str:
    return "<table>" + "".join("<tr>" + "".join(f"<td>{html.escape(c, quote=False)}</td>" for c in row) + "</tr>" for row in cells) + "</table>"


def draw_table(d, y, header, rows) -> tuple[int, tuple, list[list[str]]]:
    f, fb, row_h, pad = font("r", 23), font("b", 23), 52, 18
    cols = list(zip(header, *rows))
    widths = [int(max(fb.getlength(c[0]), *(f.getlength(x) for x in c[1:])) + 2 * pad) for c in cols]
    widths[0] = max(widths[0], 280)
    scale = min(1.0, TEXT_W / sum(widths))
    if sum(widths) < TEXT_W * 0.8:
        scale = TEXT_W * 0.8 / sum(widths)
    widths = [int(w * scale) for w in widths]
    total = sum(widths)
    x0 = MX + (TEXT_W - total) // 2
    d.rectangle([x0, y, x0 + total, y + row_h], fill=(226, 226, 226))
    for r in range(len(rows) + 2):
        d.line([(x0, y + r * row_h), (x0 + total, y + r * row_h)], fill=(40, 40, 40), width=2)
    xs = [x0]
    for w in widths:
        xs.append(xs[-1] + w)
    for x in xs:
        d.line([(x, y), (x, y + row_h * (len(rows) + 1))], fill=(40, 40, 40), width=2)
    for r, row in enumerate([header] + rows):
        for c, text in enumerate(row):
            ff = fb if r == 0 else f
            ty = y + r * row_h + row_h // 2
            if c == 0 or r == 0:
                d.text((xs[c] + pad, ty), text, font=ff, fill=0, anchor="lm")
            else:
                d.text((xs[c + 1] - pad, ty), text, font=ff, fill=0, anchor="rm")
    return y + row_h * (len(rows) + 1), (x0, y, x0 + total, y + row_h * (len(rows) + 1)), [header] + rows


def render_page(doc: dict, idx: int, rng: random.Random) -> tuple[Image.Image, list[dict], str]:
    """Draw page `idx` (0-based) of `doc`; return the image, the truth blocks and the page Markdown."""
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    npages = len(doc["pages"])
    paras = CORPUS[doc["src"]]["paragraphs"]
    blocks: list[dict] = []
    add = lambda typ, text, bbox, **kw: blocks.append({"type": typ, "text": text, "bbox": [int(v) for v in bbox], **kw})
    # running header and footer
    fh = font("i", 21)
    d.text((MX, 78), doc["running"], font=fh, fill=GREY, anchor="ls")
    d.line([(MX, 98), (W - MX, 98)], fill=(150, 150, 150), width=2)
    add("header", doc["running"], (MX, 78 - 17, MX + fh.getlength(doc["running"]), 78 + 6))
    y, table_no = TOP, 0
    for item in doc["pages"][idx]:
        kind = item[0]
        if kind == "h1":
            f = font("b", 46)
            d.text((MX, y), doc["title"], font=f, fill=0, anchor="la")
            add("title", doc["title"], (MX, y + 6, MX + f.getlength(doc["title"]), y + 46 + 6), level=1)
            y += 46 + 30
        elif kind == "byline":
            f = font("i", 24)
            d.text((MX, y), doc["byline"], font=f, fill=GREY, anchor="la")
            add("text", doc["byline"], (MX, y + 4, MX + f.getlength(doc["byline"]), y + 24 + 6))
            y += 24 + 40
        elif kind == "h2":
            f = font("b", 31)
            d.text((MX, y), item[1], font=f, fill=0, anchor="la")
            add("title", item[1], (MX, y + 4, MX + f.getlength(item[1]), y + 31 + 6), level=2)
            y += 31 + 24
        elif kind == "p":
            text = paras[item[1]]
            f = font("r", 25)
            lines = wrap(text, f, TEXT_W)
            for k, line in enumerate(lines):
                d.text((MX, y + k * 39), line, font=f, fill=0, anchor="la")
            add("text", text, (MX, y + 4, MX + max(f.getlength(ln) for ln in lines), y + (len(lines) - 1) * 39 + 25 + 6))
            y += len(lines) * 39 + 20
        elif kind == "table":
            caption, header, rows = table_data(item[1], rng)
            f = font("bi", 22)
            d.text((MX, y), caption, font=f, fill=0, anchor="la")
            cap_w = f.getlength(caption)
            if cap_w > TEXT_W:                       # captions are written to fit one line; fail loudly if not
                raise ValueError(f"caption too wide: {caption}")
            add("caption", caption, (MX, y + 3, MX + cap_w, y + 22 + 6))
            y += 22 + 20
            y, bbox, cells = draw_table(d, y, header, rows)
            add("table", cell_html(cells), bbox, cells=cells)
            y += 34
        elif kind == "formula":
            fim, latex = render_formula(item[1])
            fx = MX + (TEXT_W - fim.width) // 2
            y += 10
            im.paste(fim, (fx, y))
            add("formula", f"$${latex}$$", (fx + 6, y + 4, fx + fim.width - 6, y + fim.height - 4), latex=latex)
            y += fim.height + 30
        else:
            raise KeyError(kind)
    if y > BOTTOM:
        raise ValueError(f"{doc['id']} page {idx + 1} overflows by {y - BOTTOM} px")
    footer = f"Public-domain sample text · Page {idx + 1} of {npages}"
    d.line([(MX, H - 112), (W - MX, H - 112)], fill=(150, 150, 150), width=2)
    ff = font("r", 21)
    fw = ff.getlength(footer)
    d.text(((W - fw) / 2, H - 92), footer, font=ff, fill=GREY, anchor="la")
    add("footer", footer, ((W - fw) / 2, H - 92 + 3, (W + fw) / 2, H - 92 + 21 + 5))
    # reading order: the header first, the footer last; the blocks in between are already in order
    for n, b in enumerate(blocks):
        b["order"] = n
    md = []
    for b in blocks:
        if b["type"] == "title":
            md.append("#" * b["level"] + " " + b["text"])
        else:
            md.append(b["text"])
    return im, blocks, "\n\n".join(md)


# ---- the photographed variant ------------------------------------------------------------------------------------------

def _persp_coeffs(src, dst):
    """Coefficients (a..h) of the projective map taking each `dst` point to the matching `src` point."""
    a = []
    for (x, y), (u, v) in zip(dst, src):
        a += [[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]]
    return np.linalg.solve(np.array(a, float), np.array(src, float).reshape(8)).tolist()


def _apply(c, x, y):
    den = c[6] * x + c[7] * y + 1
    return (c[0] * x + c[1] * y + c[2]) / den, (c[3] * x + c[4] * y + c[5]) / den


def photograph(im: Image.Image, rng: random.Random):
    """Page on a desk with perspective, rotation, uneven light, blur and noise. Returns (image, point mapper)."""
    w, h = im.size
    cw, ch = int(w * 1.12), int(h * 1.06)
    bg = Image.new("RGB", (cw, ch), rng.choice([(92, 74, 58), (60, 62, 66), (128, 112, 96), (70, 78, 70)]))
    ox, oy = (cw - w) // 2, (ch - h) // 2
    j = lambda m: rng.uniform(-m, m)
    dst = [(ox + j(0.02) * w + 0.01 * w, oy + j(0.012) * h + 0.005 * h), (ox + w + j(0.02) * w - 0.01 * w, oy + j(0.012) * h + 0.01 * h),
           (ox + w + j(0.02) * w - 0.015 * w, oy + h + j(0.012) * h - 0.01 * h), (ox + j(0.02) * w + 0.015 * w, oy + h + j(0.012) * h - 0.005 * h)]
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    warped = im.convert("RGBA").transform((cw, ch), Image.PERSPECTIVE, _persp_coeffs(src, dst), Image.BICUBIC)
    bg.paste(warped, (0, 0), warped)
    fwd = _persp_coeffs(dst, src)                       # page point -> canvas point
    arr = np.asarray(bg).astype(np.float32)
    yy, xx = np.mgrid[0:ch, 0:cw]
    angle = rng.uniform(0, 2 * np.pi)
    arr *= (1.0 + 0.22 * (np.cos(angle) * (xx / cw - 0.5) + np.sin(angle) * (yy / ch - 0.5)))[..., None]   # uneven light
    rot = rng.uniform(-2.0, 2.0)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).rotate(rot, resample=Image.BICUBIC, fillcolor=(40, 40, 40))
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.7, 1.1)))
    noise = np.random.default_rng(rng.randint(0, 2**31)).normal(0, rng.uniform(3, 6), (ch, cw, 1))
    img = Image.fromarray(np.clip(np.asarray(img, dtype=np.float32) + noise, 0, 255).astype(np.uint8))
    s = min(1.0, H / max(img.size))
    if s < 1:
        img = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
    th = math.radians(rot)

    def mapper(x, y):
        u, v = _apply(fwd, x, y)
        cx, cy = cw / 2, ch / 2
        u2 = cx + (u - cx) * math.cos(th) + (v - cy) * math.sin(th)       # Pillow rotates counter-clockwise
        v2 = cy - (u - cx) * math.sin(th) + (v - cy) * math.cos(th)
        return u2 * s, v2 * s
    return img, mapper


def map_bbox(mapper, bbox, size):
    pts = [mapper(x, y) for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3]))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [max(0, int(min(xs))), max(0, int(min(ys))), min(size[0], int(max(xs)) + 1), min(size[1], int(max(ys)) + 1)]


# ---- the set ------------------------------------------------------------------------------------------------------------

def generate(out: Path, seed: int = 20261113) -> tuple[list, dict, list]:
    """Write images/<id>.jpg under `out` (30 pages); return (manifest items, truth items, attribution rows)."""
    (out / "images").mkdir(parents=True, exist_ok=True)
    items, truth, attr = [], {}, []
    for k, doc in enumerate(DOCS):
        src = CORPUS[doc["src"]]
        for variant in ("clean", "photographed"):
            did = doc["id"] if variant == "clean" else f"{doc['id']}-photo"
            for p in range(len(doc["pages"])):
                rng = random.Random(f"{seed}-{doc['id']}-{p}")                   # tables: the same in both variants
                im, blocks, markdown = render_page(doc, p, rng)
                pid = f"{did}-p{p + 1}"
                if variant == "photographed":
                    prng = random.Random(f"{seed}-photo-{doc['id']}-{p}")
                    im, mapper = photograph(im, prng)
                    for b in blocks:
                        b["bbox"] = map_bbox(mapper, b["bbox"], im.size)
                    quality = prng.randint(58, 72)
                else:
                    quality = CLEAN_QUALITY
                rel = f"images/{pid}.jpg"
                im.convert("RGB").save(out / rel, "JPEG", quality=quality, optimize=True)
                twin = f"{doc['id']}-photo-p{p + 1}" if variant == "clean" else f"{doc['id']}-p{p + 1}"
                items.append({"id": pid, "image": rel, "document": did, "page": p + 1})
                truth[pid] = {"document": did, "page": p + 1, "variant": variant, "twin": twin, "width": im.width, "height": im.height,
                              "blocks": blocks, "markdown": markdown}
                attr.append({"file": rel, "id": pid, "title": f"Synthetic document page {pid} ({variant}), {doc['title']}",
                             "author": "Foldbun Stack Cookbook contributors", "author_url": "",
                             "source_page_url": "https://github.com/Foldbun/stack-cookbook", "licence": "CC0 1.0",
                             "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/", "original_url": src["url"],
                             "modified": "",
                             "notes": f"generated by datasets/generators/documents.py (seed {seed}); layout, headings, tables and formulas are synthetic; "
                                      f"body text excerpted from {src['author']}, {src['title']} ({src['year']}, public domain), {src['part']}"})
    return items, truth, attr


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20261113)
    a = ap.parse_args()
    items, truth, _ = generate(a.out, a.seed)
    print(f"{len(items)} pages -> {a.out}/images; blocks per page: " + ", ".join(f"{k}:{len(v['blocks'])}" for k, v in list(truth.items())[:6]))


if __name__ == "__main__":
    main()
