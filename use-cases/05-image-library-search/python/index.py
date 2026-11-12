"""The on-disk vector index: one float32 matrix per model, rebuilt incrementally and replaced atomically.

Layout of an index directory (the same in every language of this use case):

    index.json                    the commit point: library catalogue, and per model the file name, dimensions and
                                  the sha256 of the image behind every row
    vectors-<model>-<n>.f32       row-major little-endian float32, one L2-normalised row per sha256 listed in index.json

Rows are keyed by the sha256 of the file content, not by its path. A file that was renamed or copied keeps its vector,
a file whose content changed gets a new row, and the row of a file that disappeared is dropped at the next commit.
`index.json` is replaced last and atomically (write a temporary file, fsync, rename), and vector files are never
modified in place, so a crash leaves either the old index or the new one, plus orphan files that the next commit removes.
"""

import hashlib
import json
import os
from pathlib import Path

import numpy as np

FORMAT = 1
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class IndexProblem(Exception):
    """The index cannot be used as it is (unknown format, or a model now returns another vector length)."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """Write `path` so that a reader sees the old file or the new one, never half of it."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def scan_folder(folder: Path, previous: list[dict] | None = None) -> list[dict]:
    """Catalogue of the images under `folder`: id (relative path), sha256, size.

    A file whose path, size and modification time match the previous catalogue keeps its sha256 without being read again.
    """
    known = {it["id"]: it for it in previous or []}
    items = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES):
        rel = path.relative_to(folder).as_posix()
        st = path.stat()
        old = known.get(rel)
        if old and old.get("bytes") == st.st_size and old.get("mtimeNs") == st.st_mtime_ns:
            sha = old["sha256"]
        else:
            sha = file_sha256(path)
        items.append({"id": rel, "path": rel, "sha256": sha, "bytes": st.st_size, "mtimeNs": st.st_mtime_ns})
    return items


class LibraryIndex:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.items: list[dict] = []  # the library: [{id, path, sha256, bytes, ...}]
        self.models: dict[str, dict] = {}  # model -> {dimensions, file, sha256s}
        self.generation = 0
        self._vectors: dict[str, np.ndarray] = {}  # model -> (rows, dimensions) float32
        self._rows: dict[str, dict[str, int]] = {}  # model -> sha256 -> row
        self._dirty: set[str] = set()

    # region load
    @classmethod
    def load(cls, directory: Path) -> "LibraryIndex":
        """Open an index directory; an empty index when there is none yet."""
        index = cls(directory)
        meta_file = index.dir / "index.json"
        if not meta_file.exists():
            return index
        meta = json.loads(meta_file.read_text())
        if meta.get("format") != FORMAT:
            raise IndexProblem(f"{meta_file}: unknown index format {meta.get('format')!r}")
        index.items, index.models, index.generation = meta["items"], meta["models"], meta["generation"]
        for model, info in index.models.items():
            raw = np.fromfile(index.dir / info["file"], dtype="<f4")
            index._vectors[model] = raw.reshape(len(info["sha256s"]), info["dimensions"])
            index._rows[model] = {sha: i for i, sha in enumerate(info["sha256s"])}
        return index

    # endregion

    def set_items(self, items: list[dict]) -> None:
        """Replace the catalogue. Rows of images that are no longer in the library are dropped at the next commit."""
        self.items = items
        keep = {it["sha256"] for it in items}
        for model, rows in self._rows.items():
            if set(rows) - keep:
                self._dirty.add(model)

    # region missing
    def missing(self, model: str, limit: int | None = None) -> list[str]:
        """sha256 of the library images that have no row for `model` yet, in catalogue order, each once."""
        have = self._rows.get(model, {})
        seen, out = set(), []
        for it in self.items:
            sha = it["sha256"]
            if sha not in have and sha not in seen:
                seen.add(sha)
                out.append(sha)
        return out[:limit]

    # endregion

    def add(self, model: str, sha256s: list[str], vectors) -> None:
        """Add rows for `model`. Vectors are renormalised so that the dot product is exactly the cosine similarity."""
        if not len(sha256s):
            return
        new = np.asarray(vectors, dtype=np.float32).reshape(len(sha256s), -1)
        dims = new.shape[1]
        if model in self.models and self.models[model]["dimensions"] != dims:
            raise IndexProblem(
                f"{model} now returns {dims} values per vector but the index holds {self.models[model]['dimensions']}: the model changed, rebuild the index"
            )
        norms = np.linalg.norm(new, axis=1, keepdims=True)
        new = new / np.where(norms == 0, 1, norms)
        old = self._vectors.get(model, np.zeros((0, dims), dtype=np.float32))
        rows = self._rows.setdefault(model, {})
        keep = [i for i, sha in enumerate(sha256s) if sha not in rows]
        for offset, i in enumerate(keep):
            rows[sha256s[i]] = len(old) + offset
        self._vectors[model] = np.vstack([old, new[keep]])
        self.models.setdefault(model, {"dimensions": dims, "file": None, "sha256s": []})
        self._dirty.add(model)

    # region commit
    def commit(self) -> None:
        """Write what changed. Call it after every batch, so that a stop or a crash loses at most one batch."""
        self.dir.mkdir(parents=True, exist_ok=True)
        keep = {it["sha256"] for it in self.items}
        self.generation += 1
        for model in sorted(self._dirty):
            rows = self._rows[model]
            order = [sha for sha, _ in sorted(rows.items(), key=lambda kv: kv[1]) if sha in keep]
            matrix = self._vectors[model][[rows[sha] for sha in order]] if order else self._vectors[model][:0]
            self._vectors[model], self._rows[model] = matrix, {sha: i for i, sha in enumerate(order)}
            name = f"vectors-{model}-{self.generation}.f32"
            atomic_write(self.dir / name, np.ascontiguousarray(matrix, dtype="<f4").tobytes())
            self.models[model].update(file=name, sha256s=order)
        self._dirty.clear()
        meta = {"format": FORMAT, "generation": self.generation, "items": self.items, "models": self.models}
        atomic_write(self.dir / "index.json", json.dumps(meta, indent=1).encode())
        live = {info["file"] for info in self.models.values()}
        for stale in self.dir.glob("vectors-*.f32"):
            if stale.name not in live:
                stale.unlink()

    # endregion

    def matrix(self, model: str) -> tuple[list[dict], np.ndarray]:
        """The library items that have a vector for `model`, and their vectors as one (n, dimensions) matrix."""
        rows = self._rows.get(model, {})
        items = [it for it in self.items if it["sha256"] in rows]
        if not items:
            return [], np.zeros((0, self.models.get(model, {}).get("dimensions", 0)), dtype=np.float32)
        return items, self._vectors[model][[rows[it["sha256"]] for it in items]]

    def has(self, model: str, sha256: str) -> bool:
        return sha256 in self._rows.get(model, {})

    def vector(self, model: str, sha256: str):
        row = self._rows.get(model, {}).get(sha256)
        return None if row is None else self._vectors[model][row]

    def dimensions(self, model: str) -> int | None:
        return self.models.get(model, {}).get("dimensions")
