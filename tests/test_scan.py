"""The publication scan on recorded answers: embedded images are checked as images, everything else as text."""

import base64
import io

from PIL import Image

from harness import scan


def _png() -> bytes:
    buf = io.BytesIO()
    Image.effect_noise((64, 64), 80).save(buf, "PNG")
    return buf.getvalue()


def test_an_embedded_image_is_taken_out_of_the_text():
    blob = base64.b64encode(_png()).decode()
    assert len(blob) >= 512
    text, problems = scan.split_embedded_images("use-cases/x/fixtures/recorded.json", f'{{"data": "{blob}", "note": "kept"}}')
    assert blob not in text and '"note": "kept"' in text
    assert problems == []


def test_an_embedded_image_with_metadata_is_reported():
    blob = base64.b64encode(_png() + b"Exif\x00\x00" + b"\x00" * 600).decode()
    _, problems = scan.split_embedded_images("use-cases/x/fixtures/recorded.json", f'{{"data": "{blob}"}}')
    assert len(problems) == 1 and "metadata" in problems[0]


def test_base64_that_is_not_an_image_stays_in_the_text():
    blob = base64.b64encode(b"plain bytes, not an image " * 40).decode()
    line = f'{{"data": "{blob}"}}'
    text, problems = scan.split_embedded_images("use-cases/x/fixtures/recorded.json", line)
    assert text == line and problems == []
