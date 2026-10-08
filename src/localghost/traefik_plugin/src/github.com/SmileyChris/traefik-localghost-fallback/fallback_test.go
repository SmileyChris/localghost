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
	writeEntryDir(t, dir, name, hostname, "/home/dev/"+name)
}

func writeEntryDir(t *testing.T, dir, name, hostname, directory string) {
	t.Helper()
	payload := `{
  "hostname": "` + hostname + `",
  "name": "` + name + `",
  "directory": "` + directory + `",
  "type": "django",
  "last_started": "` + time.Now().Add(-26*time.Hour).Format(time.RFC3339) + `"
}`
	if err := os.WriteFile(filepath.Join(dir, name+".json"), []byte(payload), 0o644); err != nil {
		t.Fatal(err)
	}
}

func handler(t *testing.T, registry string) http.Handler {
	t.Helper()
	return handlerWithRouters(t, registry, "")
}

// handlerWithRouters points the online check at routersURL; empty turns it
// off, so tests never reach a real hub on this machine.
func handlerWithRouters(t *testing.T, registry, routersURL string) http.Handler {
	t.Helper()
	next := http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("fallback must not call the next handler")
	})
	config := CreateConfig()
	config.RegistryPath = registry
	config.RoutersURL = routersURL
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
	for _, want := range []string{"blog", "/home/dev/blog", "uvx localghost restart blog", "uvx localghost sessions forget blog", "data:image/png;base64,"} {
		if !strings.Contains(body, want) {
			t.Fatalf("body missing %q:\n%s", want, body)
		}
	}
	if strings.Contains(body, "\U0001F47B") {
		t.Fatal("ghost page must use the logo image, not the ghost emoji")
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

func TestUnknownHostProjectLinksCarryHubPort(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	rec := get(handler(t, dir), "mystery.localhost:18080", "text/html")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `href="//blog.localhost:18080"`) {
		t.Fatalf("404 page project link should carry the hub's port:\n%s", rec.Body.String())
	}
}

func TestUnknownHostProjectLinksOmitDefaultPort(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	rec := get(handler(t, dir), "mystery.localhost", "text/html")
	if !strings.Contains(rec.Body.String(), `href="//blog.localhost"`) {
		t.Fatalf("404 page project link should not append a port when the request had none:\n%s", rec.Body.String())
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

func TestPlainTextGhostSuggestsRestart(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	rec := get(handler(t, dir), "blog.localhost", "application/json")
	if !strings.Contains(rec.Body.String(), "uvx localghost restart blog") {
		t.Fatalf("plain-text ghost should suggest restart: %q", rec.Body.String())
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

func TestLocalhostServesWelcomePage(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	rec := get(handler(t, dir), "localhost", "text/html")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{"localghost", "blog.localhost", "data:image/png;base64,", "traefik.localhost", "localghost restart NAME"} {
		if !strings.Contains(body, want) {
			t.Fatalf("welcome body missing %q", want)
		}
	}
}

func TestLoopbackIPWelcomeCarriesPortInLinks(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	rec := get(handler(t, dir), "127.0.0.1:18080", "text/html")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "//blog.localhost:18080") {
		t.Fatalf("welcome links should carry the hub port:\n%s", rec.Body.String())
	}
}

func TestLocalhostPlainTextWelcome(t *testing.T) {
	dir := t.TempDir()
	rec := get(handler(t, dir), "localhost", "application/json")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "text/plain") {
		t.Fatalf("Content-Type = %q, want text/plain", got)
	}
	if !strings.Contains(rec.Body.String(), "hub is running") {
		t.Fatalf("plain welcome missing status line: %q", rec.Body.String())
	}
}

func TestWelcomeWithoutRegistryStillRenders(t *testing.T) {
	rec := get(handler(t, filepath.Join(t.TempDir(), "absent")), "localhost", "text/html")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
}

func TestWelcomeShowsOnlineStateAndMode(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	writeEntry(t, dir, "shop", "shop.localhost")
	detached := `{"hostname": "api.localhost", "name": "api", "directory": "/home/dev/api",
  "type": "django", "last_started": "2026-01-01T00:00:00Z", "detached": true}`
	if err := os.WriteFile(filepath.Join(dir, "api.json"), []byte(detached), 0o644); err != nil {
		t.Fatal(err)
	}
	var askedHost string
	api := httptest.NewServer(http.HandlerFunc(func(rw http.ResponseWriter, req *http.Request) {
		askedHost = req.Host
		_, _ = rw.Write([]byte(`[
  {"name": "blog@docker", "rule": "Host(` + "`blog.localhost`" + `)", "status": "enabled"},
  {"name": "api@docker", "rule": "Host(` + "`api.localhost`" + `) || Host(` + "`api.tail`" + `)", "status": "enabled"},
  {"name": "shop@docker", "rule": "Host(` + "`shop.localhost`" + `)", "status": "disabled"}
]`))
	}))
	defer api.Close()

	body := get(handlerWithRouters(t, dir, api.URL), "localhost", "text/html").Body.String()

	if askedHost != "traefik.localhost" {
		t.Fatalf("router API asked with Host %q, want traefik.localhost", askedHost)
	}
	for _, want := range []string{
		`<span class="project-when running">Running in the foreground</span>`,
		`<span class="project-when running">Running detached</span>`,
		`<span class="project-when">Last started 26 hours ago</span>`,
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("welcome body missing %q:\n%s", want, body)
		}
	}
	if strings.Count(body, `class="project-when running"`) != 2 {
		t.Fatalf("only blog and api are online:\n%s", body)
	}
}

func TestWelcomeShowsStartTimesWhenTheRouterAPIFails(t *testing.T) {
	dir := t.TempDir()
	writeEntry(t, dir, "blog", "blog.localhost")
	api := httptest.NewServer(http.HandlerFunc(func(rw http.ResponseWriter, _ *http.Request) {
		rw.WriteHeader(http.StatusNotFound)
	}))
	defer api.Close()

	body := get(handlerWithRouters(t, dir, api.URL), "localhost", "text/html").Body.String()

	if strings.Contains(body, "Running ") {
		t.Fatalf("unknown state must not claim running:\n%s", body)
	}
	if !strings.Contains(body, `<span class="project-when">Last started 26 hours ago</span>`) {
		t.Fatalf("welcome body missing start time:\n%s", body)
	}
}
