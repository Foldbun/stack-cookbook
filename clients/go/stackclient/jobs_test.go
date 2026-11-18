// Async jobs of the Go client against the fake API, with the same checks as the Python and TypeScript tests.
package stackclient

import (
	"math"
	"net/http"
	"strings"
	"testing"
	"time"
)

func near(t *testing.T, got, want float64) {
	t.Helper()
	if math.Abs(got-want) > 1e-9 {
		t.Errorf("got %v, want %v", got, want)
	}
}

// resetWith resets the fake API to a scenario written inline.
func resetWith(t *testing.T, body string) string {
	t.Helper()
	base := fakeURL(t)
	resp, err := http.Post(base+"/_fake/reset", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	t.Cleanup(func() {
		if v := fakeLog(t).Violations; len(v) > 0 {
			t.Errorf("the fake API reports violations of the API description: %v", v)
		}
	})
	return base
}

func jobItems(t *testing.T, c *Client, n int) []map[string]any {
	t.Helper()
	items := []map[string]any{}
	for i := 0; i < n; i++ {
		items = append(items, map[string]any{"image": ImageHandle(uploadPNG(t, c, i))})
	}
	return items
}

func waitCompleted(t *testing.T, c *Client, id string) Job {
	t.Helper()
	for i := 0; i < 20; i++ {
		job, err := c.GetJob(id)
		if err != nil {
			t.Fatal(err)
		}
		if job.Status == "completed" {
			return job
		}
	}
	t.Fatal("the job did not complete")
	return Job{}
}

func results(t *testing.T, c *Client, id string, o Opts) []JobItem {
	t.Helper()
	items := []JobItem{}
	if err := c.JobResults(id, o, func(it JobItem) error { items = append(items, it); return nil }); err != nil {
		t.Fatal(err)
	}
	return items
}

func TestJobRunsToCompletionAndLandsInTheLedger(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{})
	items := jobItems(t, c, 5)
	job, err := c.CreateJob("siglip2", items, map[string]string{"dataset": "t"}, Opts{Key: "job-key"})
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != "queued" || job.Counts.Total != 5 || job.Estimate != 0.025 || strings.ReplaceAll(string(job.Metadata), " ", "") != `{"dataset":"t"}` {
		t.Errorf("job %+v", job)
	}
	entries := calls(t, "/v1/jobs")
	if len(entries) != 1 || entries[0].Status != 202 || entries[0].IdempotencyKey != "job-key" || !entries[0].HasAuthorization {
		t.Errorf("log %+v", entries)
	}
	near(t, c.reserved, 0.025) // the worst case is held until the job is final
	running, err := c.GetJob(job.ID)
	if err != nil || running.Status != "running" || running.Counts.OK == 0 || running.Counts.OK >= 5 {
		t.Errorf("running %+v err %v", running, err)
	}
	done := waitCompleted(t, c, job.ID)
	if done.Counts.OK != 5 || done.Cost != 0.025 || !done.Final() {
		t.Errorf("done %+v", done)
	}
	near(t, c.reserved, 0)
	near(t, c.spent, 0.025)
	got := results(t, c, job.ID, Opts{Tag: "j"})
	if len(got) != 5 || got[0].Index != 0 || got[4].Index != 4 {
		t.Fatalf("items %+v", got)
	}
	rows := c.Calls("j")
	model, miss := "siglip2", "miss"
	want := Row{Endpoint: "/v1/jobs", Model: &model, List: 0.005, Charged: 0.005, Cache: &miss, QueueMs: 4, ProcessingMs: 80}
	if len(rows) != 5 || rows[0].Endpoint != want.Endpoint || *rows[0].Model != model || rows[0].List != want.List || rows[0].Charged != want.Charged ||
		*rows[0].Cache != miss || rows[0].QueueMs != 4 || rows[0].ProcessingMs != 80 || rows[0].Replayed {
		t.Errorf("rows %+v", rows)
	}
	if s := c.Summary(); s != (Summary{Calls: 5, ListTotal: 0.025, ChargedTotal: 0.025}) {
		t.Errorf("summary %+v", s)
	}
	near(t, c.spent, 0.025) // reading the results does not count the spend twice
	results(t, c, job.ID, Opts{})
	if n := c.Summary().Calls; n != 5 { // nor does reading them again
		t.Errorf("%d ledger rows after a second read", n)
	}
}

func TestJobResultsPageByCursorAndCacheHitsAreFree(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{})
	items := jobItems(t, c, 4)
	first, err := c.CreateJob("siglip2", items, nil, Opts{})
	if err != nil {
		t.Fatal(err)
	}
	waitCompleted(t, c, first.ID)
	results(t, c, first.ID, Opts{})
	second, err := c.CreateJob("siglip2", items, nil, Opts{}) // the same four images again: cached, so free
	if err != nil {
		t.Fatal(err)
	}
	waitCompleted(t, c, second.ID)
	got := results(t, c, second.ID, Opts{PageSize: 2, Tag: "again"})
	if len(got) != 4 || got[3].Index != 3 {
		t.Errorf("items %+v", got)
	}
	if n := len(calls(t, "/v1/jobs/"+second.ID+"/results")); n != 3 { // 2 + 2 + the empty last page
		t.Errorf("%d result requests", n)
	}
	for _, r := range c.Calls("again") {
		if r.Cache == nil || *r.Cache != "hit" {
			t.Errorf("row %+v", r)
		}
	}
	if s := c.Summary(); s.ChargedTotal != 0.02 {
		t.Errorf("summary %+v", s)
	}
}

func TestJobWithFailedItems(t *testing.T) {
	c := newClient(t, scenario(t, "job-partial"), Config{})
	job, err := c.CreateJob("siglip2", jobItems(t, c, 4), nil, Opts{})
	if err != nil {
		t.Fatal(err)
	}
	waitCompleted(t, c, job.ID)
	items := results(t, c, job.ID, Opts{Tag: "p"})
	got := []string{}
	for _, it := range items {
		got = append(got, it.Status)
	}
	if strings.Join(got, ",") != "ok,error,ok,ok" || items[1].Error == nil || items[1].Error.Code != "bad_model_output" {
		t.Errorf("items %+v", items)
	}
	if n := len(c.Calls("p")); n != 3 {
		t.Errorf("%d rows", n)
	}
	if j, _ := c.GetJob(job.ID); j.Counts != (JobCounts{Total: 4, OK: 3, Error: 1}) {
		t.Errorf("counts %+v", j.Counts)
	}
}

func TestJobTextItemsAndIdempotentReplay(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{})
	items := []map[string]any{{"texts": []string{"a dog", "a cat"}}, {"texts": []string{"a bird"}}}
	a, err := c.CreateJob("siglip2", items, nil, Opts{Key: "same"})
	if err != nil {
		t.Fatal(err)
	}
	b, err := c.CreateJob("siglip2", items, nil, Opts{Key: "same"})
	if err != nil || a.ID != b.ID {
		t.Fatalf("ids %q %q err %v", a.ID, b.ID, err)
	}
	near(t, c.reserved, a.Estimate) // one hold, not two
	near(t, a.Estimate, 0.003)
	_, err = c.CreateJob("siglip2", items[:1], nil, Opts{Key: "same"})
	if e := wantCode(t, err, "idempotency_conflict"); e.Status != 409 {
		t.Errorf("status %d", e.Status)
	}
	if _, err := c.CancelJob(a.ID); err != nil {
		t.Fatal(err)
	}
	near(t, c.reserved, 0)
}

func TestCancelJob(t *testing.T) {
	c := newClient(t, scenario(t, ""), Config{})
	job, err := c.CreateJob("dinov3", jobItems(t, c, 3), nil, Opts{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.GetJob(job.ID); err != nil { // one poll: part of the items ran
		t.Fatal(err)
	}
	cancelled, err := c.CancelJob(job.ID)
	if err != nil || cancelled.Status != "cancelled" || cancelled.Counts.Cancelled == 0 {
		t.Fatalf("cancelled %+v err %v", cancelled, err)
	}
	n := 0
	for _, it := range results(t, c, job.ID, Opts{}) {
		if it.Status == "cancelled" {
			n++
		}
	}
	if n != cancelled.Counts.Cancelled {
		t.Errorf("%d cancelled items, counts say %d", n, cancelled.Counts.Cancelled)
	}
	near(t, c.reserved, 0)
	near(t, c.spent, cancelled.Cost)
	_, err = c.CancelJob(job.ID)
	if e := wantCode(t, err, "job_finished"); e.Status != 409 {
		t.Errorf("status %d", e.Status)
	}
}

func TestJobIsRefusedLocallyWithoutAKeyOrOverTheCap(t *testing.T) {
	base := scenario(t, "")
	f := freeClient(t, base, Config{})
	gone := "job_" + strings.Repeat("0", 32)
	_, err := f.CreateJob("siglip2", []map[string]any{}, nil, Opts{})
	wantCode(t, err, "missing_api_key")
	_, err = f.GetJob(gone)
	wantCode(t, err, "missing_api_key")
	_, err = f.CancelJob(gone)
	wantCode(t, err, "missing_api_key")
	wantCode(t, f.JobResults(gone, Opts{}, func(JobItem) error { return nil }), "missing_api_key")
	limit := 0.01
	c := newClient(t, base, Config{MaxCredits: &limit})
	items := jobItems(t, c, 3) // the worst case is 3 x 0.005
	_, err = c.CreateJob("siglip2", items, nil, Opts{})
	e := wantCode(t, err, "budget_exceeded")
	if e.Message != "call refused: up to 0.015 credits would pass FOLDBUN_MAX_CREDITS=0.01 (spent 0)" {
		t.Errorf("message %q", e.Message)
	}
	if n := len(calls(t, "/v1/jobs")); n != 0 {
		t.Errorf("%d job requests sent", n)
	}
}

func TestJobSubmitRetriesTooManyJobs(t *testing.T) {
	c := newClient(t, scenario(t, "job-too-many"), Config{})
	items := jobItems(t, c, 1)
	t0 := time.Now()
	job, err := c.CreateJob("siglip2", items, nil, Opts{Key: "retry-key"})
	if err != nil || job.Status != "queued" || time.Since(t0) < 1200*time.Millisecond {
		t.Fatalf("job %+v err %v after %v", job, err, time.Since(t0))
	}
	es := calls(t, "/v1/jobs")
	if len(es) != 2 || es[0].Status != 429 || es[1].Status != 202 || es[0].IdempotencyKey != "retry-key" || es[1].IdempotencyKey != "retry-key" {
		t.Errorf("log %+v", es)
	}
}

func TestJobSubmitWithoutBalanceIs402(t *testing.T) {
	c := newClient(t, resetWith(t, `{"balance": 0.001}`), Config{})
	items := jobItems(t, c, 2)
	_, err := c.CreateJob("siglip2", items, nil, Opts{})
	if e := wantCode(t, err, "insufficient_balance"); e.Status != 402 {
		t.Errorf("status %d", e.Status)
	}
	near(t, c.reserved, 0)
}
