# Copied from clients/python/ in github.com/Foldbun/stack-cookbook. Edit the original there.
"""COCO compressed run-length encoding of binary masks (Python standard library only).

The API returns `mask: {"encoding": "coco", "size": [height, width], "counts": "..."}`. This is the format of
pycocotools' `frPyObjects` / `encode`: the mask is read column by column (column-major, "Fortran order"), the counts are
alternating run lengths that start with a run of zeros, and the counts string packs every count into 5-bit groups
(character = 48 + group, bit 0x20 means "more groups follow", bit 0x10 of the last group is the sign). From the third
count on, each count is stored as the difference to the count two places before.

A mask here is a list of `h` rows, each a `bytearray` of `w` values 0 or 1 (row-major, the way images are usually
indexed as `rows[y][x]`). The functions do not need numpy; they are written for masks of up to a few megapixels.
"""

from itertools import groupby

VERSION = "1.0.0"


def counts_to_runs(counts) -> list[int]:
    """Unpack a counts string (or bytes) into run lengths."""
    data = counts.encode("ascii") if isinstance(counts, str) else bytes(counts)
    runs, pos = [], 0
    while pos < len(data):
        value, shift, more = 0, 0, True
        while more:
            c = data[pos] - 48
            value |= (c & 0x1F) << shift
            more = bool(c & 0x20)
            pos += 1
            shift += 5
            if not more and c & 0x10:
                value |= -1 << shift  # negative: fill the upper bits with ones
        if len(runs) > 2:
            value += runs[-2]
        runs.append(value)
    return runs


def runs_to_counts(runs) -> str:
    """Pack run lengths into a counts string."""
    out = []
    for i, run in enumerate(runs):
        value = run - runs[i - 2] if i > 2 else run
        more = True
        while more:
            c = value & 0x1F
            value >>= 5
            more = value != -1 if c & 0x10 else value != 0
            if more:
                c |= 0x20
            out.append(chr(c + 48))
    return "".join(out)


def decode(rle) -> list[bytearray]:
    """`{"size": [h, w], "counts": ...}` to rows. Raises ValueError when the runs do not add up to h * w."""
    h, w = rle["size"]
    runs = counts_to_runs(rle["counts"])
    if sum(runs) != h * w or any(r < 0 for r in runs):
        raise ValueError(f"counts cover {sum(runs)} pixels, a {h}x{w} mask has {h * w}")
    flat, pos = bytearray(h * w), 0  # column-major: pixel (x, y) is flat[x * h + y]
    for i, run in enumerate(runs):
        if i % 2:
            flat[pos : pos + run] = b"\x01" * run
        pos += run
    return [flat[y::h] for y in range(h)]


def encode(rows) -> dict:
    """Rows of 0/1 values to `{"size": [h, w], "counts": str}`; any non-zero value counts as 1."""
    h, w = len(rows), len(rows[0]) if rows else 0
    flat = bytearray(h * w)
    for y, row in enumerate(rows):
        flat[y::h] = bytes(1 if v else 0 for v in row)
    runs, expect = [], 0
    for value, group in groupby(flat):
        if value != expect:  # runs alternate and start with zeros, so a mask starting with 1s opens with a 0-run
            runs.append(0)
            expect = value
        runs.append(sum(1 for _ in group))
        expect ^= 1
    if not runs:
        runs = [0]
    return {"size": [h, w], "counts": runs_to_counts(runs)}


def area(rle) -> int:
    """Number of mask pixels, from the runs alone."""
    return sum(counts_to_runs(rle["counts"])[1::2])


def bbox(rle):
    """Tight box [x1, y1, x2, y2] with x2 and y2 exclusive, or None for an empty mask."""
    h, w = rle["size"]
    pos, x1, y1, x2, y2 = 0, w, h, -1, -1
    for i, run in enumerate(counts_to_runs(rle["counts"])):
        if i % 2 and run:
            first, last = pos, pos + run - 1  # column-major index of the first and last pixel of the run
            x1, x2 = min(x1, first // h), max(x2, last // h)
            if last // h > first // h:  # the run spans a column boundary: it touches the top and bottom rows
                y1, y2 = 0, h - 1
            else:
                y1, y2 = min(y1, first % h), max(y2, last % h)
        pos += run
    return None if x2 < 0 else [x1, y1, x2 + 1, y2 + 1]


def resize(rle, height, width) -> dict:
    """Nearest-neighbour resample to another grid, for masks that are smaller than the image they belong to."""
    rows = decode(rle)
    h, w = rle["size"]
    xs = [min(w - 1, int((x + 0.5) * w / width)) for x in range(width)]
    out = [bytes(rows[min(h - 1, int((y + 0.5) * h / height))][x] for x in xs) for y in range(height)]
    return encode(out)
