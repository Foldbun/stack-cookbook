package main

// Static files of the page: GET / serves index.html, GET /<name> a regular file directly inside the directory.
// Nothing under /api/ comes from here, and there is no CORS: the page and the backend share one origin.

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

var contentTypes = map[string]string{
	".html": "text/html; charset=utf-8",
	".js":   "text/javascript; charset=utf-8",
	".mjs":  "text/javascript; charset=utf-8",
	".css":  "text/css; charset=utf-8",
	".svg":  "image/svg+xml",
	".png":  "image/png",
	".jpg":  "image/jpeg",
	".jpeg": "image/jpeg",
	".webp": "image/webp",
	".json": "application/json",
	".ico":  "image/x-icon",
}

// fileName is the name of the file a URL path asks for, or "" when the path is not one the backend serves: only
// names directly inside the directory count, so no separator, no dot files and no "..".
func fileName(urlPath string) string {
	name := strings.TrimPrefix(urlPath, "/")
	if name == "" {
		return "index.html"
	}
	if strings.ContainsAny(name, "/\\\x00") || strings.HasPrefix(name, ".") {
		return ""
	}
	return name
}

// static serves one file for GET and HEAD. Any other method is 405 with Allow, whatever the name; a name that is not
// served (also a missing directory or file) is 404 not_found.
func (b *Backend) static(w http.ResponseWriter, r *http.Request) error {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		e := newError(405, "method_not_allowed", "use GET or HEAD")
		e.Allow = "GET, HEAD"
		return e
	}
	notFound := newError(404, "not_found", "no route "+r.URL.Path)
	name := fileName(r.URL.Path)
	if b.staticDir == "" || name == "" {
		return notFound
	}
	path := filepath.Join(b.staticDir, name)
	info, err := os.Lstat(path) // a symbolic link is not a regular file: it could point out of the directory
	if err != nil || !info.Mode().IsRegular() {
		return notFound
	}
	f, err := os.Open(path)
	if err != nil {
		return notFound
	}
	defer f.Close()
	ct := contentTypes[strings.ToLower(filepath.Ext(name))]
	if ct == "" {
		ct = "application/octet-stream"
	}
	w.Header().Set("Content-Type", ct)
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	http.ServeContent(w, r, name, info.ModTime(), f)
	return nil
}
