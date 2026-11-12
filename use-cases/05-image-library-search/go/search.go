package main

// Exact nearest-neighbour search in plain Go, and near-duplicate grouping.
//
// Vectors are L2-normalised, so the dot product is the cosine similarity. A query is one pass over the whole library:
// 10,000 vectors of 768 values take a few milliseconds. That is why there is no index structure here.

import (
	"math"
	"runtime"
	"sort"
	"sync"
)

// DefaultThreshold is the cosine similarity of dinov3 vectors above which two images are near-duplicates; a starting
// point only, choose it on a labelled sample of your own library.
const DefaultThreshold = 0.90

// Matrix is N vectors of Dim float32 values, row-major.
type Matrix struct {
	Data   []float32
	N, Dim int
}

// Row is the i-th vector.
func (m Matrix) Row(i int) []float32 { return m.Data[i*m.Dim : (i+1)*m.Dim] }

// Hit is a row of a matrix and its similarity to the query.
type Hit struct {
	Row   int
	Score float32
}

// dot is the dot product of two vectors, accumulated in float64 and rounded once to float32.
func dot(a, b []float32) float32 {
	var sum float64
	for i, x := range a {
		sum += float64(x) * float64(b[i])
	}
	return float32(sum)
}

// region top-k

// TopK returns the k rows of m most similar to query (which has m.Dim values), as (row, score), best first. exclude
// skips one row (the query itself); pass -1 to skip none. Equal scores are ordered by row, so the order is stable.
func TopK(m Matrix, query []float32, k int, exclude int) []Hit {
	if m.N == 0 {
		return []Hit{}
	}
	hits := make([]Hit, 0, m.N)
	for i := 0; i < m.N; i++ {
		if i != exclude {
			hits = append(hits, Hit{i, dot(m.Row(i), query)})
		}
	}
	sort.Slice(hits, func(a, b int) bool {
		if hits[a].Score != hits[b].Score {
			return hits[a].Score > hits[b].Score
		}
		return hits[a].Row < hits[b].Row
	})
	if k < 0 {
		k = 0
	}
	return hits[:min(k, len(hits))]
}

// endregion

// region duplicates

// UnionFind groups elements into connected components.
type UnionFind struct{ parent []int }

func NewUnionFind(n int) *UnionFind {
	u := &UnionFind{parent: make([]int, n)}
	for i := range u.parent {
		u.parent[i] = i
	}
	return u
}

func (u *UnionFind) Find(i int) int {
	for u.parent[i] != i {
		u.parent[i] = u.parent[u.parent[i]] // path halving
		i = u.parent[i]
	}
	return i
}

func (u *UnionFind) Union(i, j int) { u.parent[u.Find(j)] = u.Find(i) }

// SimilarPairs returns every pair (i, j) with i < j whose similarity is at least threshold, ordered by i then j. Rows are
// compared in parallel, in blocks.
func SimilarPairs(m Matrix, threshold float64) [][2]int {
	const block = 64
	limit := float32(threshold) // the scores are float32: compare in float32
	blocks := (m.N + block - 1) / block
	found := make([][][2]int, blocks)
	var wg sync.WaitGroup
	next := make(chan int)
	for w := 0; w < runtime.NumCPU(); w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for b := range next {
				for i := b * block; i < min((b+1)*block, m.N); i++ {
					for j := i + 1; j < m.N; j++ {
						if dot(m.Row(i), m.Row(j)) >= limit {
							found[b] = append(found[b], [2]int{i, j})
						}
					}
				}
			}
		}()
	}
	for b := 0; b < blocks; b++ {
		next <- b
	}
	close(next)
	wg.Wait()
	var pairs [][2]int
	for _, f := range found {
		pairs = append(pairs, f...)
	}
	return pairs
}

// DuplicateGroups returns the groups of rows that are near-duplicates of each other, found as connected components of
// the "similar enough" graph. Rows inside a group, and the groups themselves, are in row order.
//
// Connected components are transitive: if A resembles B and B resembles C, all three end up in one group even when A and
// C are below the threshold. A high threshold keeps chains short; the groups are what a person reviews, not a verdict.
func DuplicateGroups(m Matrix, threshold float64) [][]int {
	uf := NewUnionFind(m.N)
	for _, p := range SimilarPairs(m, threshold) {
		uf.Union(p[0], p[1])
	}
	byRoot, groups := map[int][]int{}, [][]int{}
	for i := 0; i < m.N; i++ {
		root := uf.Find(i)
		byRoot[root] = append(byRoot[root], i)
	}
	for _, g := range byRoot {
		if len(g) > 1 {
			groups = append(groups, g)
		}
	}
	sort.Slice(groups, func(a, b int) bool { return groups[a][0] < groups[b][0] })
	return groups
}

// endregion

// round4 rounds to 4 decimals, as the scores in result.json are.
func round4(x float64) float64 { return math.Round(x*1e4) / 1e4 }
