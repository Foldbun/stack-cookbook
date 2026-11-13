# Datasets

Inputs and ground truth for the first four cookbook use cases. Smoke sets are committed here; full sets are built
into release tarballs. Everything is reproducible from the builders: fixed seeds, pinned source URLs, and the sha256 of
every download in `sources.lock.json`. Licences and attribution: [LICENSES.md](LICENSES.md) and `attribution/`.

| Use case | Set | Source | Smoke (committed) | Full (release tarball) |
|---|---|---|---|---|
| `01-dataset-prelabelling` | 60 images, 4 classes (Dog, Cat, Bird, Boat), boxes | Open Images V7 validation | 4 images, 0.9 MB | 10.6 MB |
| `02-click-to-segment` | 30 objects with masks | Open Images V7 validation masks | 3 objects, 0.4 MB | 4.6 MB |
| `03-receipts-to-json` | 60 CORD v2 receipts + 20 synthetic invoices (10 clean, 10 photographed) | CORD v2 (CC BY 4.0), generator (CC0) | 3 receipts + 1 photographed invoice, 0.5 MB | 11.7 MB |
| `04-documents-to-rag-chunks` | 30 synthetic document pages: 15 clean + the same 15 photographed (6 documents of 2 to 3 pages) | generator (CC0), body text from public-domain books | 2 pages, 0.4 MB | 7.7 MB |
| `05-image-library-search` | 400 images, 100 near-duplicate variants, 40 text queries | Open Images V7 validation | 20 images + 5 variants + 5 queries, 2.9 MB | 73.7 MB |
| `06-product-photo-pipeline` | 40 product-like objects (9 families) with masks | Open Images V7 validation masks | 3 objects, 0.4 MB | 5.1 MB |

## Layout

```
datasets/
  build/            builders (python -m datasets.build[.<name>])
  generators/       synthetic data (invoices.py, documents.py + documents_corpus.json: the public-domain excerpts)
  smoke/<use case>/ committed smoke sets: manifest.json, truth.json, ATTRIBUTION.csv, images/ (and masks/)
  attribution/      ATTRIBUTION-<use case>.csv for the full sets (committed)
  (use-cases/<use case>/assets/full.json is the committed copy of each full set's manifest, written by the builders)
  manifest.json     release asset names, sizes, sha256 and the sha256 of every smoke file (committed)
  sources.lock.json every downloaded URL with its sha256 and size (committed)
  .cache/           downloads and staging (ignored)
  dist/             <use case>-full.tar.gz, <use case>-full.files.json, manifest.json, sheets/ (ignored)
```

A full tarball unpacks to the same layout as a smoke directory: `manifest.json`, `truth.json`, `ATTRIBUTION.csv`,
`images/` (and `masks/`). The smoke set is a strict subset of the full set (same ids, same bytes).

## Builder commands

Run from the repository root. `uv` installs the dependencies declared in `datasets/pyproject.toml`
(Pillow, numpy, pyarrow, reportlab, pypdfium2).

```
uv run --project datasets python -m datasets.build                 # build 01, 02, 03, 05, 06 and write manifest.json
uv run --project datasets python -m datasets.build --only 01 05    # a subset
uv run --project datasets python -m datasets.build --verify        # rebuild from cache, compare with manifest.json
uv run --project datasets python -m datasets.build.openimages 01 02 05 06
uv run --project datasets python -m datasets.build.cord [--invoices-only]
uv run --project datasets python -m datasets.build.near_duplicates --help
uv run --project datasets python -m datasets.generators.invoices --help
uv run --project datasets python -m datasets.build.documents      # 04 alone (also what `--only 04` runs)
uv run --project datasets python -m datasets.generators.documents --help
uv run --project datasets python -m datasets.build.sheets          # contact sheets into dist/sheets/
uv run --project datasets python -m datasets.build.repo_manifests [--check]   # use-cases/*/assets/full.json from the archives in dist/
```

`--verify` rebuilds into `dist/` and `smoke/`, fails if a cached download no longer matches `sources.lock.json`, if a
rebuilt tarball or smoke file differs from the committed `manifest.json`, or if a smoke file has no attribution row.
Downloads (about 700 MB in total, dominated by the CORD parquet file and the mask archives) happen only for files
missing from `.cache/`, with 4 parallel connections and the User-Agent `foldbun-cookbook-datasets/1.0`.

## Conventions shared by all files

- JSON, UTF-8, ids are strings, image paths are relative to the set root (`images/<id>.jpg`).
- `manifest.json` (the input the implementations read) never contains ground truth. `truth.json` (read only by the
  evaluators) never contains anything an implementation needs.
- Boxes are `[x1, y1, x2, y2]` in pixels of the stored image. Images are JPEG; Open Images photos have a long side of at
  most 1024 px, CORD receipts at most 1300 px, document pages (04) 1754 px.
- The harness `--limit N` takes the first N manifest items.

## `manifest.json`

```json
{ "usecase": "01-dataset-prelabelling", "version": 1, "set": "smoke" | "full",
  "items": [ { "id": "...", "image": "images/<id>.jpg", ...use-case parameters } ],
  ...use-case top-level fields }
```

| Use case | Item parameters | Top-level fields |
|---|---|---|
| 01 | `prompts`: the four text prompts, `["dog","cat","bird","boat"]` | `classes`: `[{name, prompt}]` |
| 02 | `label`: lower-case class name of the target object (display only); `maxClicks` (committed full manifest only, 10): the click limit of the protocol. `id` is `<imageId>-<boxId>`; several items never share an image | none |
| 03 | none (`id` is `cord-NNN` or `inv-NNN`) | none |
| 04 | `document`: id of the document the page belongs to (`d01`; `d01-photo` for the photographed copy in the full set); `page`: page number in that document | none |
| 05 | `kind`: `"original"` or `"variant"` | `queries`: `[{id, text}]`, text is `a photo of a <class>` |
| 06 | none (`id` is the Open Images image id) | none |

## `truth.json`

Common: `{ "usecase", "set", "source", ...documentation strings, "items": { "<id>": {...} } }`. The smoke file holds
only the smoke ids (and, for 05, only the smoke queries and pairs).

**01** `items[id] = { width, height, classes: { "Dog": { verified, boxes: [ { bbox, oi_class, occluded, truncated } ] }, "Cat": {...}, "Bird": {...}, "Boat": {...} } }`.
`verified` is `1` when the class is present (boxes listed), `0` when humans verified it absent, `null` when nobody
verified it: score precision only on classes with `verified` 0 or 1 (the Open Images rule). Bird boxes include its
sub-classes (`oi_class` names the actual class). Images contain exactly one of the four classes; no group boxes.

**02** `items[id] = { label, mask, width, height, bbox, area_fraction, oi_mask_path }`. `mask` is a PNG (8-bit,
255 = object, 0 = background) of the same size as the image, relative to the set root. `bbox` is the tight box of the
mask.

**03** `items[id]` for CORD receipts (`kind: "cord"`): `{ width, height, source_index, currency: "IDR", vendor: null,
date: null, line_items: [ { description, quantity, amount, amount_text } ], line_item_count, subtotal, discount,
service, tax, total, total_text, raw }`. Amounts are integers parsed from the printed text (`amount_text`, `total_text`
keep the original string, e.g. `"45,500"` or `"1.250.000"`); a field the receipt does not show is `null`; `raw` is
CORD's unmodified `gt_parse`. CORD has no date or vendor field. For synthetic invoices (`kind: "clean"` or
`"photographed"`): `{ width, height, vendor, customer, invoice_number, date, due_date (ISO 8601), currency, number_format
("en" = 1,234.50, "eu" = 1.234,50), total_label (the printed word next to the total), line_items: [ { description,
quantity, unit_price, amount } ], line_item_count, subtotal, discount, tax_rate_percent, tax, total }`, numbers with
two decimals, internally consistent (items add up to the subtotal; subtotal - discount + tax = total). Photographed
invoices are inv-011 to inv-020.

**04** `items[id] = { document, page, variant ("clean" or "photographed"), twin (id of the other variant of the same page), width, height, blocks, markdown }`. `blocks` are the page's layout blocks in reading order: `{ order, type, text, bbox, level?, cells?, latex? }` with `type` one of `header`, `title`, `text`, `caption`, `table`, `formula`, `footer`; `level` (1 or 2) on titles; `cells` (matrix of strings) on tables, whose `text` is the same table as HTML; `latex` on formulas, whose `text` is `$$latex$$`. `bbox` is in pixels of the stored image; for a photographed page it is the clean page's box carried through the same perspective and rotation (its enclosing rectangle). `markdown` is the page as Markdown, blocks joined by a blank line. Ids are `dNN-pK` (clean) and `dNN-photo-pK`; the smoke manifest puts `d01-p2` and `d01-photo-p3` in one document, `d01`. Pages are 1240 x 1754 px (A4 at 150 dpi); the long-side limit of 1024 px used for photos does not suit text pages.

**05** `items[id] = { width, height, labels: [names], split: "a" | "b" }` for originals and variants (a variant inherits
the labels and the split of its source). `labels` are the human-verified positive image-level labels. Plus:
`queries: [ { id, text, class, relevant: [original ids] } ]` (relevant = originals carrying the verified label; every
other original is non-relevant; variants are never relevant) and `near_duplicates: [ { variant, source, recipe,
transforms: [ {op, ...parameters}, {op: "save", format, quality, size} ], split } ]`. Each variant has exactly one
source; any other pair of images is a non-duplicate. Use `split` to choose a threshold on one half (`a`) and evaluate
on the other (`b`). Recipes (10 variants each in the full set): resize, crop, jpeg, color, flip, watermark,
resize+jpeg, crop+color, flip+jpeg, crop+resize.

**06** `items[id] = { family, kind, oi_classes, instances, mask, width, height, bbox, area_fraction }`. `mask` is an 8-bit PNG
(255 = product) of the stored image size. When an image carries several instances, the mask is the **union** of every
instance mask of the family's Open Images classes on that image (`instances` counts them); the truth is the whole
product(s), because the endpoint cuts out everything salient. `family` is Cup (Coffee cup, Mug), Bowl (Bowl, Mixing
bowl), Bottle, Camera, Toy, Guitar, Chest of drawers, Vase or Cookware (Teapot, Kettle, Wok, Frying pan, Jug, Pitcher,
cookers) or Appliance (Microwave oven, Printer, Washing machine, Food processor, Toaster). `kind` marks the hard cases
for the quality rules: `normal` (one centred product), `truncated` (the mask touches the frame edge), `multi` (two or
more instances) and `thin` (mask fills less than 45 percent of its bounding box: necks, handles, legs).

## `ATTRIBUTION.csv`

Columns: `file, id, title, author, author_url, source_page_url, licence, licence_url, original_url, modified, notes`.
One row per committed image and mask; `file` matches the path in the manifest.

## `dist/manifest.json` and `manifest.json`

`manifest.json` (committed): `{ release: "datasets-v1", sets: { "<use case>": { file, bytes, sha256, files, smoke: { "<path>": "<sha256>" } } } }`.
`dist/manifest.json` (release asset, not committed) additionally lists every file in each tarball:
`sets[uc].contents = [ { path, bytes, sha256 } ]`. The harness downloads `file`, checks `sha256`, and unpacks it.

## Selection notes

- Open Images: validation split, CC BY 2.0 with an author, rotation 0, and no excluded content (see LICENSES.md);
  seeded; one class per image for 01. The design table lists Person and Car for 01; both are excluded by the
  no-people / no-plates rule, so the classes are Dog, Cat, Bird, Boat.
- 02: masks with a mask-to-box ratio of at least 0.45 and 5 to 60 percent of the image, one object per image, at most
  three per class, never a group or truncated box.
- 05: 40 queries from a fixed class list, 10 originals sampled per query class, the remaining 40 originals random.
- 06: uses the `product` profile of `oi_meta.build_pool`. Product-like classes that 01 / 02 / 05 exclude (bottles, bags,
  watches, toys, cameras, instruments ...: `PRODUCT_ALLOWED` in `oi_exclusions.py`) are allowed again, but person, body-part,
  face, plate, clothing-on-body and text-like classes, depictions and every machine label of an excluded class with
  confidence >= 0.2 still exclude the image, so no image has any person-related annotation or prediction. 01 / 02 / 05
  use the default profile and are unchanged (their archives are byte-identical). Selection: one product family per
  image (no other family boxed at 4 percent of the frame or more), 28 `normal` (centred, 5 to 65 percent of the frame),
  4 `truncated`, 4 `multi`, 4 `thin`; families with too few clean candidates are topped up from the others (at most 5
  each). The 3 smoke images are 2 `normal` (Cup, Bottle) and 1 `truncated`. Images rejected in the manual review of the
  contact sheets are in `REJECT_06` (about 70 images, with reasons: brand names or logos printed large, watermarks,
  legible text, people or reflections of people, collages, scenes that are not product photos, objects outside the mask).
- Images dropped after looking at the contact sheets are listed with a reason in `REJECT` (`openimages.py`).
  Images removed in a later review are listed in `REMOVED` and receipts in `EXCLUDE_INDEX` / the exclusion rules of `cord.py`;
  they are applied after the seeded selection, so every other image keeps its place and each removed one is replaced by
  the next eligible candidate (receipts keep their `cord-NNN` number, variants their `dup-NNN` number).
