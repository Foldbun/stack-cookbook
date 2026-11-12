"""Exact nearest-neighbour search with numpy, and near-duplicate grouping.

Vectors are L2-normalised, so the dot product is the cosine similarity. A query is one matrix-vector product over the
whole library: 10,000 vectors of 768 values take a few milliseconds. That is why there is no index structure here.
"""

import numpy as np

DEFAULT_THRESHOLD = 0.90  # cosine similarity of dinov3 vectors; a starting point only, choose it on a labelled sample of your own library


# region top-k
def top_k(matrix: np.ndarray, query, k: int, exclude: int | None = None) -> list[tuple[int, float]]:
    """The `k` rows of `matrix` most similar to `query`, as (row, score), best first. `exclude` skips one row (the query itself)."""
    if len(matrix) == 0:
        return []
    scores = matrix @ np.asarray(query, dtype=np.float32)
    if exclude is not None:
        scores[exclude] = -np.inf
    k = min(k, len(scores) - (exclude is not None))
    if k <= 0:
        return []
    best = np.argpartition(-scores, k - 1)[:k] if k < len(scores) else np.arange(len(scores))
    best = best[np.lexsort((best, -scores[best]))]  # by score, ties by row, so the order is stable
    return [(int(i), float(scores[i])) for i in best]


# endregion


# region duplicates
class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]  # path halving
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        self.parent[self.find(j)] = self.find(i)


def similar_pairs(matrix: np.ndarray, threshold: float, block: int = 1024):
    """Yield (i, j, score) for every pair i < j with similarity >= threshold, one block of rows at a time (memory stays O(block x n))."""
    n = len(matrix)
    for start in range(0, n, block):
        scores = matrix[start : start + block] @ matrix.T
        rows, cols = np.nonzero(scores >= threshold)
        for r, c in zip(rows, cols):
            if start + r < c:
                yield int(start + r), int(c), float(scores[r, c])


def duplicate_groups(matrix: np.ndarray, threshold: float) -> list[list[int]]:
    """Groups of rows that are near-duplicates of each other, found as connected components of the 'similar enough' graph.

    Connected components are transitive: if A resembles B and B resembles C, all three end up in one group even when A and
    C are below the threshold. A high threshold keeps chains short; the groups are what a person reviews, not a verdict.
    """
    uf = UnionFind(len(matrix))
    for i, j, _ in similar_pairs(matrix, threshold):
        uf.union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(len(matrix)):
        groups.setdefault(uf.find(i), []).append(i)
    return sorted((g for g in groups.values() if len(g) > 1), key=lambda g: g[0])


# endregion
