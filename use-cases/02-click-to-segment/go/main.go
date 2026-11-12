// Click-to-segment backend: go run . --port 8080 [--host 127.0.0.1] [--static ../web/dist].
// Holds the API key; see contract/02-backend.yaml.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"stack-cookbook/02-click-to-segment/stackclient"
)

const (
	exitFailed = 1
	exitNoKey  = 2
)

func main() { os.Exit(run(os.Args[1:])) }

// modelsFromEnv reads SEGMENTER_MODELS, a comma-separated list; empty means the default (sam-lite).
func modelsFromEnv() []string {
	var models []string
	for _, m := range strings.Split(os.Getenv("SEGMENTER_MODELS"), ",") {
		if m = strings.TrimSpace(m); m != "" {
			models = append(models, m)
		}
	}
	return models
}

// region server

func run(args []string) int {
	fs := flag.NewFlagSet("backend", flag.ContinueOnError)
	port := fs.Int("port", 8080, "port to listen on")
	host := fs.String("host", "127.0.0.1", "address to listen on")
	static := fs.String("static", "../web/dist", "directory with the page's files, served at /")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return exitFailed
	}
	// key, base URL and credit cap come from FOLDBUN_API_KEY, FOLDBUN_API_BASE, FOLDBUN_MAX_CREDITS
	client, err := stackclient.New(stackclient.Config{})
	if err != nil {
		if stackclient.Code(err) == "missing_api_key" {
			fmt.Fprintln(os.Stderr, stackclient.MissingKey)
			return exitNoKey
		}
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	logger := log.New(os.Stderr, "", log.LstdFlags)
	backend := NewBackend(client, Options{Models: modelsFromEnv(), StaticDir: *static, Log: func(line string) { logger.Println(line) }})
	ln, err := net.Listen("tcp", net.JoinHostPort(*host, strconv.Itoa(*port)))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	}
	srv := &http.Server{
		Handler:           backend,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       60 * time.Second, // up to 20 MiB of image
		WriteTimeout:      5 * time.Minute,  // a click may wait for the retries of the client file
		IdleTimeout:       60 * time.Second,
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	served := make(chan error, 1)
	go func() { served <- srv.Serve(ln) }()
	fmt.Printf("listening on http://%s\n", net.JoinHostPort(*host, strconv.Itoa(ln.Addr().(*net.TCPAddr).Port)))
	select {
	case err := <-served:
		fmt.Fprintln(os.Stderr, err)
		return exitFailed
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 4*time.Second) // the contract allows 5 s
	defer cancel()
	if srv.Shutdown(shutdown) != nil {
		srv.Close()
	}
	return 0
}

// endregion
