"""Shared helpers for the dataset builders: polite cached downloads with a sha256 lock file,
image re-encoding, deterministic tarballs, JSON / CSV writers, set emission."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

USER_AGENT = "foldbun-cookbook-datasets/1.0"
DATASETS = Path(__file__).resolve().parent.parent          # the datasets/ directory
CACHE = DATASETS / ".cache"                                 # downloads (ignored by git)
WORK = DATASETS / ".cache" / "work"                         # full-set staging dirs (ignored)
DIST = DATASETS / "dist"                                    # tarballs, sheets (ignored)
SMOKE = DATASETS / "smoke"                                  # committed smoke sets
ATTRIBUTION = DATASETS / "attribution"                      # committed attribution CSVs
USE_CASES = DATASETS.parent / "use-cases"                   # where the committed full-set manifests go
LOCK_PATH = DATASETS / "sources.lock.json"                  # url -> {sha256, bytes}; committed
MAX_SIDE = 1024
JPEG_QUALITY = 90
CHANGE_NOTE = "Resized to a long side of at most 1024 px and re-encoded as JPEG"

_lock_mutex = threading.Lock()


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_lock() -> dict:
    return json.loads(LOCK_PATH.read_text()) if LOCK_PATH.exists() else {}


def save_lock(lock: dict) -> None:
    LOCK_PATH.write_text(json.dumps(dict(sorted(lock.items())), indent=1) + "\n")


class VerifyError(RuntimeError):
    pass


def fetch(url: str, dest: Path, *, retries: int = 3, pause: float = 0.0) -> Path:
    """Download url to dest (resumable, atomic). The sha256 of every download is recorded in
    sources.lock.json; a later download or cache hit that disagrees with the lock raises."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest.exists() and dest.stat().st_size > 0):
        part = dest.with_name(dest.name + ".part")
        for attempt in range(retries):
            try:
                have = part.stat().st_size if part.exists() else 0
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                if have:
                    req.add_header("Range", f"bytes={have}-")
                with urllib.request.urlopen(req, timeout=120) as r:
                    mode = "ab" if (have and r.status == 206) else "wb"
                    with open(part, mode) as f:
                        while chunk := r.read(1 << 18):
                            f.write(chunk)
                os.replace(part, dest)
                break
            except urllib.error.HTTPError as e:
                if e.code == 416:           # nothing left to resume: the .part is complete
                    os.replace(part, dest)
                    break
                if e.code in (403, 404) or attempt == retries - 1:
                    raise
                time.sleep(2 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == retries - 1:
                    raise
                time.sleep(2 * (attempt + 1))
        if pause:
            time.sleep(pause)
    digest = sha256_file(dest)
    with _lock_mutex:
        lock = load_lock()
        rec = lock.get(url)
        if rec is None:
            lock[url] = {"sha256": digest, "bytes": dest.stat().st_size}
            save_lock(lock)
        elif rec["sha256"] != digest:
            raise VerifyError(f"sha256 mismatch for {url}: lock {rec['sha256']} != {digest}")
    return dest


def fetch_many(jobs: list[tuple[str, Path]], workers: int = 4) -> None:
    """Limited concurrency (4 workers) and a small pause after each request."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda j: fetch(j[0], j[1], pause=0.1), jobs))


def reencode(src: Path | bytes, dst: Path, max_side: int = MAX_SIDE) -> tuple[int, int]:
    """Decode, downscale to a long side of at most max_side, write JPEG q90. Returns (w, h)."""
    im = Image.open(io.BytesIO(src) if isinstance(src, bytes) else src)
    im.load()
    im = im.convert("RGB")
    w, h = im.size
    s = max_side / max(w, h)
    if s < 1:
        im = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return im.size


def write_json(path: Path, obj, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=indent, ensure_ascii=False) + "\n")


ATTR_FIELDS = ["file", "id", "title", "author", "author_url", "source_page_url", "licence", "licence_url",
               "original_url", "modified", "notes"]


def write_attribution(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ATTR_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ATTR_FIELDS})


def files_listing(root: Path) -> list[dict]:
    return [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": sha256_file(p)}
            for p in sorted(root.rglob("*")) if p.is_file()]


def make_tarball(src_dir: Path, out: Path) -> dict:
    """Deterministic .tar.gz of src_dir's contents (sorted, fixed mtime / owner, gzip mtime 0)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for p in sorted(src_dir.rglob("*")):
                if not p.is_file():
                    continue
                ti = tar.gettarinfo(str(p), arcname=p.relative_to(src_dir).as_posix())
                ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname, ti.mode = 0, 0, 0, "", "", 0o644
                with open(p, "rb") as f:
                    tar.addfile(ti, f)
    return {"file": out.name, "bytes": out.stat().st_size, "sha256": sha256_file(out)}


# Item parameters of the committed copy that the archive's manifest does not carry (the click limit of the full protocol).
FULL_ITEM_PARAMS = {"02-click-to-segment": {"maxClicks": 10}}


def repo_manifest(uc: str, manifest: dict) -> dict:
    """The committed copy of a full set's manifest (use-cases/<uc>/assets/full.json): the archive's manifest.json
    with a note on top. The images, masks and ground truth are not in git; they come from the release archive."""
    items = [{**it, **FULL_ITEM_PARAMS.get(uc, {})} for it in manifest["items"]]
    note = (f"The {len(items)} items of the full set. The images are not in git: download {uc}-full.tar.gz from the "
            "release named in datasets/manifest.json and extract it into this directory (it adds images/, "
            "truth.json and its own manifest.json).")
    rest = {k: v for k, v in manifest.items() if k not in ("usecase", "version", "set", "items")}
    return {"usecase": manifest["usecase"], "version": manifest["version"], "set": "full", "note": note, **rest, "items": items}


def write_repo_manifest(uc: str, manifest: dict, root: Path = USE_CASES) -> Path:
    path = root / uc / "assets" / "full.json"
    write_json(path, repo_manifest(uc, manifest))
    return path


def emit_set(uc: str, stage: Path, *, items: list[dict], truth: dict, attribution: list[dict],
             smoke_items: list[dict], smoke_truth: dict, smoke_files: list[str], extra: dict | None = None) -> None:
    """Finish a use case. `stage` already holds the full set's files (images/, masks/ ...).

    Writes stage/{manifest.json, truth.json, ATTRIBUTION.csv}; the committed copy of the manifest
    (use-cases/<uc>/assets/full.json, see repo_manifest); the committed smoke subset into
    datasets/smoke/<uc>/ (the same relative paths, copied from stage); the committed attribution CSV for
    the full set; and the deterministic tarball + file listing into datasets/dist/.
    `smoke_files` are paths relative to the set root; every one needs an attribution row.
    """
    head = {"usecase": uc, "version": 1, **(extra or {})}
    full = {**head, "set": "full", "items": items}
    write_json(stage / "manifest.json", full)
    write_repo_manifest(uc, full)
    write_json(stage / "truth.json", {"usecase": uc, "set": "full", **truth})
    write_attribution(stage / "ATTRIBUTION.csv", attribution)
    write_attribution(ATTRIBUTION / f"ATTRIBUTION-{uc}.csv", attribution)

    sm = SMOKE / uc
    if sm.exists():
        shutil.rmtree(sm)
    for rel in smoke_files:
        (sm / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(stage / rel, sm / rel)
    write_json(sm / "manifest.json", {**head, "set": "smoke", "items": smoke_items})
    write_json(sm / "truth.json", {"usecase": uc, "set": "smoke", **smoke_truth})
    keep = set(smoke_files)
    write_attribution(sm / "ATTRIBUTION.csv", [a for a in attribution if a["file"] in keep])
    missing = keep - {a["file"] for a in attribution}
    if missing:
        raise RuntimeError(f"{uc}: smoke files without attribution row: {sorted(missing)[:3]}")

    info = make_tarball(stage, DIST / f"{uc}-full.tar.gz")
    info["usecase"] = uc
    info["files"] = files_listing(stage)
    write_json(DIST / f"{uc}-full.files.json", info)
