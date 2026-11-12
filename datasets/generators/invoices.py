"""Synthetic invoices with ground-truth fields, rendered to images (CC0).

  uv run --project datasets python -m datasets.generators.invoices --out <dir> [--seed N] [--count 20]

20 fictional invoices (invented companies, addresses and amounts): 10 clean renders and 10 "photographed"
variants (perspective, rotation, shadow, blur, noise, JPEG). Layout: reportlab -> pypdfium2 -> Pillow. Every
invoice is internally consistent (line items add up to the subtotal; subtotal - discount + tax = total), prints
amounts in one of two number formats (1,234.50 or 1.234,50), and labels its total differently (Total,
Amount due, Balance due, Grand total) so rules cannot hard-code a single word.

Writes <out>/images/inv-NNN.jpg and returns / prints the ground-truth records.
"""
from __future__ import annotations

import argparse
import io
import json
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib.colors import Color, black, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

VENDORS = [
    ("Brightwater Print Studio", "14 Alder Lane", "Fairhaven, WA 98225"),
    ("Kestrel & Finch Joinery Ltd", "Unit 7, Mill Yard", "Leeds LS11 5AB, UK"),
    ("Tallow Hill Roasters", "88 Quarry Road", "Dunedin 9016, NZ"),
    ("Lumen Harbour Electrical", "5 Wharf Street", "Fremantle WA 6160, AU"),
    ("Ostrander Bike Works", "302 Linden Avenue", "Portland, OR 97214"),
    ("Moss & Marrow Catering", "21 Orchard Court", "Cork T12 XY04, IE"),
    ("Pinecone Digital GmbH", "Gartenweg 19", "04103 Leipzig, DE"),
    ("Saltmarsh Fabrication", "Dock Road 3", "Rotterdam 3011 AB, NL"),
    ("Quillfeather Translations", "9 Rue des Tilleuls", "69003 Lyon, FR"),
    ("Redwing Landscaping Co.", "710 Ridge Drive", "Boise, ID 83702"),
]
CUSTOMERS = ["Alder & Wren Design", "Copperfield Bakery", "Northgate Physio Clinic", "Hollis Family Dental",
             "Meridian Yoga Collective", "Larkspur Books", "Tidewater Dive Shop", "Birchwood Veterinary Group",
             "Juniper Street Cafe", "Foxglove Nursery"]
CATALOG = [
    ("Design consultation (hours)", 85), ("Laminated signage panel", 46.5), ("Site visit and survey", 120),
    ("Cable, 25 m reel", 38.9), ("Espresso beans, 1 kg", 24.75), ("Equipment rental, per day", 65),
    ("Custom oak shelf", 210), ("Translation, per 100 words", 9.4), ("Delivery and installation", 75),
    ("Brake pad set", 29.99), ("Wireless access point", 149), ("Catering, per head", 18.5),
    ("Annual maintenance plan", 340), ("Hedge trimming (hours)", 42), ("Wall-mount bracket", 12.8),
    ("Printed brochure, box of 250", 96), ("Software licence, per seat", 15.5), ("Cleaning supplies bundle", 31.2),
]
TOTAL_LABELS = ["Total", "Amount due", "Balance due", "Grand total"]
CURRENCIES = [("USD", "$", "en", True), ("GBP", "£", "en", True), ("EUR", "€", "eu", False), ("AUD", "A$", "en", True)]
Q = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Q, rounding=ROUND_HALF_UP)


def fmt(v: Decimal, style: str, sym: str, prefix: bool) -> str:
    s = f"{v:,.2f}"
    if style == "eu":
        s = s.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{sym}{s}" if prefix else f"{s} {sym}"


def make_invoice(rng: random.Random, k: int) -> dict:
    vendor = VENDORS[k % len(VENDORS)]
    cur, sym, style, prefix = CURRENCIES[rng.choice([0, 0, 1, 2, 2, 3])]
    n = rng.randint(2, 7)
    lines = []
    for name, price in rng.sample(CATALOG, n):
        qty = rng.choice([1, 1, 2, 3, 4, 5, 8, 10])
        unit = money(Decimal(str(price)) * Decimal(str(round(rng.uniform(0.9, 1.15), 3))))
        lines.append({"description": name, "quantity": qty, "unit_price": unit, "amount": money(unit * qty)})
    subtotal = sum((l["amount"] for l in lines), Decimal(0))
    disc_pct = rng.choice([0, 0, 0, 5, 10])
    discount = money(subtotal * disc_pct / 100)
    tax_rate = rng.choice([0, 5, 8, 10, 15, 20]) if cur != "USD" else rng.choice([0, 6, 7, 8.25])
    tax = money((subtotal - discount) * Decimal(str(tax_rate)) / 100)
    total = subtotal - discount + tax
    issued = date(2025, 1, 6) + timedelta(days=rng.randint(0, 600))
    return {
        "vendor": vendor[0], "vendor_address": list(vendor[1:]), "customer": rng.choice(CUSTOMERS),
        "invoice_number": f"{rng.choice(['INV', 'RE', 'FA'])}-{rng.randint(2025, 2026)}-{rng.randint(100, 9999):04d}",
        "date": issued.isoformat(), "due_date": (issued + timedelta(days=rng.choice([14, 30, 45]))).isoformat(),
        "currency": cur, "symbol": sym, "number_format": style, "symbol_prefix": prefix,
        "line_items": lines, "subtotal": subtotal, "discount_percent": disc_pct, "discount": discount,
        "tax_rate": tax_rate, "tax": tax, "total": total, "total_label": rng.choice(TOTAL_LABELS),
        "layout": rng.choice(["band", "minimal"]), "accent": rng.choice([(0.12, 0.30, 0.55), (0.55, 0.20, 0.15), (0.10, 0.40, 0.30), (0.25, 0.25, 0.28)]),
    }


def render_pdf(inv: dict) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    acc = Color(*inv["accent"])
    f = lambda v: fmt(v, inv["number_format"], inv["symbol"], inv["symbol_prefix"])
    mx = 48
    band = inv["layout"] == "band"
    if band:
        c.setFillColor(acc); c.rect(0, H - 110, W, 110, stroke=0, fill=1); c.setFillColor(white)
    else:
        c.setFillColor(black)
    c.setFont("Helvetica-Bold", 21); c.drawString(mx, H - 52, inv["vendor"])
    c.setFont("Helvetica", 10)
    for j, line in enumerate(inv["vendor_address"]):
        c.drawString(mx, H - 70 - 13 * j, line)
    c.setFont("Helvetica-Bold", 26); c.drawRightString(W - mx, H - 55, "INVOICE" if inv["layout"] == "band" else "Tax invoice")
    c.setFillColor(black); c.setFont("Helvetica", 10)
    y = H - 150
    for label, val in (("Invoice no.", inv["invoice_number"]), ("Date", inv["date"]), ("Due date", inv["due_date"])):
        c.setFont("Helvetica-Bold", 10); c.drawString(W - 230, y, label + ":")
        c.setFont("Helvetica", 10); c.drawString(W - 150, y, val); y -= 15
    c.setFont("Helvetica-Bold", 10); c.drawString(mx, H - 150, "Bill to")
    c.setFont("Helvetica", 11); c.drawString(mx, H - 165, inv["customer"])
    c.setFont("Helvetica", 9); c.drawString(mx, H - 179, "Accounts payable")
    top = H - 235
    cols = [mx, 330, 385, 470]            # description, qty (right edge 375), unit price (right 460), amount (right W-mx)
    if band:
        c.setFillColor(acc); c.rect(mx - 6, top - 6, W - 2 * mx + 12, 22, stroke=0, fill=1); c.setFillColor(white)
    else:
        c.setStrokeColor(acc); c.setLineWidth(1.2); c.line(mx - 6, top + 18, W - mx + 6, top + 18); c.line(mx - 6, top - 6, W - mx + 6, top - 6)
        c.setFillColor(black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(cols[0], top, "Description"); c.drawRightString(375, top, "Qty")
    c.drawRightString(462, top, "Unit price"); c.drawRightString(W - mx, top, "Amount")
    c.setFillColor(black); c.setFont("Helvetica", 10)
    y = top - 28
    for l in inv["line_items"]:
        c.drawString(cols[0], y, l["description"]); c.drawRightString(375, y, str(l["quantity"]))
        c.drawRightString(462, y, f(l["unit_price"])); c.drawRightString(W - mx, y, f(l["amount"]))
        c.setStrokeColor(Color(0.85, 0.85, 0.85)); c.setLineWidth(0.5); c.line(mx - 6, y - 7, W - mx + 6, y - 7)
        y -= 24
    y -= 12
    rows = [("Subtotal", f(inv["subtotal"]))]
    if inv["discount"] > 0:
        rows.append((f"Discount ({inv['discount_percent']}%)", "-" + f(inv["discount"])))
    rows.append((f"{'VAT' if inv['number_format'] == 'eu' else 'Tax'} ({inv['tax_rate']}%)", f(inv["tax"])))
    for label, val in rows:
        c.setFont("Helvetica", 10); c.drawString(330, y, label); c.drawRightString(W - mx, y, val); y -= 16
    c.setStrokeColor(acc); c.setLineWidth(1.5); c.line(325, y + 9, W - mx + 6, y + 9)
    c.setFont("Helvetica-Bold", 13); c.drawString(330, y - 10, inv["total_label"]); c.drawRightString(W - mx, y - 10, f(inv["total"]))
    c.setFont("Helvetica", 8.5); c.setFillColor(Color(0.35, 0.35, 0.35))
    c.drawString(mx, 90, "Payment terms: net " + str((date.fromisoformat(inv["due_date"]) - date.fromisoformat(inv["date"])).days) + " days. Bank transfer to the account on file.")
    c.drawString(mx, 77, "Fictional invoice generated for software testing. Any resemblance to a real business is coincidental.")
    c.showPage(); c.save()
    return buf.getvalue()


def rasterize(pdf: bytes, scale: float = 2.0) -> Image.Image:
    doc = pdfium.PdfDocument(pdf)
    im = doc[0].render(scale=scale).to_pil().convert("RGB")
    doc.close()
    return im


def _persp_coeffs(src, dst):
    A = []
    for (x, y), (u, v) in zip(dst, src):
        A += [[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]]
    return np.linalg.solve(np.array(A, float), np.array(src, float).reshape(8)).tolist()


def photograph(im: Image.Image, rng: random.Random) -> Image.Image:
    """Put the page on a desk-coloured background with perspective, a soft shadow gradient, blur, noise, JPEG-ish look."""
    w, h = im.size
    canvas_w, canvas_h = int(w * 1.25), int(h * 1.12)
    bg = Image.new("RGB", (canvas_w, canvas_h), rng.choice([(92, 74, 58), (60, 62, 66), (128, 112, 96), (40, 44, 40)]))
    ox, oy = (canvas_w - w) // 2, (canvas_h - h) // 2
    j = lambda m: rng.uniform(-m, m)
    dst = [(ox + j(0.05) * w + 0.02 * w, oy + j(0.03) * h + 0.01 * h),
           (ox + w + j(0.05) * w - 0.02 * w, oy + j(0.03) * h + 0.02 * h),
           (ox + w + j(0.05) * w - 0.03 * w, oy + h + j(0.03) * h - 0.02 * h),
           (ox + j(0.05) * w + 0.03 * w, oy + h + j(0.03) * h - 0.01 * h)]
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    page = im.convert("RGBA")
    warped = page.transform((canvas_w, canvas_h), Image.PERSPECTIVE, _persp_coeffs(src, dst), Image.BICUBIC)
    bg.paste(warped, (0, 0), warped)
    arr = np.asarray(bg).astype(np.float32)
    yy, xx = np.mgrid[0:canvas_h, 0:canvas_w]
    angle = rng.uniform(0, 2 * np.pi)
    grad = (np.cos(angle) * (xx / canvas_w - 0.5) + np.sin(angle) * (yy / canvas_h - 0.5))
    arr *= (1.0 + 0.28 * grad)[..., None]                                  # uneven lighting
    arr = np.asarray(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).rotate(rng.uniform(-3, 3), resample=Image.BICUBIC, expand=False, fillcolor=(40, 40, 40)), dtype=np.float32)
    out = Image.fromarray(arr.astype(np.uint8)).filter(ImageFilter.GaussianBlur(rng.uniform(0.9, 1.6)))
    noise = np.random.default_rng(rng.randint(0, 2**31)).normal(0, rng.uniform(4, 8), (canvas_h, canvas_w, 1))
    out = Image.fromarray(np.clip(np.asarray(out, dtype=np.float32) + noise, 0, 255).astype(np.uint8))
    s = 1500 / max(out.size)
    return out.resize((round(out.width * s), round(out.height * s)), Image.LANCZOS) if s < 1 else out


def truth_record(inv: dict, kind: str) -> dict:
    num = lambda d: float(d)
    return {
        "kind": kind, "vendor": inv["vendor"], "customer": inv["customer"], "invoice_number": inv["invoice_number"],
        "date": inv["date"], "due_date": inv["due_date"], "currency": inv["currency"],
        "number_format": inv["number_format"], "total_label": inv["total_label"],
        "line_items": [{"description": l["description"], "quantity": l["quantity"], "unit_price": num(l["unit_price"]),
                        "amount": num(l["amount"])} for l in inv["line_items"]],
        "line_item_count": len(inv["line_items"]), "subtotal": num(inv["subtotal"]), "discount": num(inv["discount"]),
        "tax_rate_percent": inv["tax_rate"], "tax": num(inv["tax"]), "total": num(inv["total"]),
    }


def generate(out: Path, seed: int = 4242, count: int = 20) -> dict:
    """Write images/inv-NNN.jpg under out; return {id: truth}. The first half is clean, the second photographed."""
    rng = random.Random(seed)
    truth = {}
    (out / "images").mkdir(parents=True, exist_ok=True)
    for k in range(count):
        inv = make_invoice(rng, k)
        im = rasterize(render_pdf(inv))
        kind = "clean" if k < count // 2 else "photographed"
        if kind == "photographed":
            im = photograph(im, rng)
            q = rng.randint(55, 70)
        else:
            q = 88
        iid = f"inv-{k + 1:03d}"
        im.save(out / "images" / f"{iid}.jpg", "JPEG", quality=q, optimize=True)
        truth[iid] = truth_record(inv, kind)
    return truth


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--count", type=int, default=20)
    a = ap.parse_args()
    print(json.dumps(generate(a.out, a.seed, a.count), indent=1)[:2000])


if __name__ == "__main__":
    main()
