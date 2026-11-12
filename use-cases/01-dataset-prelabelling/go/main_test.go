package main

import (
	"encoding/json"
	"reflect"
	"testing"

	"stack-cookbook/01-dataset-prelabelling/stackclient"
)

func TestLedgerCountsReplayedRowsButNotTheirPrice(t *testing.T) {
	items := []itemResult{
		{Calls: []stackclient.Row{{List: 0.7, Charged: 0.7}}},
		{Calls: []stackclient.Row{{List: 0.7, Replayed: true}, {List: 0.1, Charged: 0.1}}}, // a resumed row, and a fresh one
		{Calls: []stackclient.Row{}},
	}
	got := ledgerOf(items)
	if got.Calls != 3 || got.ListTotal != 0.8 || got.ChargedTotal != 0.8 {
		t.Errorf("ledger = %+v, want 3 calls, 0.8 list, 0.8 charged", got)
	}
}

func TestParseArgsRules(t *testing.T) {
	for _, tc := range []struct {
		name  string
		args  []string
		ok    bool
		mode  string
		model string
	}{
		{"text mode is the default", []string{"--manifest", "m.json"}, true, "text", "sam3"},
		{"boxes imply sam-lite", []string{"--manifest", "m.json", "--boxes", "b.csv"}, true, "boxes", "sam-lite"},
		{"exemplar runs on sam3", []string{"--manifest", "m.json", "--exemplar", "e.json"}, true, "exemplar", "sam3"},
		{"no manifest", []string{}, false, "", ""},
		{"boxes need sam-lite", []string{"--manifest", "m.json", "--boxes", "b.csv", "--model", "sam3"}, false, "", ""},
		{"sam-lite needs boxes", []string{"--manifest", "m.json", "--model", "sam-lite"}, false, "", ""},
		{"two modes", []string{"--manifest", "m.json", "--boxes", "b.csv", "--exemplar", "e.json"}, false, "", ""},
		{"review above accept", []string{"--manifest", "m.json", "--accept", "0.3", "--review", "0.6"}, false, "", ""},
		{"batch too large", []string{"--manifest", "m.json", "--batch-size", "1001"}, false, "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, err := parseArgs(tc.args)
			if (err == nil) != tc.ok {
				t.Fatalf("err = %v, want ok = %v", err, tc.ok)
			}
			if tc.ok && (c.Mode != tc.mode || c.Model != tc.model) {
				t.Errorf("mode %s model %s, want %s %s", c.Mode, c.Model, tc.mode, tc.model)
			}
		})
	}
}

func TestBuildBodyAndPrompts(t *testing.T) {
	m := manifest{Items: []manifestItem{{ID: "a", Prompts: []string{"dog", "cow"}}}}
	m.Classes = append(m.Classes, struct {
		Name   string `json:"name"`
		Prompt string `json:"prompt"`
	}{"Dog", "dog"})
	classes := classNames(m, m.Items)
	if !reflect.DeepEqual(classes.Names, []string{"Dog", "cow"}) { // a prompt without a class stands for itself
		t.Errorf("names = %v", classes.Names)
	}
	u := &Unit{Item: m.Items[0], Handle: "img_1"}
	c := &config{Mode: "text", Review: 0.3}
	u.Prompts, u.Labels = buildPrompts(u, c, classes, &extra{})
	if !reflect.DeepEqual(u.Labels, []string{"Dog", "cow"}) {
		t.Errorf("labels = %v", u.Labels)
	}
	got, _ := json.Marshal(buildBody(u, c))
	want := `{"image":{"id":"img_1"},"options":{"defaults":{"filter":{"minScore":0.3},"geometry":["polygon"],"polygon":{"holes":false,"tolerancePx":1.0}},"nms":{"iou":0.7,"metric":"mask","scope":"group"}},"prompts":[{"text":"dog"},{"text":"cow"}]}`
	if string(got) != want {
		t.Errorf("body = %s\nwant   %s", got, want)
	}

	// masks add the mask geometry and a grid at image size; boxes have no filter and no overlap removal
	c = &config{Mode: "boxes", Masks: true}
	u.Prompts = []map[string]any{{"box": [4]int{1, 2, 3, 4}}}
	got, _ = json.Marshal(buildBody(u, c))
	want = `{"image":{"id":"img_1"},"options":{"defaults":{"geometry":["polygon","mask"],"polygon":{"holes":false,"tolerancePx":1.0}},"maskSize":4096},"prompts":[{"box":[1,2,3,4]}]}`
	if string(got) != want {
		t.Errorf("boxes body = %s\nwant        %s", got, want)
	}

	// exemplar mode: positive boxes first, then negative ones, all labelled with the exemplar class
	ex := &extra{Class: "Bird"}
	ex.Items = map[string]struct {
		Positive [][4]int `json:"positive"`
		Negative [][4]int `json:"negative"`
	}{"a": {Positive: [][4]int{{1, 1, 5, 5}}, Negative: [][4]int{{6, 6, 9, 9}}}}
	prompts, labels := buildPrompts(u, &config{Mode: "exemplar"}, classes, ex)
	if len(prompts) != 2 || prompts[0]["positive"] != true || prompts[1]["positive"] != false || !reflect.DeepEqual(labels, []string{"Bird", "Bird"}) {
		t.Errorf("exemplar prompts = %v, labels = %v", prompts, labels)
	}
}
