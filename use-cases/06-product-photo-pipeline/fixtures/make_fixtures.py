"""Write fixtures/synthetic.json: remove-background answers for the three smoke photos, built from their ground-truth masks.

    uv run python use-cases/06-product-photo-pipeline/fixtures/make_fixtures.py

The fake API only answers PNG for this endpoint, but the pipeline asks for WebP. Until a real recording replaces this file,
the cut-out is the photo with the ground-truth mask (softened by a blur, like a real matte) as its alpha channel,
encoded the way the API documents it: lossy WebP with a lossless alpha channel. The answer has the structure of a real one.
"""

import base64
import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageFilter

HERE = Path(__file__).resolve().parent
SMOKE = HERE.parent / "assets" / "smoke"


def main() -> None:
    manifest = json.loads((SMOKE / "manifest.json").read_text())
    out = []
    for item in manifest["items"]:
        path = SMOKE / item["image"]
        photo = Image.open(path).convert("RGB")
        mask = Image.open(SMOKE / "masks" / f"{item['id']}.png").convert("L").filter(ImageFilter.GaussianBlur(1.1))
        cutout = photo.convert("RGBA")
        cutout.putalpha(mask)
        buf = io.BytesIO()
        cutout.save(buf, "WEBP", quality=92, alpha_quality=100)
        out.append(
            {
                "endpoint": "/v1/remove-background",
                "model": "birefnet",
                "image": hashlib.sha256(path.read_bytes()).hexdigest(),
                "request": {"options": {"output": "cutout", "format": "webp"}},
                "response": {"format": "webp", "width": photo.width, "height": photo.height, "data": base64.b64encode(buf.getvalue()).decode()},
            }
        )
    (HERE / "synthetic.json").write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
