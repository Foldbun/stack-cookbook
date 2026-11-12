package main

// The on-disk vector index: one float32 matrix per model, rebuilt incrementally and replaced atomically.
//
// Layout of an index directory (the same in every language of this use case, so an index written by one can be read by
// another):
//
//	index.json                    the commit point: library catalogue, and per model the file name, dimensions and
//	                              the sha256 of the image behind every row
//	vectors-<model>-<n>.f32       row-major little-endian float32, one L2-normalised row per sha256 listed in index.json
//
// Rows are keyed by the sha256 of the file content, not by its path. A file that was renamed or copied keeps its vector,
// a file whose content changed gets a new row, and the row of a file that disappeared is dropped at the next commit.
// index.json is replaced last and atomically (write a temporary file, fsync, rename), and vector files are never
// modified in place, so a crash leaves either the old index or the new one, plus orphan files that the next commit removes.

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"sort"
)

const indexFormat = 1

var contentTypes = map[string]string{".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

// ErrIndexProblem wraps the reasons an index cannot be used as it is (unknown format, or a model now returns another
// vector length).
var ErrIndexProblem = errors.New("index problem")

// Item is one image of the library.
type Item struct {
	ID      string `json:"id"`
	Path    string `json:"path"`
	Sha256  string `json:"sha256"`
	Bytes   int64  `json:"bytes"`
	MtimeNs *int64 `json:"mtimeNs,omitempty"` // set by the folder scanner of the Python imgsearch.py; kept when present
}

// ModelInfo is what index.json records per model.
type ModelInfo struct {
	Dimensions int      `json:"dimensions"`
	File       string   `json:"file"`
	Sha256s    []string `json:"sha256s"`
}

type indexMeta struct {
	Format     int                   `json:"format"`
	Generation int                   `json:"generation"`
	Items      []Item                `json:"items"`
	Models     map[string]*ModelInfo `json:"models"`
}

func fileSha256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// atomicWrite writes path so that a reader sees the old file or the new one, never half of it.
func atomicWrite(path string, data []byte) error {
	tmp := path + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Normalize returns v divided by its L2 norm (v itself when the norm is 0), so that a dot product of two such vectors is
// their cosine similarity.
func Normalize(v []float32) []float32 {
	var sum float64
	for _, x := range v {
		sum += float64(x) * float64(x)
	}
	norm := math.Sqrt(sum)
	if norm == 0 {
		norm = 1
	}
	out := make([]float32, len(v))
	for i, x := range v {
		out[i] = float32(float64(x) / norm)
	}
	return out
}

// LibraryIndex is the library catalogue plus one vector matrix per model.
type LibraryIndex struct {
	Dir        string
	Items      []Item
	Models     map[string]*ModelInfo
	Generation int

	vectors map[string][]float32      // model -> rows x dimensions, row-major
	rows    map[string]map[string]int // model -> sha256 -> row
	dirty   map[string]bool
}

// NewIndex is an empty index that will live in dir.
func NewIndex(dir string) *LibraryIndex {
	return &LibraryIndex{Dir: dir, Models: map[string]*ModelInfo{}, vectors: map[string][]float32{}, rows: map[string]map[string]int{}, dirty: map[string]bool{}}
}

// region load

// LoadIndex opens an index directory; an empty index when there is none yet.
func LoadIndex(dir string) (*LibraryIndex, error) {
	index := NewIndex(dir)
	raw, err := os.ReadFile(filepath.Join(dir, "index.json"))
	if errors.Is(err, os.ErrNotExist) {
		return index, nil
	}
	if err != nil {
		return nil, err
	}
	var meta indexMeta
	if err := json.Unmarshal(raw, &meta); err != nil {
		return nil, fmt.Errorf("%w: index.json: %v", ErrIndexProblem, err)
	}
	if meta.Format != indexFormat {
		return nil, fmt.Errorf("%w: %s: unknown index format %d", ErrIndexProblem, filepath.Join(dir, "index.json"), meta.Format)
	}
	index.Items, index.Generation = meta.Items, meta.Generation
	for model, info := range meta.Models {
		data, err := os.ReadFile(filepath.Join(dir, info.File))
		if err != nil {
			return nil, err
		}
		if len(data) != 4*len(info.Sha256s)*info.Dimensions {
			return nil, fmt.Errorf("%w: %s has %d bytes, expected %d rows of %d values", ErrIndexProblem, info.File, len(data), len(info.Sha256s), info.Dimensions)
		}
		vec := make([]float32, len(data)/4)
		for i := range vec {
			vec[i] = math.Float32frombits(binary.LittleEndian.Uint32(data[4*i:]))
		}
		rows := make(map[string]int, len(info.Sha256s))
		for i, sha := range info.Sha256s {
			rows[sha] = i
		}
		index.Models[model], index.vectors[model], index.rows[model] = info, vec, rows
	}
	return index, nil
}

// endregion

// SetItems replaces the catalogue. Rows of images that are no longer in the library are dropped at the next commit.
func (x *LibraryIndex) SetItems(items []Item) {
	x.Items = items
	keep := map[string]bool{}
	for _, it := range items {
		keep[it.Sha256] = true
	}
	for model, rows := range x.rows {
		for sha := range rows {
			if !keep[sha] {
				x.dirty[model] = true
			}
		}
	}
}

// region missing

// Missing returns the sha256 of the library images that have no row for model yet, in catalogue order, each once.
func (x *LibraryIndex) Missing(model string) []string {
	have := x.rows[model]
	seen, out := map[string]bool{}, []string{}
	for _, it := range x.Items {
		if _, ok := have[it.Sha256]; !ok && !seen[it.Sha256] {
			seen[it.Sha256] = true
			out = append(out, it.Sha256)
		}
	}
	return out
}

// endregion

// Add adds rows for model. Vectors are renormalised so that the dot product is exactly the cosine similarity; a sha256
// that already has a row keeps it.
func (x *LibraryIndex) Add(model string, sha256s []string, vectors [][]float32) error {
	if len(sha256s) == 0 {
		return nil
	}
	if len(vectors) != len(sha256s) {
		return fmt.Errorf("%w: %d vectors for %d images", ErrIndexProblem, len(vectors), len(sha256s))
	}
	dims := len(vectors[0])
	for _, v := range vectors {
		if len(v) != dims {
			return fmt.Errorf("%w: %s returned vectors of different lengths", ErrIndexProblem, model)
		}
	}
	if info, ok := x.Models[model]; ok && info.Dimensions != dims {
		return fmt.Errorf("%w: %s now returns %d values per vector but the index holds %d: the model changed, rebuild the index", ErrIndexProblem, model, dims, info.Dimensions)
	}
	rows := x.rows[model]
	if rows == nil {
		rows = map[string]int{}
		x.rows[model] = rows
	}
	vec := x.vectors[model]
	for i, sha := range sha256s {
		if _, ok := rows[sha]; ok {
			continue
		}
		rows[sha] = len(vec) / dims
		vec = append(vec, Normalize(vectors[i])...)
	}
	x.vectors[model] = vec
	if _, ok := x.Models[model]; !ok {
		x.Models[model] = &ModelInfo{Dimensions: dims, Sha256s: []string{}}
	}
	x.dirty[model] = true
	return nil
}

// region commit

// Commit writes what changed. Call it after every batch, so that a stop or a crash loses at most one batch.
func (x *LibraryIndex) Commit() error {
	if err := os.MkdirAll(x.Dir, 0o755); err != nil {
		return err
	}
	keep := map[string]bool{}
	for _, it := range x.Items {
		keep[it.Sha256] = true
	}
	x.Generation++
	dirty := make([]string, 0, len(x.dirty))
	for model := range x.dirty {
		dirty = append(dirty, model)
	}
	sort.Strings(dirty)
	for _, model := range dirty {
		rows, dims := x.rows[model], x.Models[model].Dimensions
		order := make([]string, 0, len(rows))
		for sha := range rows {
			if keep[sha] {
				order = append(order, sha)
			}
		}
		sort.Slice(order, func(i, j int) bool { return rows[order[i]] < rows[order[j]] })
		old, fresh := x.vectors[model], make(map[string]int, len(order))
		kept, file := make([]float32, 0, len(order)*dims), make([]byte, 0, 4*len(order)*dims)
		for i, sha := range order {
			row := old[rows[sha]*dims : (rows[sha]+1)*dims]
			kept = append(kept, row...)
			for _, v := range row {
				file = binary.LittleEndian.AppendUint32(file, math.Float32bits(v))
			}
			fresh[sha] = i
		}
		x.vectors[model], x.rows[model] = kept, fresh
		name := fmt.Sprintf("vectors-%s-%d.f32", model, x.Generation)
		if err := atomicWrite(filepath.Join(x.Dir, name), file); err != nil {
			return err
		}
		x.Models[model].File, x.Models[model].Sha256s = name, order
	}
	x.dirty = map[string]bool{}
	items := x.Items
	if items == nil {
		items = []Item{}
	}
	meta, err := json.MarshalIndent(indexMeta{Format: indexFormat, Generation: x.Generation, Items: items, Models: x.Models}, "", " ")
	if err != nil {
		return err
	}
	if err := atomicWrite(filepath.Join(x.Dir, "index.json"), meta); err != nil {
		return err
	}
	live := map[string]bool{}
	for _, info := range x.Models {
		live[info.File] = true
	}
	stale, _ := filepath.Glob(filepath.Join(x.Dir, "vectors-*.f32"))
	for _, path := range stale {
		if !live[filepath.Base(path)] {
			os.Remove(path)
		}
	}
	return nil
}

// endregion

// Matrix returns the library items that have a vector for model, and their vectors as one matrix.
func (x *LibraryIndex) Matrix(model string) ([]Item, Matrix) {
	rows := x.rows[model]
	m := Matrix{Dim: x.Dimensions(model)}
	items := []Item{}
	for _, it := range x.Items {
		if row, ok := rows[it.Sha256]; ok {
			items = append(items, it)
			m.Data = append(m.Data, x.vectors[model][row*m.Dim:(row+1)*m.Dim]...)
		}
	}
	m.N = len(items)
	return items, m
}

// Has reports whether model has a vector for the image with this sha256.
func (x *LibraryIndex) Has(model, sha256 string) bool {
	_, ok := x.rows[model][sha256]
	return ok
}

// Dimensions is the vector length of model, or 0 when the index holds none.
func (x *LibraryIndex) Dimensions(model string) int {
	if info, ok := x.Models[model]; ok {
		return info.Dimensions
	}
	return 0
}
