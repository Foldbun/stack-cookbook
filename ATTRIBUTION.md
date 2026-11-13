# Attribution

The code in this repository is MIT licensed (see `LICENSE`). Images, datasets and model outputs are listed here with their own licences. Each use case keeps a row per file in `use-cases/<id>/assets/**/ATTRIBUTION*.csv`; `harness check` fails when a file has no row.

## Images and datasets

| Files | Source | Licence |
|---|---|---|
| `use-cases/00-quickstart/assets/smoke/settings.webp` | Synthetic screenshot made for the Foldbun Stack Playground | CC0 1.0 |
| Open Images V7 validation images in the sets of use cases 01, 02, 05 and 06, and the near-duplicate variants derived from them (`datasets/`, `use-cases/*/assets/`) | [Open Images V7](https://storage.googleapis.com/openimages/web/factsfigures_v7.html); author and source page per image in the attribution CSVs | CC BY 2.0 per image; resized and re-encoded |
| Open Images V7 boxes, image-level labels and segmentation masks (use cases 01, 02, 05, 06) | "Open Images Dataset V7, Google LLC" | CC BY 4.0; boxes converted to pixel coordinates, masks resized |
| CORD v2 receipts (use case 03) | [CORD v2, NAVER Clova](https://github.com/clovaai/cord) | CC BY 4.0; re-encoded as JPEG, ground truth normalised |
| Synthetic invoices (use case 03) | Generated in this repository by `datasets/generators/invoices.py` from fictional data | CC0 1.0 |
| Synthetic document pages (use case 04) | Generated in this repository by `datasets/generators/documents.py`; layout, headings, tables and formulas are synthetic, body text is excerpted from public-domain books (Adam Smith, 1776; Charles Darwin, 1859; Henry David Thoreau, 1854; Albert Einstein, 1920 translation by Robert W. Lawson), see `datasets/LICENSES.md` | CC0 1.0 for the generated pages; the excerpted text is in the public domain |

The per-file rows are in `datasets/attribution/ATTRIBUTION-<use case>.csv` (full sets) and `datasets/smoke/<use case>/ATTRIBUTION.csv` (smoke sets, also under `use-cases/*/assets/smoke/`). How each source's licence was checked, and what each one requires, is in [`datasets/LICENSES.md`](datasets/LICENSES.md).

## Models

Results shown in this repository were produced by these models through the Foldbun Stack API.

| Model | Licence |
|---|---|
| `sam3` | SAM License |
| `sam-lite` | Apache-2.0 |
| `ppocr-v6` | Apache-2.0 |
| `paddleocr-vl` | Apache-2.0 |
| `dinov3` | DINOv3 License |
| `siglip2` | Apache-2.0 |
| `birefnet` | MIT |
