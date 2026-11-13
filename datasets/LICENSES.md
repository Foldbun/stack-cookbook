# Dataset licences

Checked on 2026-11-12 against the sources named below. This file records what each source says, what it requires,
and whether the selected files may be redistributed in a public repository and as a release asset. It is a
technical record, not legal advice.

| Source | Licence | Verified at (2026-11-12) | Redistribution of the selected files |
|---|---|---|---|
| Open Images V7, validation images (01, 02, 05, 06) | CC BY 2.0, per image (every row of the metadata file has `License = https://creativecommons.org/licenses/by/2.0/`) | `https://storage.googleapis.com/openimages/2018_04/validation/validation-images-with-rotation.csv` (column `License`), `https://storage.googleapis.com/openimages/web/factsfigures_v7.html` ("The images are listed as having a CC BY 2.0 license.") | Yes, with attribution and an indication of changes. Google states it makes no representation about each image's licence status, so each image is re-checked from the metadata at build time (only `licenses/by/2.0` with a non-empty author is eligible). |
| Open Images V7, annotations: boxes, image-level labels, segmentation masks (01, 02, 05, 06) | CC BY 4.0 (Google LLC) | `https://storage.googleapis.com/openimages/web/factsfigures_v7.html` ("The annotations are licensed by Google LLC under CC BY 4.0 license.") | Yes, with attribution to "Open Images Dataset V7, Google LLC" and an indication of changes (masks are resized to the stored image size, boxes are converted to pixel coordinates). |
| CORD v2 receipts (03) | CC BY 4.0 (NAVER Clova) | `https://huggingface.co/datasets/naver-clova-ix/cord-v2` (dataset metadata `License: cc-by-4.0`; the card body is empty), `https://github.com/clovaai/cord` ("This work is licensed under a Creative Commons Attribution 4.0 International License."), `https://raw.githubusercontent.com/clovaai/cord/master/LICENSE-CC-BY` (CC BY 4.0 legal code) | Yes, with attribution and an indication of changes, for the dataset as published. See the caveat below. |
| Synthetic invoices (03) | CC0 1.0, produced by `datasets/generators/invoices.py` | n/a, generated in this repository from fictional data | Yes. |
| Synthetic document pages (04) | CC0 1.0 for the layout and everything generated (headings, running headers, footers, tables with invented figures, formulas), produced by `datasets/generators/documents.py`. The body text is excerpted from public-domain books (sources below) | Sources fetched as plain text from Project Gutenberg on 2026-11-13: Smith, *The Wealth of Nations* (1776, author d. 1790) `https://www.gutenberg.org/ebooks/3300`; Darwin, *On the Origin of Species* (1859, author d. 1882) `https://www.gutenberg.org/ebooks/1228`; Thoreau, *Walden* (1854, author d. 1862) `https://www.gutenberg.org/ebooks/205`; Einstein, *Relativity: The Special and General Theory*, translated by Robert W. Lawson (English edition 1920) `https://www.gutenberg.org/ebooks/30155`. Project Gutenberg states these ebooks are in the public domain in the United States. | Yes. The excerpts are not modified except that paragraphs longer than 1,000 characters are split at a sentence boundary. The Project Gutenberg licence text and trademark are not reproduced; no Gutenberg file is redistributed, only excerpts of the public-domain text. Publication years are all before 1929 and the authors died before 1956; the Einstein translation is a 1920 English edition, public domain in the United States (the status elsewhere was not checked: do not assume it). |
| Near-duplicate variants (05) | CC BY 2.0, inherited from the source image | n/a, derived by `datasets/build/near_duplicates.py` | Yes, same terms as the source image; the attribution row names the source image and the transforms. |

## Fonts

The pages of use case 04 are drawn with Bitstream Vera Sans (regular, bold, italic, bold italic), the font files shipped inside the `reportlab` package (BSD licence for the package; the Bitstream Vera licence, `reportlab/fonts/bitstream-vera-license.txt`, allows embedding and redistribution of documents rendered with the fonts, as long as the font files themselves are not sold on their own). The font files are not redistributed by this repository: only rendered pages are.

## Attribution text

Every committed image and mask has a row in `datasets/attribution/ATTRIBUTION-<use case>.csv` (and the smoke subset
in `datasets/smoke/<use case>/ATTRIBUTION.csv`) with: file, id, title, author, author URL, source page URL, licence,
licence URL, original URL, and the modification.

- Open Images photos: "Photo by {author}, {source page URL}, licensed CC BY 2.0. Resized to a long side of at most
  1024 px and re-encoded as JPEG." Annotations: "Open Images Dataset V7, Google LLC, licensed CC BY 4.0."
- CORD: "CORD v2, NAVER Clova (https://github.com/clovaai/cord), licensed CC BY 4.0. Re-encoded as JPEG; ground truth
  normalised from the published `gt_parse`."

## Caveats and decisions

- **CORD is included.** Both official pages state CC BY 4.0. Residual risk: CORD receipts show third-party printed
  material (restaurant and shop names, a few logos; the dataset's authors already blurred many names and addresses).
  CC BY 4.0 from NAVER covers NAVER's rights, not any trademark or copyright of the receipt issuer. To ship no
  third-party receipts, run `python -m datasets.build.cord --invoices-only`: use case 03 is then
  built from the 20 synthetic invoices only (smoke: 3 clean + 1 photographed invoice).
- **No people, also for product photos (06).** Use case 06 re-admits product-like classes that the list below excludes
  (bottles, bags, toys, cameras ...) through the `product` profile (`PRODUCT_ALLOWED` in `oi_exclusions.py`). An image
  still qualifies only when no person, body part, face, plate or depiction is annotated on it or predicted with
  confidence >= 0.2, and each image passed the same manual review (brand names or logos printed large, watermarks,
  legible text and people or their reflections were rejected: `REJECT_06`).
- **No people.** Open Images images are selected with the exclusion list in `datasets/build/oi_exclusions.py`
  (person and body-part boxes and labels, children, clothing, faces, licence plates, flags, signs, logos, weapons,
  alcohol, medical, vehicles, screens, statues, depictions) and a manual review of the contact sheets
  (`REJECT` in `openimages.py`). CORD receipts where a hand or finger is visible were removed (`REJECT_INDEX` in
  `cord.py`).
- **Not used.** TextOCR (07) and DocLayNet (the optional layout comparison of 04) belong to later packages and are not part of this one; their
  licences were not checked here and must be verified before those datasets are built.
- **Takedown.** Flickr authors can change a photo's licence. The pinned sha256 values in `sources.lock.json` let a
  rebuild detect changed bytes; a takedown request for a single image is handled by adding it to `REJECT` and
  rebuilding.
