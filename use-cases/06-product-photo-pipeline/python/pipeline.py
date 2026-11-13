"""The local half of the product-photo pipeline: four quality rules on a cut-out, and the catalogue composition.

Everything here is plain Pillow plus the standard library, so it runs wherever Python does and is tested without the API.
The cut-out itself (`POST /v1/remove-background`, model `birefnet`) is fetched in main.py.
"""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass, field

from PIL import Image, ImageFilter

# region thresholds
# Quality rules. Every number is a named constant so a catalogue team can tune it. They were set on the 40 photos of the
# full set; expected/full-summary.json records how many of them the rules stop.
FOREGROUND_ALPHA = 128  # alpha at or above this is "the product" (half opaque)
SEMI_LOW, SEMI_HIGH = 16, 240  # alpha strictly between these is "semi-transparent" (neither background nor solid)
MIN_COVERAGE, MAX_COVERAGE = 0.05, 0.90  # share of the frame the product fills: outside means a missed or a swallowed subject
EDGE_BAND = 0.01  # the outer 1 percent of each side (at least 2 px) is the frame edge ...
EDGE_TOUCH_MAX = 0.03  # ... and the product may cover at most 3 percent of that side before it counts as cut off by the frame
MAX_SEMI_SHARE = 0.10  # semi-transparent pixels as a share of all non-background pixels: above this the boundary is uncertain
MIN_COMPONENT_SHARE = 0.02  # a connected piece smaller than 2 percent of the foreground is a speck, not a second product
COMPONENT_SIDE = 256  # connected pieces are counted on a copy scaled down to this long side (specks vanish, cost is bounded)
# endregion

# region composition
CANVAS = 1200  # the catalogue image is CANVAS x CANVAS pixels, white
PADDING = 0.10  # white space around the product, as a share of the canvas on each side
SHADOW_OFFSET = 0.012  # the shadow sits this share of the canvas below the product
SHADOW_BLUR = 0.012  # Gaussian blur radius of the shadow, as a share of the canvas
SHADOW_OPACITY = 0.30
WEBP_QUALITY = 90
# endregion

REASONS = ("coverage_low", "coverage_high", "edge_cut", "semi_transparent", "multiple_pieces")


@dataclass
class Quality:
    coverage: float  # share of the frame with alpha >= FOREGROUND_ALPHA
    edge_touch: float  # the largest share of one frame side covered by the product
    semi_transparent: float  # semi-transparent pixels / non-background pixels
    pieces: int  # connected pieces of the product that are not specks
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons

    def to_json(self) -> dict:
        d = asdict(self)
        d.update(passed=self.passed, coverage=round(self.coverage, 4), edge_touch=round(self.edge_touch, 4), semi_transparent=round(self.semi_transparent, 4))
        return d


def decode_cutout(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.load()
    return im.convert("RGBA")


# region quality
def coverage_and_semi(alpha: Image.Image) -> tuple[float, float]:
    """Foreground share of the frame, and semi-transparent share of everything that is not background (one histogram)."""
    hist = alpha.histogram()
    total = alpha.width * alpha.height
    foreground = sum(hist[FOREGROUND_ALPHA:])
    semi = sum(hist[SEMI_LOW + 1 : SEMI_HIGH])
    visible = sum(hist[SEMI_LOW + 1 :])
    return foreground / total, (semi / visible if visible else 0.0)


def edge_touch(alpha: Image.Image) -> float:
    """The largest share of one frame side that the foreground covers, looking at the outer EDGE_BAND of the frame."""
    w, h = alpha.size
    binary = alpha.point(lambda a: 255 if a >= FOREGROUND_ALPHA else 0)
    bx, by = max(2, round(w * EDGE_BAND)), max(2, round(h * EDGE_BAND))
    bands = [
        (binary.crop((0, 0, w, by)), (w, 1)),
        (binary.crop((0, h - by, w, h)), (w, 1)),
        (binary.crop((0, 0, bx, h)), (1, h)),
        (binary.crop((w - bx, 0, w, h)), (1, h)),
    ]
    shares = []
    for band, size in bands:
        line = band.resize(size, Image.BOX)  # a pixel is lit when any pixel of its row or column in the band is foreground
        shares.append(sum(1 for v in line.tobytes() if v) / (size[0] * size[1]))
    return max(shares)


def count_pieces(alpha: Image.Image) -> int:
    """Connected pieces (8-neighbourhood) of the foreground, ignoring specks below MIN_COMPONENT_SHARE of the foreground."""
    scale = COMPONENT_SIDE / max(alpha.size)
    small = alpha.resize((max(1, round(alpha.width * scale)), max(1, round(alpha.height * scale))), Image.BOX) if scale < 1 else alpha
    w, h = small.size
    fg = bytearray(1 if v >= FOREGROUND_ALPHA else 0 for v in small.tobytes())
    sizes = []
    for start in range(w * h):
        if not fg[start]:
            continue
        fg[start], stack, n = 0, [start], 0
        while stack:
            p = stack.pop()
            n += 1
            x, y = p % w, p // w
            for ny in range(max(0, y - 1), min(h, y + 2)):
                for nx in range(max(0, x - 1), min(w, x + 2)):
                    q = ny * w + nx
                    if fg[q]:
                        fg[q] = 0
                        stack.append(q)
        sizes.append(n)
    total = sum(sizes)
    return sum(1 for n in sizes if n >= MIN_COMPONENT_SHARE * total)


def assess(alpha: Image.Image) -> Quality:
    """Apply the four rules to an 8-bit alpha channel. `reasons` is empty when the cut-out may go into the catalogue."""
    coverage, semi = coverage_and_semi(alpha)
    touch = edge_touch(alpha)
    pieces = count_pieces(alpha)
    reasons = []
    if coverage < MIN_COVERAGE:
        reasons.append("coverage_low")
    if coverage > MAX_COVERAGE:
        reasons.append("coverage_high")
    if touch > EDGE_TOUCH_MAX:
        reasons.append("edge_cut")
    if semi > MAX_SEMI_SHARE:
        reasons.append("semi_transparent")
    if pieces > 1:
        reasons.append("multiple_pieces")
    return Quality(coverage, touch, semi, pieces, reasons)


# endregion


# region compose
def compose(cutout: Image.Image) -> Image.Image:
    """Crop to the alpha bounding box, fit inside the padded canvas, centre on white and add a soft shadow."""
    box = cutout.getchannel("A").point(lambda a: 255 if a > SEMI_LOW else 0).getbbox()
    if box is None:
        raise ValueError("the cut-out is empty")
    product = cutout.crop(box)
    inner = round(CANVAS * (1 - 2 * PADDING))
    scale = inner / max(product.size)
    product = product.resize((max(1, round(product.width * scale)), max(1, round(product.height * scale))), Image.LANCZOS)
    x, y = (CANVAS - product.width) // 2, (CANVAS - product.height) // 2
    shadow = Image.new("L", (CANVAS, CANVAS), 0)
    shadow.paste(product.getchannel("A").point(lambda a: round(a * SHADOW_OPACITY)), (x, y + round(CANVAS * SHADOW_OFFSET)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(CANVAS * SHADOW_BLUR))
    page = Image.new("RGB", (CANVAS, CANVAS), "white")
    page.paste(Image.new("RGB", (CANVAS, CANVAS), "black"), (0, 0), shadow)
    page.paste(product, (x, y), product)
    return page


def encode_webp(page: Image.Image) -> bytes:
    out = io.BytesIO()
    page.save(out, "WEBP", quality=WEBP_QUALITY)
    return out.getvalue()


# endregion
