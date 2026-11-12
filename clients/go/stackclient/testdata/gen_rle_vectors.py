"""Writes rle_vectors.json: masks and their COCO compressed RLE strings, for rle_test.go.

An independent implementation of the format described by the API ("COCO compressed RLE, column-major"):
the mask is read down its columns; the run lengths alternate background / object and start with background;
from the third run on a length is stored as the difference to the run two places before; each number is
written as little-endian groups of 5 bits with a continuation bit (0x20) and a sign bit (0x10) in the last
group, offset by 48 into printable ASCII. Run `python3 gen_rle_vectors.py` from this directory; the output is
committed, the Go tests only read it. Not used at runtime.
"""

import json
import random


def runs_of(rows):
    h, w = len(rows), len(rows[0])
    flat = [rows[r][c] for c in range(w) for r in range(h)]  # column-major
    runs, cur, n = [], "0", 0
    for ch in flat:
        if ch != cur:
            runs.append(n)
            cur, n = ch, 0
        n += 1
    runs.append(n)
    return runs


def to_counts(runs):
    out = []
    for i, x in enumerate(runs):
        if i > 2:
            x -= runs[i - 2]
        while True:
            group = x & 31  # Python ints are signed and unbounded: >> is arithmetic
            x >>= 5
            last = (x == 0 and not group & 16) or (x == -1 and group & 16)
            out.append(chr(48 + (group if last else group | 32)))
            if last:
                break
    return "".join(out)


def from_counts(s):
    """Decoder, used only to check the encoder before the vectors are written."""
    runs, i = [], 0
    while i < len(s):
        x, shift = 0, 0
        while True:
            c = ord(s[i]) - 48
            i += 1
            x |= (c & 31) << shift
            shift += 5
            if not c & 32:
                if c & 16:
                    x -= 1 << shift
                break
        if len(runs) > 2:
            x += runs[-2]
        runs.append(x)
    return runs


def to_rows(h, w, runs):
    flat, v = [], "0"
    for r in runs:
        flat += [v] * r
        v = "1" if v == "0" else "0"
    return ["".join(flat[c * h + r] for c in range(w)) for r in range(h)]


def vector(name, rows):
    runs = runs_of(rows)
    counts = to_counts(runs)
    assert from_counts(counts) == runs and to_rows(len(rows), len(rows[0]), runs) == rows
    return {"name": name, "height": len(rows), "width": len(rows[0]), "rows": rows, "counts": counts}


def main():
    rng = random.Random(20261112)
    grid = lambda h, w, f: ["".join("1" if f(r, c) else "0" for c in range(w)) for r in range(h)]  # noqa: E731
    vectors = [
        vector("single background pixel", ["0"]),
        vector("single object pixel", ["1"]),
        vector("all background 3x2", ["000", "000"]),
        vector("all object 2x3", ["11", "11", "11"]),
        vector("one column", ["0", "1", "1", "0", "1"]),
        vector("one row", ["0110100111"]),
        vector("starts with object", grid(4, 5, lambda r, c: c < 2)),
        vector("checkerboard 7x9", grid(7, 9, lambda r, c: (r + c) % 2 == 0)),
        vector("long runs: a filled rectangle", grid(200, 300, lambda r, c: 40 <= r < 160 and 60 <= c < 250)),
        vector("diagonal", grid(50, 80, lambda r, c: abs(r * 80 // 50 - c) < 3)),
        vector("runs that shrink and grow", grid(12, 40, lambda r, c: r < (c * 7) % 13)),
    ]
    for _ in range(4):
        h, w, p = rng.randint(5, 90), rng.randint(5, 120), rng.choice([0.02, 0.2, 0.5, 0.9])
        vectors.append(vector(f"random {h}x{w} density {p}", grid(h, w, lambda r, c, p=p: rng.random() < p)))
    with open("rle_vectors.json", "w") as f:
        json.dump(vectors, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main()
