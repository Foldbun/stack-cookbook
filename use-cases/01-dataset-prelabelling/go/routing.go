package main

import "sort"

// Turn the API's instances into labels, and decide which ones a person has to look at.
//
// Three bands, by `score` (for text prompts the detection confidence, for boxes the predicted mask IoU):
//
//	score >= accept            the label is exported as it is;
//	review <= score < accept   the label is kept in the review queue, with a crop;
//	no instance of a class an image is known to contain (`expectedClasses` in the manifest): the image goes to the
//	review queue for that class, because a silent miss is the failure that pre-labelling hides best.
//
// The request asks the API for everything above `review` (`filter.minScore`), so nothing below it is ever returned.

// instance is one object of a segment result; the fields the exports need.
type instance struct {
	Score   float64 `json:"score"`
	BBox    [4]int  `json:"bbox"` // [x1, y1, x2, y2], x2 and y2 exclusive, image pixels
	Area    int     `json:"area"`
	Prompt  int     `json:"prompt"`
	Polygon []struct {
		Outer []float64 `json:"outer"` // flat [x, y, ...] at pixel centres
	} `json:"polygon"`
	Mask *rle `json:"mask"`
}

// rle is a COCO RLE mask on the mask grid; `size` is [height, width].
type rle struct {
	Encoding string `json:"encoding,omitempty"`
	Size     [2]int `json:"size"`
	Counts   string `json:"counts"`
}

// segResult is the part of a segment result that is kept: the account's balance is not.
type segResult struct {
	Instances []instance `json:"instances"`
}

// Detection is an instance that passed routing.
type Detection struct {
	Class   string
	Score   float64
	BBox    [4]int
	Area    int
	Route   string      // "accept" or "review"
	Polygon [][]float64 // rings of [x, y, ...] at pixel centres; outer rings only
	Mask    *rle        // only with --masks
}

// thresholds are the routing settings of a run.
type thresholds struct {
	Accept, Review float64
	Boxes          bool // box mode: a box the user supplied is never dropped, only checked
}

// region routing
// route is the band of a score: "accept", "review", or "" when it is below the review threshold.
func route(score float64, t thresholds) string {
	if score >= t.Accept {
		return "accept"
	}
	if score >= t.Review || t.Boxes {
		return "review"
	}
	return ""
}

// detections returns the detections of one item (sorted by score), and the expected classes for which nothing was found.
func detections(u *Unit, res *segResult, t thresholds, masks bool) ([]Detection, []string) {
	found := []Detection{}
	for _, inst := range res.Instances {
		band := route(inst.Score, t)
		if band == "" {
			continue
		}
		d := Detection{Class: u.Labels[inst.Prompt], Score: inst.Score, BBox: inst.BBox, Area: inst.Area, Route: band}
		for _, part := range inst.Polygon {
			d.Polygon = append(d.Polygon, part.Outer)
		}
		if masks {
			d.Mask = inst.Mask
		}
		found = append(found, d)
	}
	sort.SliceStable(found, func(i, j int) bool {
		a, b := found[i], found[j]
		if a.Score != b.Score {
			return a.Score > b.Score
		}
		return lessBox(a.BBox, b.BBox)
	})
	seen := map[string]bool{}
	for _, d := range found {
		seen[d.Class] = true
	}
	missing := []string{}
	for _, c := range u.Item.ExpectedClasses {
		if !seen[c] {
			missing = append(missing, c)
		}
	}
	return found, missing
}

// endregion

func lessBox(a, b [4]int) bool {
	for i := range a {
		if a[i] != b[i] {
			return a[i] < b[i]
		}
	}
	return false
}
