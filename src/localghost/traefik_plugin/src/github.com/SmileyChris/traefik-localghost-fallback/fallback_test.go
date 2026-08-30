package traefik_localghost_fallback

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeEntry(t *testing.T, dir, name, hostname string) {
	t.Helper()
	payload := `{
  "hostname": "` + hostname + `",
  "name": "` + name + `",
  "directory": "/home/dev/` + name + `",
  "type": "django",
  "last_started": "` + time.Now().Add(-26*time.Hour).Format(time.RFC3339) + `"
}`
	if err := os.WriteFile(filepath.Join(dir, name+".json"), []byte(payload), 0o644); err != nil {
		t.Fatal(err)
	}
}

func handler(t *testing.T, registry string) http.Handler {
	t.Helper()
	next := http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("fallback must not call the next handler")
	})
	config := CreateConfig()
	config.RegistryPath = registry
	h, err := New(context.Background(), next, config, "localghost-fallback")
	if err != nil {
		t.Fatal(err)
	}
	return h
}

func get(h http.Handler, host, accept string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, "http://"+host+"/", nil)
	req.Host = host
	if accept != "" {
		req.Header.Set("Accept", accept)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestKnownHostServes503GhostPage(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	rec := get(handler(t, dir), "blog.localhost", "text/html")
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{"blog", "/home/dev/blog", "localghost run"} {
		if !strings.Contains(body, want) {
			t.Fatalf("body missing %q:\n%s", want, body)
		}
	}
}

func TestHostPortIsStripped(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	rec := get(handler(t, dir), "blog.localhost:8080", "text/html")
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", rec.Code)
	}
}

func TestUnknownHostServes404WithProjectList(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	rec := get(handler(t, dir), "mystery.localhost", "text/html")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "blog.localhost") {
		t.Fatalf("404 page should list known projects:\n%s", rec.Body.String())
	}
}

func TestMissingRegistryDirDegradesToGeneric404(t *testing.T) {
	rec := get(handler(t, filepath.Join(t.TempDir(), "absent")), "x.localhost", "text/html")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
}

func TestMalformedEntryIsSkipped(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "bad.json"), []byte("{nope"), 0o644); err != nil {
		t.Fatal(err)
	}
	writeEntry(t, dir, "blog", "blog.localhost")
	rec := get(handler(t, dir), "blog.localhost", "text/html")
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", rec.Code)
	}
}

func TestNonHTMLAcceptGetsPlainText(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	rec := get(handler(t, dir), "blog.localhost", "application/json")
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "text/plain") {
		t.Fatalf("Content-Type = %q, want text/plain", got)
	}
	if strings.Contains(rec.Body.String(), "<html") {
		t.Fatal("plain-text response must not contain HTML")
	}
}

func TestMissingRegistryPathConfigIsAnError(t *testing.T) {
	next := http.HandlerFunc(func(http.ResponseWriter, *http.Request) {})
	if _, err := New(context.Background(), next, CreateConfig(), "x"); err == nil {
		t.Fatal("expected configuration error")
	}
}
