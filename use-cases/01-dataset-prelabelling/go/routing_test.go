package main

import (
	"reflect"
	"testing"
)

func TestRoute(t *testing.T) {
	text := thresholds{Accept: 0.6, Review: 0.3}
	boxes := thresholds{Accept: 0.6, Review: 0.3, Boxes: true}
	for _, tc := range []struct {
		name  string
		score float64
		t     thresholds
		want  string
	}{
		{"above accept", 0.95, text, "accept"},
		{"exactly accept", 0.6, text, "accept"},
		{"just below accept", 0.5999, text, "review"},
		{"exactly review", 0.3, text, "review"},
		{"below review is dropped", 0.2999, text, ""},
		{"a box is never dropped", 0.05, boxes, "review"},
		{"a box can still be accepted", 0.8, boxes, "accept"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := route(tc.score, tc.t); got != tc.want {
				t.Errorf("route(%v) = %q, want %q", tc.score, got, tc.want)
			}
		})
	}
}

func TestDetections(t *testing.T) {
	unit := &Unit{Labels: []string{"Dog", "Cat"}, Item: manifestItem{ExpectedClasses: []string{"Dog", "Cat", "Bird"}}}
	res := &segResult{Instances: []instance{
		{Score: 0.5, BBox: [4]int{0, 0, 10, 10}, Prompt: 1},
		{Score: 0.9, BBox: [4]int{5, 5, 20, 20}, Prompt: 0},
		{Score: 0.9, BBox: [4]int{1, 5, 20, 20}, Prompt: 0},
		{Score: 0.1, BBox: [4]int{0, 0, 1, 1}, Prompt: 0}, // below review: dropped
	}}
	found, missing := detections(unit, res, thresholds{Accept: 0.6, Review: 0.3}, false)
	got := []string{}
	for _, d := range found {
		got = append(got, d.Class+"/"+d.Route)
	}
	if want := []string{"Dog/accept", "Dog/accept", "Cat/review"}; !reflect.DeepEqual(got, want) {
		t.Errorf("found %v, want %v", got, want)
	}
	if found[0].BBox[0] != 1 || found[1].BBox[0] != 5 { // equal scores: by box
		t.Errorf("equal scores must be ordered by box, got %v then %v", found[0].BBox, found[1].BBox)
	}
	if !reflect.DeepEqual(missing, []string{"Bird"}) {
		t.Errorf("missing %v, want [Bird]", missing)
	}
}
