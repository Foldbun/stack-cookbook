package main

import (
	"bytes"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Files that other tools read: COCO JSON, YOLO-seg text files, a review queue (CSV plus crops) and `costs.csv`.
//
// Coordinates. The API gives polygon vertices as pixel *centres* (`0 .. width - 1`) and boxes as pixel *edges* with an
// exclusive far side. COCO and YOLO both use the pixel grid, where the left edge of pixel 0 is 0.0, so every polygon
// vertex is shifted by +0.5. Boxes need no shift. COCO polygons have no holes, so only outer rings are exported; with
// `--masks` the segmentation is a COCO RLE, which keeps holes.

// record is an image that was segmented, with what routing decided.
type record struct {
	ID, File, Path string
	Width, Height  int
	Detections     []Detection
	Missing        []string
}

// gridRing moves a flat ring [x0, y0, x1, y1, ...] from pixel centres to pixel-grid coordinates.
func gridRing(ring []float64) []float64 {
	out := make([]float64, len(ring))
	for i, v := range ring {
		out[i] = v + 0.5
	}
	return out
}

// region coco-export
type cocoAnnotation struct {
	ID           int     `json:"id"`
	ImageID      int     `json:"image_id"`
	CategoryID   int     `json:"category_id"`
	BBox         [4]int  `json:"bbox"` // [x, y, width, height]
	Area         int     `json:"area"`
	Segmentation any     `json:"segmentation"`
	IsCrowd      int     `json:"iscrowd"`
	Score        float64 `json:"score"`
}

func cocoAnn(id, imageID, catID int, d Detection, width, height int) (cocoAnnotation, error) {
	x1, y1, x2, y2 := d.BBox[0], d.BBox[1], d.BBox[2], d.BBox[3]
	a := cocoAnnotation{ID: id, ImageID: imageID, CategoryID: catID, BBox: [4]int{x1, y1, x2 - x1, y2 - y1}, Score: round(d.Score, 4)}
	if d.Mask != nil { // --masks: a COCO RLE at the size of the image (the mask grid can be smaller)
		m, err := fitMask(*d.Mask, height, width)
		if err != nil {
			return a, err
		}
		area, err := rleArea(m)
		if err != nil {
			return a, err
		}
		a.Segmentation, a.Area = map[string]any{"size": m.Size, "counts": m.Counts}, area
	} else {
		rings := [][]float64{}
		for _, r := range d.Polygon {
			rings = append(rings, gridRing(r))
		}
		a.Segmentation, a.Area = rings, d.Area
	}
	return a, nil
}

// writeCOCO writes the COCO instances file of the detections routed to `band`. Every image is listed, also those
// without annotations.
func writeCOCO(path string, records []record, classes []string, band, description string) error {
	type image struct {
		ID       int    `json:"id"`
		FileName string `json:"file_name"`
		Width    int    `json:"width"`
		Height   int    `json:"height"`
	}
	type category struct {
		ID   int    `json:"id"`
		Name string `json:"name"`
	}
	doc := struct {
		Info        map[string]string `json:"info"`
		Licenses    []string          `json:"licenses"`
		Images      []image           `json:"images"`
		Categories  []category        `json:"categories"`
		Annotations []cocoAnnotation  `json:"annotations"`
	}{
		Info:        map[string]string{"description": description, "version": "1.0", "date_created": time.Now().UTC().Format("2006-01-02T15:04:05Z")},
		Licenses:    []string{},
		Images:      []image{},
		Categories:  []category{},
		Annotations: []cocoAnnotation{},
	}
	cats := map[string]int{}
	for i, name := range classes {
		cats[name] = i + 1
		doc.Categories = append(doc.Categories, category{i + 1, name})
	}
	for n, rec := range records {
		doc.Images = append(doc.Images, image{n + 1, rec.File, rec.Width, rec.Height})
		for _, d := range rec.Detections {
			if d.Route != band || (len(d.Polygon) == 0 && d.Mask == nil) {
				continue
			}
			a, err := cocoAnn(len(doc.Annotations)+1, n+1, cats[d.Class], d, rec.Width, rec.Height)
			if err != nil {
				return err
			}
			doc.Annotations = append(doc.Annotations, a)
		}
	}
	data, err := json.MarshalIndent(doc, "", " ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0o644)
}

// endregion

// region yolo-export
// writeYOLO writes the YOLO segmentation labels of the accepted detections: `labels/<image>.txt`, one line per polygon
// part: `class x1 y1 x2 y2 ...` normalised to 0..1, and `data.yaml`. An image without detections gets an empty file.
func writeYOLO(root string, records []record, classes []string) error {
	if err := os.MkdirAll(filepath.Join(root, "labels"), 0o755); err != nil {
		return err
	}
	index := map[string]int{}
	for i, name := range classes {
		index[name] = i
	}
	for _, rec := range records {
		var lines strings.Builder
		for _, d := range rec.Detections {
			if d.Route != "accept" {
				continue
			}
			for _, ring := range d.Polygon {
				fmt.Fprintf(&lines, "%d", index[d.Class])
				for i, v := range gridRing(ring) {
					size := rec.Height
					if i%2 == 0 {
						size = rec.Width
					}
					fmt.Fprintf(&lines, " %.6f", math.Min(1, math.Max(0, v/float64(size))))
				}
				lines.WriteString("\n")
			}
		}
		stem := strings.TrimSuffix(filepath.Base(rec.File), filepath.Ext(rec.File))
		if err := os.WriteFile(filepath.Join(root, "labels", stem+".txt"), []byte(lines.String()), 0o644); err != nil {
			return err
		}
	}
	var names strings.Builder
	for i, name := range classes {
		fmt.Fprintf(&names, "  %d: %s\n", i, jsonString(name))
	}
	yaml := "# Put or link the images in yolo/images; labels/ holds the matching .txt files.\npath: .\ntrain: images\nval: images\nnames:\n" + names.String()
	return os.WriteFile(filepath.Join(root, "data.yaml"), []byte(yaml), 0o644)
}

// endregion

var queueFields = []string{"queue_id", "image_id", "image_file", "class", "reason", "score", "x1", "y1", "x2", "y2", "crop"}

// region review-queue
// writeReview writes `queue.csv` plus one crop per row. Rows: detections between the review and accept thresholds
// (`low_score`), and expected classes with no detection (`empty_but_expected`, the crop is the whole image). It returns
// the number of rows.
func writeReview(root string, records []record) (int, error) {
	if err := os.MkdirAll(filepath.Join(root, "crops"), 0o755); err != nil {
		return 0, err
	}
	type row struct {
		rec                record
		qid, class, reason string
		score              string
		box                *[4]int // nil: the crop is the whole image
	}
	var rows []row
	for _, rec := range records {
		n := 0
		for _, d := range rec.Detections {
			if d.Route == "review" {
				n++
				rows = append(rows, row{rec, fmt.Sprintf("%s-%02d", rec.ID, n), d.Class, "low_score", num(round(d.Score, 4)), &d.BBox})
			}
		}
		for _, class := range rec.Missing {
			n++
			rows = append(rows, row{rec, fmt.Sprintf("%s-%02d", rec.ID, n), class, "empty_but_expected", "", nil})
		}
	}
	var buf bytes.Buffer
	w := csv.NewWriter(&buf)
	w.Write(queueFields)
	for _, r := range rows {
		crop, box := "crops/"+r.qid+".jpg", []string{"", "", "", ""}
		if err := saveCrop(r.rec.Path, r.box, filepath.Join(root, crop)); err != nil {
			logf("  review %s: no crop (%v)", r.qid, err)
			crop = ""
		}
		if r.box != nil {
			for i, v := range r.box {
				box[i] = strconv.Itoa(v)
			}
		}
		w.Write(append([]string{r.qid, r.rec.ID, r.rec.File, r.class, r.reason, r.score}, append(box, crop)...))
	}
	w.Flush()
	return len(rows), os.WriteFile(filepath.Join(root, "queue.csv"), buf.Bytes(), 0o644)
}

// endregion

var costFields = []string{"image_id", "status", "list_credits", "charged_credits", "replayed", "cache", "queue_ms", "processing_ms", "instances", "accepted", "review"}

// region costs
// writeCosts writes one row per image: what the call cost at list price and what was charged (0 when replayed),
// timing and counts.
func writeCosts(path string, items []itemResult) error {
	var buf bytes.Buffer
	w := csv.NewWriter(&buf)
	w.Write(costFields)
	for _, it := range items {
		var list, charged float64
		var queue, processing int
		replayed, caches := false, map[string]bool{}
		for _, c := range it.Calls {
			if !c.Replayed {
				list += c.List
			}
			charged += c.Charged
			replayed = replayed || c.Replayed
			if c.Cache != nil && *c.Cache != "" {
				caches[*c.Cache] = true
			}
			queue += c.QueueMs
			processing += c.ProcessingMs
		}
		names := []string{}
		for name := range caches {
			names = append(names, name)
		}
		sort.Strings(names)
		var instances, accepted, review int
		if it.Output != nil {
			instances, accepted, review = len(it.Output.Instances), it.Output.Counts.Accept, it.Output.Counts.Review
		}
		// "True" / "False" and plain decimals, so the file is the same in every language of this use case.
		w.Write([]string{it.ID, it.Status, num(round(list, 6)), num(round(charged, 6)), pyBool(replayed),
			strings.Join(names, ","), strconv.Itoa(queue), strconv.Itoa(processing), strconv.Itoa(instances), strconv.Itoa(accepted), strconv.Itoa(review)})
	}
	w.Flush()
	return os.WriteFile(path, buf.Bytes(), 0o644)
}

// endregion

func round(x float64, places int) float64 {
	p := math.Pow10(places)
	return math.Round(x*p) / p
}

func pyBool(b bool) string {
	if b {
		return "True"
	}
	return "False"
}

func num(x float64) string { return strconv.FormatFloat(x, 'f', -1, 64) }

func jsonString(s string) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.Encode(s)
	return strings.TrimRight(buf.String(), "\n")
}
