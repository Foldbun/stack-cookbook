package main

import (
	"encoding/csv"
	"encoding/json"
	"image"
	"image/color"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"stack-cookbook/01-dataset-prelabelling/stackclient"
)

func TestGridRingShiftsByHalfAPixel(t *testing.T) {
	got := gridRing([]float64{0, 0, 9, 4})
	if want := []float64{0.5, 0.5, 9.5, 4.5}; !reflect.DeepEqual(got, want) {
		t.Errorf("gridRing = %v, want %v", got, want)
	}
}

func TestCocoAnnotation(t *testing.T) {
	d := Detection{Class: "Dog", Score: 0.98414, BBox: [4]int{10, 20, 50, 80}, Area: 1200, Route: "accept", Polygon: [][]float64{{10, 20, 49, 20, 49, 79}}}
	a, err := cocoAnn(7, 2, 3, d, 100, 100)
	if err != nil {
		t.Fatal(err)
	}
	if a.BBox != [4]int{10, 20, 40, 60} {
		t.Errorf("bbox = %v, want [x y width height] = [10 20 40 60]", a.BBox)
	}
	if want := [][]float64{{10.5, 20.5, 49.5, 20.5, 49.5, 79.5}}; !reflect.DeepEqual(a.Segmentation, want) {
		t.Errorf("segmentation = %v, want %v", a.Segmentation, want)
	}
	if a.Score != 0.9841 || a.Area != 1200 || a.ID != 7 || a.ImageID != 2 || a.CategoryID != 3 {
		t.Errorf("annotation = %+v", a)
	}
}

func TestFitMaskResamplesToTheImageGrid(t *testing.T) {
	small := stackclient.NewBitmap(2, 2) // top-left pixel set
	small.Pix[0] = 1
	m := rle{Size: [2]int{2, 2}, Counts: stackclient.EncodeCOCORLE(small)}
	for _, tc := range []struct {
		name          string
		height, width int
		area          int
	}{
		{"same grid", 2, 2, 1},
		{"twice as large", 4, 4, 4},
		{"wider than tall", 2, 6, 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := fitMask(m, tc.height, tc.width)
			if err != nil {
				t.Fatal(err)
			}
			area, err := rleArea(got)
			if err != nil || got.Size != [2]int{tc.height, tc.width} || area != tc.area {
				t.Errorf("size %v area %d (%v), want %dx%d area %d", got.Size, area, err, tc.height, tc.width, tc.area)
			}
		})
	}
}

func TestYOLOLinesAreNormalisedAndClamped(t *testing.T) {
	root := t.TempDir()
	label := func(name string) string {
		data, _ := os.ReadFile(filepath.Join(root, "labels", name))
		return string(data)
	}
	classes := []string{"Dog", "Cat"}
	recs := []record{
		{ID: "a", File: "images/a.jpg", Width: 200, Height: 100, Detections: []Detection{
			{Class: "Cat", Route: "accept", Polygon: [][]float64{{0, 0, 199, 99}}},
			{Class: "Dog", Route: "review", Polygon: [][]float64{{1, 1, 2, 2}}}, // review rows are not exported
		}},
		{ID: "b", File: "images/b.jpg", Width: 10, Height: 10},
	}
	if err := writeYOLO(root, recs, classes); err != nil {
		t.Fatal(err)
	}
	if want := "1 0.002500 0.005000 0.997500 0.995000\n"; label("a.txt") != want { // (v + 0.5) / size
		t.Errorf("a.txt = %q, want %q", label("a.txt"), want)
	}
	if label("b.txt") != "" {
		t.Errorf("an image without detections gets an empty file, got %q", label("b.txt"))
	}
	yaml, _ := os.ReadFile(filepath.Join(root, "data.yaml"))
	if !strings.Contains(string(yaml), "  0: \"Dog\"\n  1: \"Cat\"\n") {
		t.Errorf("data.yaml = %q", yaml)
	}
	recs[0].Detections[0].Polygon = [][]float64{{-5, 0, 300, 99}} // outside the image: clamped to 0..1
	if err := writeYOLO(root, recs[:1], classes); err != nil {
		t.Fatal(err)
	}
	if want := "1 0.000000 0.005000 1.000000 0.995000\n"; label("a.txt") != want {
		t.Errorf("clamped a.txt = %q, want %q", label("a.txt"), want)
	}
}

func TestCostsCSV(t *testing.T) {
	cache := "miss"
	out := &itemOutput{Instances: []outInstance{{}, {}, {}}}
	out.Counts.Accept, out.Counts.Review = 2, 1
	items := []itemResult{
		{ID: "x", Status: "ok", Output: out, Calls: []stackclient.Row{{List: 0.7, Charged: 0.7, Cache: &cache, QueueMs: 3, ProcessingMs: 500}}},
		{ID: "y", Status: "ok", Output: out, Calls: []stackclient.Row{{List: 0.7, Charged: 0, Replayed: true, QueueMs: 1, ProcessingMs: 2}}},
		{ID: "z", Status: "error", Calls: []stackclient.Row{}},
	}
	path := filepath.Join(t.TempDir(), "costs.csv")
	if err := writeCosts(path, items); err != nil {
		t.Fatal(err)
	}
	f, _ := os.Open(path)
	defer f.Close()
	rows, _ := csv.NewReader(f).ReadAll()
	want := [][]string{
		costFields,
		{"x", "ok", "0.7", "0.7", "False", "miss", "3", "500", "3", "2", "1"},
		{"y", "ok", "0", "0", "True", "", "1", "2", "3", "2", "1"}, // replayed: list price and charge are not counted again
		{"z", "error", "0", "0", "False", "", "0", "0", "0", "0", "0"},
	}
	if !reflect.DeepEqual(rows, want) {
		t.Errorf("costs.csv = %v\nwant %v", rows, want)
	}
}

func TestFailedItemKeepsNullOutputAndEmptyCalls(t *testing.T) {
	data, _ := json.Marshal(itemResult{ID: "x", Status: "error", Calls: []stackclient.Row{}, Error: localError("no_prompts", "m")})
	if !strings.Contains(string(data), `"calls":[]`) || !strings.Contains(string(data), `"output":null`) {
		t.Errorf("item = %s", data)
	}
}

func TestExifOrientationAndUpright(t *testing.T) {
	// a JPEG header with one EXIF IFD entry: orientation 6 (rotate 90 degrees clockwise to view upright)
	jpegHead := []byte{0xFF, 0xD8, 0xFF, 0xE1, 0x00, 0x22, 'E', 'x', 'i', 'f', 0, 0,
		'M', 'M', 0, 42, 0, 0, 0, 8, 0, 1, 0x01, 0x12, 0, 3, 0, 0, 0, 1, 0, 6, 0, 0, 0, 0, 0, 0}
	if got := exifOrientation(jpegHead); got != 6 {
		t.Errorf("orientation = %d, want 6", got)
	}
	if got := exifOrientation([]byte("not a jpeg")); got != 1 {
		t.Errorf("orientation of a non-JPEG = %d, want 1", got)
	}
	// 2 wide, 1 high: left pixel red, right pixel blue
	img := image.NewNRGBA(image.Rect(0, 0, 2, 1))
	red, blue := color.NRGBA{255, 0, 0, 255}, color.NRGBA{0, 0, 255, 255}
	img.SetNRGBA(0, 0, red)
	img.SetNRGBA(1, 0, blue)
	for _, tc := range []struct {
		orientation int
		w, h        int
		at00        color.NRGBA
	}{
		{1, 2, 1, red},
		{2, 2, 1, blue}, // mirrored
		{3, 2, 1, blue}, // rotated by 180
		{6, 1, 2, red},  // clockwise: the left end becomes the top
		{8, 1, 2, blue}, // counter-clockwise: the right end becomes the top
	} {
		got := upright(img, tc.orientation)
		b := got.Bounds()
		if b.Dx() != tc.w || b.Dy() != tc.h || color.NRGBAModel.Convert(got.At(b.Min.X, b.Min.Y)) != tc.at00 {
			t.Errorf("orientation %d: %dx%d, top-left %v; want %dx%d, %v", tc.orientation, b.Dx(), b.Dy(), got.At(b.Min.X, b.Min.Y), tc.w, tc.h, tc.at00)
		}
	}
}
