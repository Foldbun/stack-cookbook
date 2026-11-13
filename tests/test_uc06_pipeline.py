"""Use case 06: the four quality rules and the catalogue composition, on synthetic alpha channels (no API)."""

import io
import sys

from conftest import ROOT
from PIL import Image, ImageDraw

sys.path.insert(0, str(ROOT / "use-cases" / "06-product-photo-pipeline" / "python"))

import pipeline  # noqa: E402


def alpha_with(*boxes, size=(400, 300), value=255):
    im = Image.new("L", size, 0)
    d = ImageDraw.Draw(im)
    for box in boxes:
        d.rectangle(box, fill=value)
    return im


def test_a_centred_product_passes():
    q = pipeline.assess(alpha_with((100, 60, 300, 240)))
    assert q.passed and q.pieces == 1 and q.edge_touch == 0 and 0.25 < q.coverage < 0.35


def test_coverage_too_low_and_too_high():
    assert "coverage_low" in pipeline.assess(alpha_with((190, 140, 210, 160))).reasons
    assert "coverage_high" in pipeline.assess(alpha_with((2, 2, 397, 297))).reasons


def test_a_product_touching_the_frame_is_cut_off():
    q = pipeline.assess(alpha_with((100, 60, 300, 299)))  # runs into the bottom edge
    assert q.reasons == ["edge_cut"] and q.edge_touch > 0.4
    assert pipeline.assess(alpha_with((100, 60, 300, 240))).edge_touch == 0


def test_a_wide_soft_boundary_is_flagged():
    soft = alpha_with((60, 40, 340, 260), value=100)  # all semi-transparent
    assert "semi_transparent" in pipeline.assess(soft).reasons


def test_two_products_are_two_pieces_but_a_speck_is_not():
    two = pipeline.assess(alpha_with((40, 60, 160, 240), (240, 60, 360, 240)))
    assert two.pieces == 2 and "multiple_pieces" in two.reasons
    speck = pipeline.assess(alpha_with((100, 60, 300, 240), (350, 20, 354, 24)))
    assert speck.pieces == 1 and speck.passed


def test_composition_is_a_centred_white_square_with_padding():
    cut = Image.new("RGBA", (300, 150), (0, 0, 0, 0))
    ImageDraw.Draw(cut).rectangle((50, 30, 249, 119), fill=(200, 30, 30, 255))  # 200 x 90 product
    page = pipeline.compose(cut)
    assert page.size == (pipeline.CANVAS, pipeline.CANVAS) and page.mode == "RGB"
    assert page.getpixel((5, 5)) == (255, 255, 255)
    red = Image.new("L", page.size, 0)
    red.putdata([255 if r > 150 and g < 80 else 0 for r, g, _ in page.getdata()])
    x0, y0, x1, y1 = red.getbbox()
    inner = round(pipeline.CANVAS * (1 - 2 * pipeline.PADDING))
    assert abs((x1 - x0) - inner) <= 2  # the long side fills the padded area
    assert abs((x0 + x1) / 2 - pipeline.CANVAS / 2) <= 2 and abs((y0 + y1) / 2 - pipeline.CANVAS / 2) <= 2
    assert page.getpixel((pipeline.CANVAS // 2, y1 + 8)) != (255, 255, 255)  # the shadow is below the product


def test_webp_export_decodes():
    cut = Image.new("RGBA", (50, 50), (10, 10, 10, 255))
    out = pipeline.encode_webp(pipeline.compose(cut))
    assert Image.open(io.BytesIO(out)).size == (pipeline.CANVAS, pipeline.CANVAS)
