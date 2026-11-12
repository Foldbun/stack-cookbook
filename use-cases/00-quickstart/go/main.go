// First call to the Foldbun Stack API: check the key, upload an image, read its text, show the cost.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"stack-cookbook/00-quickstart/stackclient"
)

const (
	exitFailed = 1
	exitNoKey  = 2
	exitTier   = 3
	exitBudget = 4
)

type item struct {
	ID         string `json:"id"`
	Image      string `json:"image"`
	ImageInput string `json:"imageInput"`
}

type line struct {
	Text       string          `json:"text"`
	Confidence float64         `json:"confidence"`
	BBox       json.RawMessage `json:"bbox"`
}

type output struct {
	Image struct {
		Width  int `json:"width"`
		Height int `json:"height"`
	} `json:"image"`
	Lines []line `json:"lines"`
}

type itemResult struct {
	ID     string            `json:"id"`
	Status string            `json:"status"`
	Calls  []stackclient.Row `json:"calls"`
	Output *output           `json:"output"`
	Error  *itemError        `json:"error,omitempty"`
}

type runResult struct {
	Usecase   string              `json:"usecase"`
	Language  string              `json:"language"`
	Client    string              `json:"client"`
	StartedAt string              `json:"startedAt"`
	ElapsedMs int64               `json:"elapsedMs"`
	Items     []itemResult        `json:"items"`
	Ledger    stackclient.Summary `json:"ledger"`
}

type itemError struct {
	Status  int    `json:"status"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

func readText(client *stackclient.Client, it item, base string) (*output, error) {
	data, err := os.ReadFile(filepath.Join(base, it.Image))
	if err != nil {
		return nil, err
	}
	contentType := "image/jpeg"
	if strings.HasSuffix(it.Image, ".webp") {
		contentType = "image/webp"
	} else if strings.HasSuffix(it.Image, ".png") {
		contentType = "image/png"
	}
	// region image-input
	var image map[string]string
	if it.ImageInput == "base64" {
		image = stackclient.ImageBase64(data) // inline: simplest for a single call
	} else {
		uploaded, err := client.Upload(data, contentType) // free; the handle can be reused for 7 days at most
		if err != nil {
			return nil, err
		}
		image = stackclient.ImageHandle(uploaded.ID)
	}
	// endregion
	// region first-call
	raw, err := client.Post("/v1/ocr", map[string]any{"model": "ppocr-v6", "image": image}, stackclient.Opts{Tag: it.ID})
	if err != nil {
		return nil, err
	}
	var result struct {
		output
		Cost    float64 `json:"cost"`
		Billing struct {
			Total     float64 `json:"total"`
			Pool      string  `json:"pool"`
			Allowance struct{ Used, Limit int }
		} `json:"billing"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return nil, err
	}
	for _, l := range result.Lines {
		fmt.Printf("  %.2f  %s\n", l.Confidence, l.Text)
	}
	b, covered := result.Billing, "billed"
	if b.Pool != "" {
		covered = fmt.Sprintf("covered by the %s pool (%d/%d today)", b.Pool, b.Allowance.Used, b.Allowance.Limit)
	}
	fmt.Printf("  list price %g credits, charged %g (%s)\n", b.Total, result.Cost, covered)
	// endregion
	return &result.output, nil
}

func run() int {
	manifestPath := flag.String("manifest", "../assets/smoke/manifest.json", "manifest.json of the run")
	out := flag.String("out", "out", "directory for result.json")
	concurrency := flag.Int("concurrency", 4, "requests in flight")
	limit := flag.Int("limit", 0, "only the first N items (0: all)")
	flag.Bool("resume", false, "accepted for the common interface; every call here is cheap")
	flag.Parse()
	raw, err := os.ReadFile(*manifestPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	var manifest struct {
		Usecase string `json:"usecase"`
		Items   []item `json:"items"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		fmt.Fprintln(os.Stderr, "manifest:", err)
		return exitFailed
	}
	started := time.Now().UTC()
	// region client
	client, err := stackclient.New(stackclient.Config{MaxConcurrency: *concurrency}) // FOLDBUN_API_KEY, FOLDBUN_API_BASE
	if stackclient.Code(err) == "missing_api_key" {
		fmt.Fprintln(os.Stderr, stackclient.MissingKey)
		return exitNoKey
	} else if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	models, err := client.Models() // free: checks the key and lists prices
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	fmt.Printf("key accepted; ppocr-v6 available: %t\n", models["ppocr-v6"].Available)
	// endregion
	items, code := []itemResult{}, 0
	for i, it := range manifest.Items {
		if *limit > 0 && i >= *limit {
			break
		}
		fmt.Println(it.ID)
		res := itemResult{ID: it.ID, Status: "ok"}
		res.Output, err = readText(client, it, filepath.Dir(*manifestPath))
		if err != nil {
			var apiErr *stackclient.Error
			if !errors.As(err, &apiErr) {
				fmt.Fprintln(os.Stderr, err)
				return exitFailed
			}
			fmt.Fprintln(os.Stderr, " ", apiErr)
			if apiErr.Code == "model_locked" || apiErr.Code == "budget_exceeded" {
				code = exitTier
				if apiErr.Code == "budget_exceeded" {
					code = exitBudget
				}
				break
			}
			res.Status, res.Error = "error", &itemError{apiErr.Status, apiErr.Code, apiErr.Message}
		}
		res.Calls = client.Calls(it.ID)
		items = append(items, res)
	}
	result := runResult{
		Usecase:   manifest.Usecase,
		Language:  "go",
		Client:    "stackclient " + stackclient.Version,
		StartedAt: started.Format("2006-01-02T15:04:05Z"),
		ElapsedMs: time.Since(started).Milliseconds(),
		Items:     items,
		Ledger:    client.Summary(),
	}
	if err := writeResult(filepath.Join(*out, "result.json"), result); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	return code
}

func writeResult(path string, result any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	return enc.Encode(result)
}

func main() { os.Exit(run()) }
