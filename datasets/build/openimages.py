"""Open Images V7 (validation split) subsets for use cases 01, 02, 05 and 06.

  uv run --project datasets python -m datasets.build.openimages --help
  uv run --project datasets python -m datasets.build.openimages 01 02 05

01  pre-labelling      60 images, 4 classes (Dog, Cat, Bird, Boat), box ground truth
02  click-to-segment   30 objects with mask ground truth
05  image-library      400 images + 100 generated near-duplicates, image-level labels as retrieval truth
06  product photos     40 product-like objects (cups, vases, teapots ...) with mask ground truth

Selection is person-free (see oi_meta.build_pool and oi_exclusions) and seeded. Images come from the public
Open Images bucket (one request each, 4 workers, custom User-Agent), are cached under datasets/.cache/ and are
re-encoded to a long side of at most 1024 px (JPEG q90).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import random
import shutil
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

from . import near_duplicates
from .common import CACHE, CHANGE_NOTE, SMOKE, USE_CASES, WORK, emit_set, fetch, fetch_many, reencode, write_attribution
from .oi_meta import IMAGE_URL, MASK_ZIP_URL, OI_DIR, build_pool, attribution_row, download_meta, _rows

SEED = 20261112
SOURCE_NOTE = "Open Images Dataset V7 (Google LLC): annotations CC BY 4.0; image CC BY 2.0 by the author listed"
UC01, UC02, UC05 = "01-dataset-prelabelling", "02-click-to-segment", "05-image-library-search"
UC06 = "06-product-photo-pipeline"

# 01 classes (the design table lists Person and Car; both are excluded by the no-people / no-plates rule, so Cat and
# Bird take their place, and Boat replaces Bicycle, which has too few clean images): name -> (prompt, Open Images box classes that count as this class)
CLASSES_01 = {
    "Dog": ("dog", ["Dog"]),
    "Cat": ("cat", ["Cat"]),
    "Bird": ("bird", ["Bird", "Swan", "Duck", "Goose", "Eagle", "Owl", "Parrot", "Sparrow", "Magpie", "Woodpecker",
                      "Falcon", "Raven", "Blue jay", "Canary", "Ostrich", "Penguin", "Chicken", "Turkey"]),
    "Boat": ("boat", ["Boat"]),
}
# Images removed after looking at the contact sheets (id -> reason). Kept in code so the build stays reproducible.
REJECT = {
    "c7e73523a8c9c0d3": "caption text and a website address overlaid on the photo",
    "e00a4c661e47760c": "photographer watermark in the corner",
    "f0c84adfd36023b3": "mislabelled: monkeys annotated as Dog",
    "8ad441bdf8795cf7": "collage of two photographs",
    "f6b7d76234a7d471": "large readable text on the lifebuoy dominates the image",
    "a6c34c3add62f6ee": "national flags visible on the masts",
    "adf9a2c313b2e6a7": "legible ship lettering as a main element",
    "4d57648cbc715f17": "passengers and signage visible on the boat",
    "8d410f163435580c": "website watermark text in the image",
    "c1e61b37a07c748f": "shop name printed on the bag is a main element",
    "78009ddd7e091570": "mask target ambiguous (cat vs. fabric)",
    "c63e3f6b39056c18": "brand packaging is the subject",
    "a8d5753dfeeabb1a": "company watermark",
    "0b5d64088922d6c9": "photographer watermark with e-mail address",
    "753775903ce24ebe": "web address watermark",
    "d7f8808f2cec6344": "photo title contains a word the repository string scan forbids",
}
# Images removed from the published sets after a later review (id -> reason). Unlike REJECT, these are applied after the
# seeded selection, so every other image keeps its place; each one is replaced by the next eligible candidate of the same
# class / topic (see take_one and the replacement loops in build_01, build_02 and build_05).
REMOVED = {
    "83e27a547db64cdb": "text overlay that is not part of the scene",
    "56ec26cdac862252": "people who could be recognised or groups of people",
    "aef1488bedfc51c4": "people who could be recognised or groups of people",
    "bab517e510b0ba53": "personal name written in the picture",
    "14e47962c09f20ef": "personal name written in the picture",
    "bc98cf15585b0fb1": "watermark or signature in the frame",
    "6e58d8b2c269091b": "watermark or signature in the frame",
    "b066b8d2df3508ca": "watermark or signature in the frame",
    "c3516f9019c14498": "watermark or signature in the frame",
    "4f9602b9c4af0af3": "watermark or signature in the frame",
    "0ab050b51e78acdb": "watermark or signature in the frame",
    "a60adec7b6f984e2": "watermark or signature in the frame",
    "ef6d4a155ae3285e": "watermark or signature in the frame",
    "5aced50a2c10b20f": "not a photograph",
    "9f20972e0bd6050b": "not a photograph",
    "7bb2ced75a4c57e9": "watermark overlay",
    "c9e4987877b7b804": "many people in the frame",
    "946e667760c0be6d": "many people in the frame",
    "daefb1511511f2a3": "watermark or signature in the frame",
    # replacement candidates that did not pass the same review
    "45196f882bcba075": "many people in the frame",
    "871b22db411ccf29": "people and flags in the frame",
    "66c23cb3a1a469f8": "flag and lettering in the frame",
    "9fed7e5d6d9a3e3b": "inscription in the frame",
    "112a8fc3f02f4eb0": "many people in the frame",
    "80729ff4443dde3a": "many people in the frame",
    "424fa792da98deb9": "legible lettering in the frame",
    "9750e3e8236e289d": "legible lettering in the frame",
    "cf35d728dd901f36": "several people in the frame",
    "106ec497581aa54c": "legible lettering in the frame",
    "fb182ea71db4588d": "flag and lettering in the frame",
    "76274b9b97391709": "flag in the frame",
    "6e8138f7e9ced8a7": "legible lettering in the frame",
    "72bf14fe07c416a6": "legible lettering in the frame",
    "e35af811e2e1ce6a": "military vessel with hull number",
    "0fba8a6fd9e7118d": "company name in the frame",
    "6dc4ffdc568cabf7": "legible lettering in the frame",
    "05eeba1be80d74fc": "several people and a flag in the frame",
    "ea8dcc98240e46ed": "legible lettering in the frame",
    "0f630da273fe8c15": "legible lettering in the frame",
    "cc32d04829f7e344": "text overlay that is not part of the scene",
    "1c6debbfd4482013": "military vessel",
    "f396780361228226": "legible lettering in the frame",
}
# 05 text queries: candidate class names (the first 40 with enough eligible positives are used, in seeded order)
QUERY_CANDIDATES = """Dog;Cat;Bird;Flower;Tree;Boat;Fish;Cake;Bread;Pizza;Salad;Fruit;Vegetable;Mushroom;Houseplant;Butterfly;
Horse;Cattle;Sheep;Monkey;Fixed-wing aircraft;Mountain;Bridge;Lighthouse;Castle;Tower;House;Chair;Vase;Coffee cup;Bowl;
Ice cream;Strawberry;Tomato;Lemon (plant);Orange (fruit);Apple;Sunset;Reef;Soup;Cheese;Lizard;Snake;Frog;Crab;Shark;Whale;
Dolphin;Duck;Parrot;Deer;Fox;Squirrel;Rabbit;Elephant;Giraffe;Zebra;Lion;Tiger;Bear;Owl;Eagle;Insect;Spider;Beetle;Bee;
Pumpkin;Cucumber;Pasta;Sandwich;Hamburger;Doughnut;Cookie;Muffin;Coffee;Tea;Candle;Lantern;Clock;Bench;Fountain;Windmill""".replace("\n", "").split(";")


def px(box, w, h):
    return [round(box[0] * w, 1), round(box[1] * h, 1), round(box[2] * w, 1), round(box[3] * h, 1)]


def cached_image(i: str) -> Path:
    return fetch(IMAGE_URL.format(id=i), OI_DIR / "images" / f"{i}.jpg", pause=0.05)


def take_downloadable(cands: list[str], n: int, min_side: int = 640) -> list[str]:
    """Walk the (already shuffled) candidates, downloading in parallel batches, until n usable images."""
    got, pos = [], 0
    while len(got) < n and pos < len(cands):
        batch = cands[pos: pos + max(8, n - len(got))]
        pos += len(batch)
        ok = []
        def one(i):
            try:
                return i, cached_image(i)
            except Exception as e:                      # 404 / network: skip this candidate
                print("skip", i, e)
                return i, None
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(4) as ex:
            for i, p in ex.map(one, batch):
                if p is None:
                    continue
                if max(Image.open(p).size) < min_side:
                    continue
                ok.append(i)
        got += ok
    return got[:n]


def take_one(cands: list[str], min_side: int = 640) -> str:
    """The first candidate that downloads and is large enough (one request at a time; used for replacements)."""
    for i in cands:
        if i in REMOVED:
            continue
        try:
            p = cached_image(i)
        except Exception as e:
            print("skip", i, e)
            continue
        if max(Image.open(p).size) >= min_side:
            return i
    raise RuntimeError("no replacement candidate left")


def stage(uc: str) -> Path:
    d = WORK / uc
    if d.exists():
        shutil.rmtree(d)
    (d / "images").mkdir(parents=True)
    return d


def write_images(ids: list[str], pool, st: Path) -> dict:
    size, rows = {}, []
    for i in ids:
        w, h = reencode(cached_image(i), st / "images" / f"{i}.jpg")
        size[i] = (w, h)
    return size


# ------------------------------------------------------------------ 01
def build_01() -> None:
    pool = build_pool()
    rng = random.Random(SEED + 1)
    group_mids = {c: {pool.mid(n) for n in names} for c, (_, names) in CLASSES_01.items()}
    parent_mid = {c: pool.mid(c) for c in CLASSES_01}
    cand, n_inst_of = collections.defaultdict(list), {}
    for i in sorted(set(pool.imgs) - set(REJECT)):
        present = {}
        for c, mids in group_mids.items():
            bs = [r for r in pool.boxes[i] if r["LabelName"] in mids]
            if bs:
                present[c] = bs
        if len(present) != 1:                       # one class per image keeps the class counts clean
            continue
        c, bs = next(iter(present.items()))
        areas = [(float(r["XMax"]) - float(r["XMin"])) * (float(r["YMax"]) - float(r["YMin"])) for r in bs]
        n_inst = sum(1 for r in bs if r["IsGroupOf"] != "1")
        if max(areas) < 0.03 or n_inst == 0 or n_inst > 6 or any(r["IsGroupOf"] == "1" for r in bs):
            continue
        # the other classes should be human-verified absent on this image, so false positives can be scored
        others = [c2 for c2 in CLASSES_01 if c2 != c]
        verified_absent = sum(1 for c2 in others if pool.human[i].get(parent_mid[c2]) == 0)
        cand[c].append((-verified_absent, rng.random(), i, n_inst))
        n_inst_of[i] = n_inst
    chosen, orders = {}, {}
    for c in CLASSES_01:
        lst = sorted(cand[c])
        multi = [t for t in lst if t[3] >= 3][:4]
        rest = [t for t in lst if t not in multi]
        orders[c] = [t[2] for t in multi + rest]
        chosen[c] = take_downloadable(orders[c], 15)
        print(UC01, c, "candidates", len(lst), "chosen", len(chosen[c]))
    # the smoke images come from the selection as seeded; the replacements below never touch them
    smoke_ids = [next(i for i in chosen[c] if n_inst_of[i] <= 2) for c in CLASSES_01]
    for c in CLASSES_01:                            # replace removed images in place, next candidate of the same class
        last = max(orders[c].index(i) for i in chosen[c])
        for k, i in enumerate(chosen[c]):
            if i in REMOVED:
                assert i not in smoke_ids, i
                tail = [j for j in orders[c][last + 1:] if j not in chosen[c]]
                chosen[c][k] = take_one(tail)
                print(UC01, c, "replaced", i, "->", chosen[c][k])
    ids = [i for c in CLASSES_01 for i in chosen[c]]
    st = stage(UC01)
    size = write_images(ids, pool, st)
    items, truth_items, attr = [], {}, []
    prompts = [CLASSES_01[c][0] for c in CLASSES_01]
    for c in CLASSES_01:
        for i in chosen[c]:
            w, h = size[i]
            items.append({"id": i, "image": f"images/{i}.jpg", "prompts": prompts})
            t = {"width": w, "height": h, "classes": {}}
            for c2, (_, _) in CLASSES_01.items():
                bs = [r for r in pool.boxes[i] if r["LabelName"] in group_mids[c2]]
                ver = 1 if bs else pool.human[i].get(parent_mid[c2])
                t["classes"][c2] = {"verified": ver, "boxes": [
                    {"bbox": px((float(r["XMin"]), float(r["YMin"]), float(r["XMax"]), float(r["YMax"])), w, h),
                     "oi_class": pool.cls[r["LabelName"]], "occluded": r["IsOccluded"] == "1",
                     "truncated": r["IsTruncated"] == "1"} for r in bs]}
            truth_items[i] = t
            attr.append(attribution_row(pool.imgs[i], f"images/{i}.jpg", CHANGE_NOTE))
    head = {"classes": [{"name": c, "prompt": CLASSES_01[c][0]} for c in CLASSES_01]}
    truth = {"source": SOURCE_NOTE, "bbox_format": "xyxy pixels in the stored image",
             "verified": "1 = class present (boxes), 0 = human-verified absent, null = not verified (do not count false positives)",
             "items": truth_items}
    smoke_files = [f"images/{i}.jpg" for i in smoke_ids]
    sm_items = [it for it in items if it["id"] in smoke_ids]
    emit_set(UC01, st, items=items, truth=truth, attribution=attr, smoke_items=sm_items,
             smoke_truth={**{k: v for k, v in truth.items() if k != "items"},
                          "items": {i: truth_items[i] for i in smoke_ids}},
             smoke_files=smoke_files, extra=head)


# ------------------------------------------------------------------ 02
def mask_zip(c: str) -> zipfile.ZipFile:
    return zipfile.ZipFile(fetch(MASK_ZIP_URL.format(c=c), OI_DIR / "masks" / f"validation-masks-{c}.zip"))


def build_02() -> None:
    pool = build_pool()
    rng = random.Random(SEED + 2)
    segs = [r for r in _rows("segmentation") if r["ImageID"] in pool.imgs and r["ImageID"] not in REJECT]
    zips = {}
    for c in "0123456789abcdef":
        zips[c] = mask_zip(c)
    rng.shuffle(segs)
    per_class, used_img, picked = collections.Counter(), set(), []
    for r in segs:
        i, name = r["ImageID"], pool.cls[r["LabelName"]]
        if i in used_img or per_class[name] >= 3:
            continue
        zf = zips[r["MaskPath"][0]]
        try:
            m = np.array(Image.open(io.BytesIO(zf.read(r["MaskPath"]))).convert("L")) > 127
        except KeyError:
            continue
        frac = m.mean()
        if not 0.05 <= frac <= 0.6:
            continue
        x0, x1, y0, y1 = (float(r[k]) for k in ("BoxXMin", "BoxXMax", "BoxYMin", "BoxYMax"))
        box_area = (x1 - x0) * (y1 - y0)
        if box_area <= 0 or frac / box_area < 0.45:          # skip thin or sparse shapes and multi-part masks
            continue
        # the matching bbox annotation must exist, be a single instance, not a depiction, not truncated
        bb = [b for b in pool.boxes[i] if b["LabelName"] == r["LabelName"]
              and abs(float(b["XMin"]) - x0) < 0.002 and abs(float(b["YMax"]) - y1) < 0.002]
        if not bb or bb[0]["IsGroupOf"] == "1" or bb[0]["IsTruncated"] == "1":
            continue
        picked.append((r, round(float(frac), 4)))
        used_img.add(i)
        per_class[name] += 1
        if len(picked) >= 400:
            break
    # the first 80 candidates are the pool of the seeded selection; the rest only serve as replacements
    reserve, picked = picked[80:], picked[:80]
    # downloadable images only; take 30
    order = [r["ImageID"] for r, _ in picked]
    ids = take_downloadable(order, 30)
    chosen = [(r, f) for r, f in picked if r["ImageID"] in set(ids)][:30]
    for k, (r0, _) in enumerate(chosen):            # replace removed images in place: next unused candidate, same class first
        if r0["ImageID"] in REMOVED:
            name0, taken = pool.cls[r0["LabelName"]], {r["ImageID"] for r, _ in chosen}
            per_label = collections.Counter(pool.cls[r["LabelName"]] for r, _ in chosen if r["ImageID"] not in REMOVED)
            tail = [(r, f) for r, f in picked + reserve if r["ImageID"] not in taken and r["ImageID"] not in REMOVED]
            same = [t for t in tail if pool.cls[t[0]["LabelName"]] == name0]
            other = [t for t in tail if per_label[pool.cls[t[0]["LabelName"]]] < 3]
            got = take_one([t[0]["ImageID"] for t in same or other])
            chosen[k] = next(t for t in tail if t[0]["ImageID"] == got)
            print(UC02, "replaced", r0["ImageID"], "->", got, pool.cls[chosen[k][0]["LabelName"]])
    st = stage(UC02)
    (st / "masks").mkdir()
    items, truth_items, attr = [], {}, []
    for r, frac in chosen:
        i, name = r["ImageID"], pool.cls[r["LabelName"]]
        w, h = reencode(cached_image(i), st / "images" / f"{i}.jpg")
        m = Image.open(io.BytesIO(zips[r["MaskPath"][0]].read(r["MaskPath"]))).convert("L").resize((w, h), Image.NEAREST)
        arr = (np.array(m) > 127).astype(np.uint8) * 255
        Image.fromarray(arr).save(st / "masks" / f"{i}-{r['BoxID']}.png", optimize=True)
        oid = f"{i}-{r['BoxID']}"
        ys, xs = np.nonzero(arr)
        items.append({"id": oid, "image": f"images/{i}.jpg", "label": name.lower()})
        truth_items[oid] = {"label": name, "mask": f"masks/{oid}.png", "width": w, "height": h,
                            "bbox": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
                            "area_fraction": round(float((arr > 0).mean()), 4), "oi_mask_path": r["MaskPath"]}
        attr.append(attribution_row(pool.imgs[i], f"images/{i}.jpg", CHANGE_NOTE))
        attr.append({**attribution_row(pool.imgs[i], f"masks/{oid}.png", "Mask resized (nearest) to the stored image size, binarised",
                                       "segmentation mask: CC BY 4.0, Google LLC"),
                     "licence": "CC BY 4.0", "licence_url": "https://creativecommons.org/licenses/by/4.0/",
                     "author": "Google LLC (Open Images Dataset V7)", "author_url": "https://storage.googleapis.com/openimages/web/index.html",
                     "title": f"mask of {name}", "source_page_url": "https://storage.googleapis.com/openimages/web/download_v7.html",
                     "original_url": MASK_ZIP_URL.format(c=r["MaskPath"][0]) + "#" + r["MaskPath"]})
    # one smoke object per label family, 3 total: first three with distinct labels and moderate size
    smoke_ids, seen = [], set()
    for oid, t in truth_items.items():
        if t["label"] not in seen and 0.1 <= t["area_fraction"] <= 0.45 and len(smoke_ids) < 3:
            seen.add(t["label"]); smoke_ids.append(oid)
    smoke_files = sorted({truth_items[o]["mask"] for o in smoke_ids} |
                         {next(it["image"] for it in items if it["id"] == o) for o in smoke_ids})
    truth = {"source": SOURCE_NOTE, "mask_format": "PNG, 8-bit, 255 = object, 0 = background, same size as the stored image",
             "items": truth_items}
    emit_set(UC02, st, items=items, truth=truth, attribution=attr,
             smoke_items=[it for it in items if it["id"] in smoke_ids],
             smoke_truth={**{k: v for k, v in truth.items() if k != "items"}, "items": {o: truth_items[o] for o in smoke_ids}},
             smoke_files=smoke_files)
    print(UC02, "objects", len(items), "classes", dict(collections.Counter(t["label"] for t in truth_items.values())))


# ------------------------------------------------------------------ 05
def build_05() -> None:
    pool = build_pool()
    rng = random.Random(SEED + 5)
    pos = collections.defaultdict(list)                     # class mid -> [image ids with human-verified positive]
    for i in sorted(set(pool.imgs) - set(REJECT)):
        for m, v in pool.human[i].items():
            if v == 1:
                pos[m].append(i)
    names = [n for n in QUERY_CANDIDATES if n in pool.name2mid and len(pos[pool.name2mid[n]]) >= 14]
    qnames = rng.sample(names, 40)
    qmids = [pool.name2mid[n] for n in qnames]
    selected, per_q, orders = [], {}, {}
    for n, m in zip(qnames, qmids):
        order = [i for i in pos[m] if i not in selected]
        rng.shuffle(order)
        # prefer images with a clean label set: few query classes at once
        order.sort(key=lambda i: sum(1 for q in qmids if pool.human[i].get(q) == 1))
        orders[n] = order
        sel = take_downloadable(order[:30], 10)
        per_q[n] = sel
        selected += sel
    others = [i for i in sorted(set(pool.imgs) - set(REJECT)) if i not in set(selected)]
    rng.shuffle(others)
    selected += take_downloadable(others, 400 - len(selected))
    ids = selected[:400]
    st = stage(UC05)
    write_images(ids, pool, st)
    # near-duplicates: 100 variants of 100 distinct sources (all 10 recipes, 10 each), seeded; made from the selection
    # as seeded (removed images included), so the random stream and every other variant stay as they were
    src_ids = rng.sample(ids, 100)
    made = []
    for k, sid in enumerate(src_ids):
        recipe = near_duplicates.RECIPES[k % len(near_duplicates.RECIPES)]
        made.append((sid, recipe) + near_duplicates.make_variant(Image.open(st / "images" / f"{sid}.jpg"), recipe, rng))
    # smoke selection from the seeded structures (no smoke image is among the removed ones)
    smoke_q = [(n, m) for n, m in zip(qnames, qmids) if n in ("Dog", "Cat", "Flower", "Boat", "Cake")][:5]
    if len(smoke_q) < 5:
        smoke_q = list(zip(qnames, qmids))[:5]
    smoke_orig = []
    for n, _ in smoke_q:
        pref = sorted((i for i in per_q[n] if i not in smoke_orig), key=lambda i: i not in set(src_ids))
        smoke_orig += pref[:4]
    assert not (set(smoke_orig) & set(REMOVED)), "a smoke image is among the removed ones"
    # replace removed originals in place: the next eligible positive of the same query class (or a random other)
    group_of = {i: n for n, sel in per_q.items() for i in sel}
    final = list(ids)
    for k, i in enumerate(ids):
        if i in REMOVED:
            pool_ids = orders[group_of[i]] if i in group_of else others
            final[k] = take_one([j for j in pool_ids if j not in set(final) and j not in set(ids)])
            print(UC05, "replaced", i, "->", final[k], group_of.get(i, "(random)"))
    for i in final:
        if i not in ids:
            reencode(cached_image(i), st / "images" / f"{i}.jpg")
    # variants whose source was removed are regenerated from another original (not a source yet), own seeded stream
    taken_src = {sid for sid, *_ in made}
    for k, (sid, recipe, _, _) in enumerate(made):
        if sid in REMOVED:
            r2 = random.Random(SEED + 5000 + k)
            new = r2.choice(sorted(i for i in final if i not in taken_src and i not in REMOVED))
            taken_src.add(new)
            made[k] = (new, recipe) + near_duplicates.make_variant(Image.open(st / "images" / f"{new}.jpg"), recipe, r2)
            print(UC05, f"dup-{k + 1:03d}", "regenerated from", new, "(was", sid + ")")
    size = {i: Image.open(st / "images" / f"{i}.jpg").size for i in final}
    for i in ids:
        if i in REMOVED:
            (st / "images" / f"{i}.jpg").unlink()
    ids = final
    items, truth_items, attr = [], {}, []
    for i in ids:
        items.append({"id": i, "image": f"images/{i}.jpg", "kind": "original"})
        labs = sorted(pool.cls[m] for m, v in pool.human[i].items() if v == 1)
        split = "a" if int(hashlib.sha256(i.encode()).hexdigest(), 16) % 2 == 0 else "b"
        truth_items[i] = {"width": size[i][0], "height": size[i][1], "labels": labs, "split": split}
        attr.append(attribution_row(pool.imgs[i], f"images/{i}.jpg", CHANGE_NOTE))
    # queries: relevant = images among the 400 that carry the human-verified positive label
    queries = []
    for k, (n, m) in enumerate(zip(qnames, qmids), 1):
        queries.append({"id": f"q{k:02d}", "text": f"a photo of a {n.lower().replace(' (plant)', '').replace(' (fruit)', '')}",
                        "class": n, "relevant": sorted(i for i in ids if pool.human[i].get(m) == 1)})
    dups = []
    for k, (sid, recipe, data, tf) in enumerate(made):
        did = f"dup-{k + 1:03d}"
        (st / "images" / f"{did}.jpg").write_bytes(data)
        w, h = Image.open(io.BytesIO(data)).size
        items.append({"id": did, "image": f"images/{did}.jpg", "kind": "variant"})
        truth_items[did] = {"width": w, "height": h, "labels": truth_items[sid]["labels"], "split": truth_items[sid]["split"]}
        dups.append({"variant": did, "source": sid, "recipe": "+".join(recipe), "transforms": tf, "split": truth_items[sid]["split"]})
        attr.append(attribution_row(pool.imgs[sid], f"images/{did}.jpg",
                                    "Derived from the source image (" + sid + "): " + ", ".join(recipe) + "; " + CHANGE_NOTE,
                                    "near-duplicate variant generated by datasets/build/near_duplicates.py (seeded)"))
    truth = {"source": SOURCE_NOTE,
             "relevance": "queries[].relevant lists original image ids carrying the human-verified positive image-level label "
                          "(Open Images); all other originals count as not relevant. Variants are never relevant to queries.",
             "near_duplicates": dups, "queries": queries, "items": truth_items}
    # smoke: 5 classes x 4 originals (all relevant to their query) + 5 variants of 5 of those + the 5 queries
    smoke_qs = [q for q in queries if q["class"] in {n for n, _ in smoke_q}]
    smoke_dups = [d for d in dups if d["source"] in smoke_orig][:5]
    if len(smoke_dups) < 5:                                  # pad with variants whose source we add to the smoke set
        for d in dups:
            if d not in smoke_dups and len(smoke_dups) < 5 and len(smoke_orig) < 25:
                smoke_dups.append(d); smoke_orig.append(d["source"]) if d["source"] not in smoke_orig else None
    sm_ids = smoke_orig + [d["variant"] for d in smoke_dups]
    sm_queries = [{**q, "relevant": [i for i in q["relevant"] if i in set(smoke_orig)]} for q in smoke_qs]
    sm_truth = {**{k: v for k, v in truth.items() if k != "items"}, "queries": sm_queries,
                "near_duplicates": smoke_dups, "items": {i: truth_items[i] for i in sm_ids}}
    emit_set(UC05, st, items=items, truth=truth, attribution=attr,
             smoke_items=[it for it in items if it["id"] in set(sm_ids)], smoke_truth=sm_truth,
             smoke_files=[f"images/{i}.jpg" for i in sm_ids], extra={"queries": [{"id": q["id"], "text": q["text"]} for q in queries]})
    # the smoke manifest only carries the smoke queries
    mp = Path(__file__).resolve().parent.parent / "smoke" / UC05 / "manifest.json"
    mf = json.loads(mp.read_text())
    mf["queries"] = [{"id": q["id"], "text": q["text"]} for q in sm_queries]
    mp.write_text(json.dumps(mf, indent=1, ensure_ascii=False) + "\n")
    print(UC05, "originals 400, variants", len(dups), "queries", len(queries), "smoke", len(sm_ids))


# ------------------------------------------------------------------ 06
# Product-like objects with an Open Images instance mask. The pool is the "product" profile of oi_meta.build_pool: the
# classes that 01 / 02 / 05 exclude (bottles, bags, watches ...) are allowed again, but an image still qualifies only when no
# person, body part, face, plate, text-like class or depiction is annotated on it or predicted with confidence >= 0.2.
# family -> Open Images segmentation classes whose masks are merged into the ground truth of the image
FAMILIES_06 = {
    "Cup": ["Coffee cup", "Mug"], "Vase": ["Vase"], "Bowl": ["Bowl", "Mixing bowl"], "Bottle": ["Bottle"],
    "Camera": ["Camera"], "Toy": ["Toy"], "Guitar": ["Guitar"], "Chest of drawers": ["Chest of drawers"],
    "Cookware": ["Teapot", "Kettle", "Wok", "Frying pan", "Jug", "Pitcher (Container)", "Pressure cooker", "Slow cooker"],
    "Appliance": ["Microwave oven", "Printer", "Washing machine", "Food processor", "Toaster"],
}
NORMAL_06 = {"Cup": 4, "Vase": 2, "Bowl": 3, "Bottle": 3, "Camera": 3, "Toy": 3, "Guitar": 2, "Chest of drawers": 2,
             "Cookware": 3, "Appliance": 3}   # centred, one product
NORMAL_CAP = 5
HARD_06 = {"truncated": 4, "multi": 4, "thin": 4}          # deliberate hard cases for the quality rules, at most 2 per family
BORDER_FRAC = 0.01                                          # outer band of the image that counts as "touching the edge"
# Images dropped after looking at the contact sheets (id -> reason). Applied before the seeded take, so each one is
# replaced by the next candidate of the same slot and every other image keeps its place.
REJECT_06 = {
    "e1ffa13fb120c12b": "brand name printed large on the cup",
    "fdf90530521378ad": "hanging composition: the mask covers only one of several objects",
    "7045280ce83f80ea": "brand name and packaging text dominate",
    "a4ce8204dc010b24": "blog address watermark and caption text; two products, one masked",
    "894c46aa0d7b512c": "a person's legs are visible",
    "877dc9d3de07e4b2": "not a photograph (technical drawing)",
    "6b314080cc535053": "large brand logo on the bottle",
    "d133c517c7947101": "legible sign text in the frame",
    "e68794b06f205792": "flowers in the vase are outside the mask",
    "c4b9c0722eefbfdc": "flowers in the vase are outside the mask",
    "af8494836e6adc45": "flowers in the vase are outside the mask",
    "e10a4758270ac022": "museum plaque with legible text in the frame",
    "e3f5c26de2e5735f": "dark shape at the edge that may be a person",
    "d973420311b00730": "brand name printed large on the bottle",
    "910796da8a49348e": "company watermark in the corner",
    "96c6f29db05a8368": "brand name and model text on the packaging dominate; the masked part is an accessory",
    "b74e6caff7b0229a": "a second object (lamp) is outside the mask",
    "ed7d3e6b28a06e77": "cooking scene, not a product photo",
    "b10730442db3217a": "kitchen interior; the product is a small part of the frame",
    "f286d9d21ea84aac": "legible printed text in the frame",
    "dcaf07749bc94f41": "photographer watermark in the corner",
    "81cc3a779b90e177": "several objects and a plant stem outside the mask",
    "25275432b3ec6f9b": "two products in the frame, only one is masked",
    "aa81ff2c072c62f7": "museum placard with legible text in the frame",
    "94546d65a40d0a9a": "part of a person may be visible at the top edge",
    "05544d84d21af1c1": "brand name and lens engraving are the dominant detail",
    "79bcd5c3550d9e65": "possible person at the edge of the frame and lettering on the object",
    "88e4a839c0ff5fc6": "other objects (whisk, tin) are outside the mask",
    "8d279daa12ffc691": "brand name printed large on the cup",
    "9ba8af4d3eabf601": "camera rig with tripod and monitor, the mask covers only the camera body",
    "a3aade02e5418967": "sweets outside the mask are part of the composition",
    "dd42197939e82d8c": "not a photograph (rendered illustration)",
    "d74ceb78372a9694": "website watermark in the corner",
    "d6ea15e0a1f1fd0b": "handwritten museum labels in the frame",
    "85c4debd240770d0": "brand name and lens engraving fill the frame",
    "8f5244eec98887a6": "engraved inscription with names is legible",
    "cd4e964d17223b05": "label text on the bottle is legible and offensive",
    "758f3553177b6e69": "lenses next to the camera are outside the mask",
    "a1792883a6ba5d4c": "camera rig with tripod and monitor, the mask covers only the camera body",
    "6142a6f0fdcaf904": "oven interior with food, not a product photo",
    "340d7d29dcbe278d": "food fills the frame, not a product photo",
    "9fe7c70d327b0111": "brand name printed large on the bottles",
    "8e0cc5199a0ee659": "photographer watermark in the corner and brand name on the lens cap",
    "a7ec5be4f77a65a8": "brand name printed large on the housing",
    "678d43310d14d676": "text overlay that is not part of the scene",
    "37052661d85d40b2": "collage with captions that name people",
    "c88aa18f9fffa58d": "large printed word on the mug",
    "a32e3afe08e4a238": "business name printed large on the mug",
    "af97d848a0c85289": "web address printed on the tripod plate",
    "ee515c9191f83f06": "branded packaging fills the frame",
    "fbdf5daeb9cc7bdd": "kitchen interior, not a product photo",
    "fbce19c2567a7240": "large logo lettering on the guitar",
    "f4721d94d13eebf7": "food fills the frame, not a product photo",
    "274b93016f1073da": "a person is reflected in the teapot",
    "680742e474d75e29": "cooking hob scene, not a product photo",
    "77153a3efa75f4a4": "photographer watermark and printed number",
    "914ea0ebfffad164": "kitchen interior, not a product photo",
    "b73597a889663f1c": "brand name printed large on the bottle",
    "a2d8194362af5a99": "collage of four photographs",
    "42fdbf5f7c05f39d": "kitchen interior, not a product photo",
    "93f18c33e62ccee9": "game machine with on-screen text, not a product photo",
    "6bc56f8366132974": "other objects (pineapple, lemon) outside the mask",
    "be6a86c753495f3a": "laundry room interior, the product is a small part of the frame",
    "cd9af78d9354f152": "legible book titles in the frame",
    "43197f53dc624b26": "business logo printed on the cup and saucer",
    "600b65f391e6cf70": "brand lettering on the strap and the camera is the dominant detail",
    "56578e312e04853a": "branded packaging with large lettering",
    "d3d5fbcb4c708d11": "blog watermark; food fills the frame",
    "daa88aa68ff91d62": "steaming pot outdoors, a cooking scene rather than a product photo",
    "e144287d1ff5bce7": "company logo printed on the toys",
    "598ce21cc779ff81": "kitchen interior; the mask follows a stove front and towels, not a product",
    "f2f0c6170f6935a7": "street scene with discarded furniture and a house, not a product photo",
}


def _union_mask(rows, zips):
    acc = None
    for r in rows:
        m = np.array(Image.open(io.BytesIO(zips[r["MaskPath"][0]].read(r["MaskPath"]))).convert("L")) > 127
        acc = m if acc is None else (acc | m)
    return acc


def _features_06(m: np.ndarray) -> dict:
    h, w = m.shape
    ys, xs = np.nonzero(m)
    if not len(xs):
        return {}
    bx, by = max(1, round(w * BORDER_FRAC)), max(1, round(h * BORDER_FRAC))
    sides = [m[:by].any(0).mean(), m[-by:].any(0).mean(), m[:, :bx].any(1).mean(), m[:, -bx:].any(1).mean()]
    box = (xs.max() + 1 - xs.min()) * (ys.max() + 1 - ys.min())
    return {"frac": float(m.mean()), "solidity": float(m.sum() / box), "touch": float(max(sides)),
            "cx": float((xs.min() + xs.max()) / 2 / w), "cy": float((ys.min() + ys.max()) / 2 / h)}


def candidates_06(pool, zips) -> dict:
    """image id -> candidate record (family, kind in normal / truncated / multi / thin, instance rows). Deterministic."""
    fam_of = {name: f for f, names in FAMILIES_06.items() for name in names}
    mids = {pool.mid(n): n for n in fam_of}
    by_img = collections.defaultdict(list)
    for r in _rows("segmentation"):
        if r["ImageID"] in pool.imgs and r["LabelName"] in mids:
            by_img[r["ImageID"]].append(r)
    out = {}
    for i, rows in sorted(by_img.items()):
        fams = {fam_of[mids[r["LabelName"]]] for r in rows}
        # another product family boxed on the image (at least 4% of the frame) would end up in the cut-out but not in the mask
        others = {fam_of[mids[b["LabelName"]]] for b in pool.boxes[i] if b["LabelName"] in mids
                  and (float(b["XMax"]) - float(b["XMin"])) * (float(b["YMax"]) - float(b["YMin"])) >= 0.04} - fams
        if len(fams) != 1 or others or i in REJECT_06:
            continue
        if any(b["IsGroupOf"] == "1" for b in pool.boxes[i] if b["LabelName"] in mids):
            continue
        try:
            f = _features_06(_union_mask(rows, zips))
        except KeyError:
            continue
        if not f:
            continue
        n_inst = len(rows)
        centred = 0.25 <= f["cx"] <= 0.75 and 0.25 <= f["cy"] <= 0.75
        if f["touch"] > 0.03:
            kind = "truncated" if f["frac"] >= 0.12 else None
        elif n_inst >= 2 and f["frac"] >= 0.08 and all(float(r["BoxXMax"]) - float(r["BoxXMin"]) > 0.08 for r in rows):
            kind = "multi"
        elif n_inst == 1 and f["solidity"] < 0.45 and f["frac"] >= 0.04:
            kind = "thin"
        elif n_inst == 1 and f["solidity"] >= 0.45 and 0.05 <= f["frac"] <= 0.65 and centred:
            kind = "normal"
        else:
            kind = None
        if kind:
            out[i] = {"family": next(iter(fams)), "kind": kind, "n": n_inst, "rows": rows,
                      "classes": sorted({mids[r["LabelName"]] for r in rows})}
    return out


def build_06() -> None:
    pool = build_pool("product")
    rng = random.Random(SEED + 6)
    zips = {c: mask_zip(c) for c in "0123456789abcdef"}
    cands = candidates_06(pool, zips)
    order = {k: [i for i, c in cands.items() if c["kind"] == k] for k in ("normal", "truncated", "multi", "thin")}
    for k in order:
        rng.shuffle(order[k])
    chosen = []
    for fam, n in NORMAL_06.items():
        fam_order = [i for i in order["normal"] if cands[i]["family"] == fam]
        got = take_downloadable(fam_order, n)
        print(UC06, "normal", fam, "candidates", len(fam_order), "chosen", len(got))
        chosen += got
    # families with fewer clean candidates than their target are topped up from the others (at most NORMAL_CAP per family)
    count = collections.Counter(cands[i]["family"] for i in chosen)
    for i in order["normal"]:
        if len(chosen) >= sum(NORMAL_06.values()):
            break
        if i not in chosen and count[cands[i]["family"]] < NORMAL_CAP and take_downloadable([i], 1):
            chosen.append(i)
            count[cands[i]["family"]] += 1
    for kind, n in HARD_06.items():
        per_fam, got = collections.Counter(), []
        for i in take_downloadable(order[kind], 40):
            if per_fam[cands[i]["family"]] < 2 and len(got) < n:
                per_fam[cands[i]["family"]] += 1
                got.append(i)
        print(UC06, kind, "candidates", len(order[kind]), "chosen", len(got))
        chosen += got
    assert len(chosen) == 40 and len(set(chosen)) == 40, len(chosen)
    st = stage(UC06)
    (st / "masks").mkdir()
    items, truth_items, attr = [], {}, []
    for i in chosen:
        c = cands[i]
        w, h = reencode(cached_image(i), st / "images" / f"{i}.jpg")
        m = Image.fromarray(_union_mask(c["rows"], zips).astype(np.uint8) * 255).resize((w, h), Image.NEAREST)
        arr = (np.array(m) > 127).astype(np.uint8) * 255
        Image.fromarray(arr).save(st / "masks" / f"{i}.png", optimize=True)
        ys, xs = np.nonzero(arr)
        items.append({"id": i, "image": f"images/{i}.jpg"})
        truth_items[i] = {"family": c["family"], "kind": c["kind"], "oi_classes": c["classes"], "instances": c["n"],
                          "mask": f"masks/{i}.png", "width": w, "height": h,
                          "bbox": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
                          "area_fraction": round(float((arr > 0).mean()), 4)}
        attr.append(attribution_row(pool.imgs[i], f"images/{i}.jpg", CHANGE_NOTE))
        attr.append({**attribution_row(pool.imgs[i], f"masks/{i}.png",
                                       "Instance masks of the listed classes merged (union), resized (nearest) to the stored image size, binarised",
                                       "segmentation mask: CC BY 4.0, Google LLC"),
                     "licence": "CC BY 4.0", "licence_url": "https://creativecommons.org/licenses/by/4.0/",
                     "author": "Google LLC (Open Images Dataset V7)", "author_url": "https://storage.googleapis.com/openimages/web/index.html",
                     "title": "mask of " + " + ".join(c["classes"]), "source_page_url": "https://storage.googleapis.com/openimages/web/download_v7.html",
                     "original_url": MASK_ZIP_URL.format(c=c["rows"][0]["MaskPath"][0]) + "#" + ",".join(r["MaskPath"] for r in c["rows"])})
    # smoke: two ordinary products of different families and one edge-truncated image (the quality rules send it to review)
    smoke_ids = [next(i for i in chosen if truth_items[i]["family"] == "Cup" and truth_items[i]["kind"] == "normal"),
                 next(i for i in chosen if truth_items[i]["family"] == "Bottle" and truth_items[i]["kind"] == "normal"),
                 next(i for i in chosen if truth_items[i]["kind"] == "truncated")]
    smoke_files = sorted(f for i in smoke_ids for f in (f"images/{i}.jpg", f"masks/{i}.png"))
    truth = {"source": SOURCE_NOTE,
             "mask_format": "PNG, 8-bit, 255 = product, 0 = background, same size as the stored image; the union of every instance "
                            "mask of the family's Open Images classes on that image",
             "kinds": {"normal": "one centred product", "truncated": "the product touches the frame edge",
                       "multi": "two or more products of the same family", "thin": "thin structures (handles, necks, legs)"},
             "items": truth_items}
    emit_set(UC06, st, items=items, truth=truth, attribution=attr,
             smoke_items=[it for it in items if it["id"] in smoke_ids],
             smoke_truth={**{k: v for k, v in truth.items() if k != "items"}, "items": {i: truth_items[i] for i in smoke_ids}},
             smoke_files=smoke_files)
    # the use case keeps a plain copy of the smoke set (no symlinks) next to its full-set manifest
    dst = USE_CASES / UC06 / "assets"
    if (dst / "smoke").exists():
        shutil.rmtree(dst / "smoke")
    shutil.copytree(SMOKE / UC06, dst / "smoke")
    # the harness looks for files under assets/, so the rows of this copy name them as smoke/images/... (like 02)
    write_attribution(dst / "smoke" / "ATTRIBUTION.csv", [{**a, "file": "smoke/" + a["file"]} for a in attr if a["file"] in set(smoke_files)])
    (dst / ".gitignore").write_text("# Extracted from 06-product-photo-pipeline-full.tar.gz (see full.json); not committed.\n"
                                    "images/\nmasks/\ntruth.json\nmanifest.json\nATTRIBUTION.csv\n")
    print(UC06, "images", len(items), dict(collections.Counter(t["family"] for t in truth_items.values())),
          dict(collections.Counter(t["kind"] for t in truth_items.values())))


BUILDERS = {"01": build_01, "02": build_02, "05": build_05, "06": build_06}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("use_cases", nargs="*", choices=list(BUILDERS), default=None,
                    help="which use cases to build (default: 01 02 05 06)")
    a = ap.parse_args()
    for uc in a.use_cases or BUILDERS:
        BUILDERS[uc]()


if __name__ == "__main__":
    main()
