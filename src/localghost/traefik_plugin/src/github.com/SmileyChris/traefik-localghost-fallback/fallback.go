// Package traefik_localghost_fallback implements a Traefik middleware plugin
// serving "ghost pages": when no real router claims a hostname, it answers
// with an offline page for projects the localghost CLI has remembered, or a
// generic not-found page listing what it does remember. It never calls the
// next handler; its router points at noop@internal.
package traefik_localghost_fallback

import (
	"context"
	"encoding/json"
	"fmt"
	"html/template"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

type Config struct {
	RegistryPath string `json:"registryPath,omitempty"`
}

func CreateConfig() *Config { return &Config{} }

type entry struct {
	Hostname    string `json:"hostname"`
	Name        string `json:"name"`
	Directory   string `json:"directory"`
	Type        string `json:"type"`
	LastStarted string `json:"last_started"`
}

type Fallback struct {
	name         string
	registryPath string
}

func New(_ context.Context, _ http.Handler, config *Config, name string) (http.Handler, error) {
	if config == nil || config.RegistryPath == "" {
		return nil, fmt.Errorf("localghost fallback: registryPath is required")
	}
	return &Fallback{name: name, registryPath: config.RegistryPath}, nil
}

func (f *Fallback) ServeHTTP(rw http.ResponseWriter, req *http.Request) {
	host, port := requestHostPort(req)
	entries := f.entries()
	// Deliberate deviation from the Accept spec's letter: curl and scripts
	// send `Accept: */*` by default and get plain text; only a request that
	// literally asks for text/html (browsers, or `curl -H 'Accept: text/html'`)
	// gets the styled ghost page.
	wantsHTML := strings.Contains(req.Header.Get("Accept"), "text/html")
	// Nothing is ever hosted at the bare loopback names themselves, so they
	// serve the hub's welcome page instead of a not-found ghost page.
	if host == "localhost" || host == "127.0.0.1" {
		f.respondWelcome(rw, port, entries, wantsHTML)
		return
	}
	for _, e := range entries {
		if e.Hostname == host {
			f.respondGhost(rw, e, wantsHTML)
			return
		}
	}
	f.respondUnknown(rw, host, port, entries, wantsHTML)
}

// requestHostPort splits the request's Host header into the hostname used
// for registry lookups and the port the hub is actually published on (if
// the client's request included one), so links rendered back to the client
// can carry that port forward instead of dead-ending on :80.
func requestHostPort(req *http.Request) (host, port string) {
	host = req.Host
	if h, p, err := net.SplitHostPort(host); err == nil {
		host, port = h, p
	}
	return strings.ToLower(host), port
}

// shellQuote renders s as a single-quoted POSIX shell argument, so a
// resurrect command copy-pastes safely even when the directory contains
// spaces. Directories containing single quotes are out of scope.
func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

// entries reads the registry fresh per request; hub traffic is a developer
// clicking a link, so simplicity beats caching.
func (f *Fallback) entries() []entry {
	names, err := filepath.Glob(filepath.Join(f.registryPath, "*.json"))
	if err != nil {
		return nil
	}
	sort.Strings(names)
	var result []entry
	for _, path := range names {
		payload, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var e entry
		if err := json.Unmarshal(payload, &e); err != nil || e.Hostname == "" {
			continue
		}
		result = append(result, e)
	}
	return result
}

func relativeTime(stamp string) string {
	when, err := time.Parse(time.RFC3339, stamp)
	if err != nil {
		return "a while ago"
	}
	elapsed := time.Since(when)
	switch {
	case elapsed < time.Minute:
		return "moments ago"
	case elapsed < time.Hour:
		return fmt.Sprintf("%d minutes ago", int(elapsed.Minutes()))
	case elapsed < 48*time.Hour:
		return fmt.Sprintf("%d hours ago", int(elapsed.Hours()))
	default:
		return fmt.Sprintf("%d days ago", int(elapsed.Hours()/24))
	}
}

// The palette and heading font mirror the documentation site
// (docs/stylesheets/extra.css): Nunito headings, localghost greens, light
// surfaces. The logo is the docs home-page image, embedded as a data URI
// because Yaegi-interpreted plugins cannot serve asset files.
const pageTemplate = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{.Title}}</title>
<link rel="icon" type="image/png" href="{{.Logo}}">
<style>
  @import url("https://fonts.googleapis.com/css2?family=Nunito:wght@600;700;800&display=swap");
  body { font-family: system-ui, sans-serif; margin: 0; min-height: 100vh;
         display: grid; place-items: center; background: #ffffff; color: #1f2933; }
  main { max-width: 38rem; padding: 2rem; }
  h1 { font-family: "Nunito", system-ui, sans-serif; font-weight: 700; }
  h1 .quiet { color: #6b7a85; font-weight: 600; }
  .accent { color: #71d5a7; }
  .logo { display: block; height: auto; margin: 0 auto 1.5rem; max-width: min(360px, 80vw); }
  .logo-small { max-width: min(240px, 60vw); }
  code { background: #f1faf5; border-radius: .3rem; padding: .15rem .45rem; }
  pre  { background: #f1faf5; border-radius: .5rem; padding: 1rem; overflow-x: auto; }
  ul { padding-left: 1.2rem; } li { margin: .4rem 0; }
  .muted { color: #6b7a85; }
  a { color: #267857; }
  .wordmark { font-family: "Nunito", system-ui, sans-serif; font-weight: 700;
              margin-top: 2.5rem; }
  .wordmark a { color: #1f2933; text-decoration: none; }
</style>
</head>
<body>
<main>
{{if .Welcome}}
  <img class="logo" src="{{.Logo}}" alt="Localghost">
  <h1>local<span class="accent">ghost</span> <span class="quiet">is running</span></h1>
  <p>Nothing lives at <code>{{.Host}}</code> itself — every project gets its
     own <code>.localhost</code> hostname.</p>
  {{if .Known}}
  <p>Remembered projects:</p>
  <ul>
  {{range .Known}}
    <li><a href="//{{.Hostname}}{{if $.Port}}:{{$.Port}}{{end}}">{{.Hostname}}</a>
        <span class="muted">— {{.Type}} in {{.Directory}}, last started {{.Relative}}</span></li>
  {{end}}
  </ul>
  {{end}}
  <p class="muted"><a href="//traefik.localhost{{if .Port}}:{{.Port}}{{end}}">Traefik dashboard</a>
     · <a href="https://smileychris.github.io/localghost/">Documentation</a></p>
{{else if .Ghost}}
  <img class="logo logo-small" src="{{.Logo}}" alt="Localghost">
  <h1>{{.Ghost.Name}} <span class="quiet">is offline</span></h1>
  <p>A {{.Ghost.Type}} project last started {{.Ghost.Relative}} from
     <code>{{.Ghost.Directory}}</code>.</p>
  <p>Bring it back:</p>
  <pre>cd {{.Ghost.QuotedDirectory}}
uvx localghost run</pre>
  <p class="muted">Forget this page with <code>localghost forget {{.Ghost.Name}}</code>.</p>
{{else}}
  <img class="logo logo-small" src="{{.Logo}}" alt="Localghost">
  <h1>Nothing haunts <span class="quiet">{{.Host}}</span></h1>
  <p>No running application and no remembered project answers to this name.</p>
  {{if .Known}}
  <p>Localghost does remember:</p>
  <ul>
  {{range .Known}}
    <li><a href="//{{.Hostname}}{{if $.Port}}:{{$.Port}}{{end}}">{{.Hostname}}</a>
        <span class="muted">— {{.Type}} in {{.Directory}}, last started {{.Relative}}</span></li>
  {{end}}
  </ul>
  {{end}}
{{end}}
{{if not .Welcome}}
  <p class="wordmark"><a href="//localhost{{if .Port}}:{{.Port}}{{end}}">local<span class="accent">ghost</span></a></p>
{{end}}
</main>
</body>
</html>
`

type pageEntry struct {
	Hostname        string
	Name            string
	Directory       string
	QuotedDirectory string
	Type            string
	Relative        string
}

type pageData struct {
	Title   string
	Host    string
	Port    string
	Welcome bool
	Logo    template.URL
	Ghost   *pageEntry
	Known   []pageEntry
}

var page = template.Must(template.New("ghost").Parse(pageTemplate))

// logoURL is typed template.URL so html/template does not neuter the
// data: scheme in src/href attributes.
var logoURL = template.URL("data:image/png;base64," + logoBase64)

func pageEntries(entries []entry) []pageEntry {
	known := make([]pageEntry, 0, len(entries))
	for _, e := range entries {
		known = append(known, pageEntry{
			Hostname:  e.Hostname,
			Name:      e.Name,
			Directory: e.Directory,
			Type:      e.Type,
			Relative:  relativeTime(e.LastStarted),
		})
	}
	return known
}

func (f *Fallback) respondWelcome(rw http.ResponseWriter, port string, entries []entry, wantsHTML bool) {
	if !wantsHTML {
		rw.Header().Set("Content-Type", "text/plain; charset=utf-8")
		rw.WriteHeader(http.StatusOK)
		fmt.Fprintf(rw, "localghost hub is running (%d remembered project(s))\n", len(entries))
		return
	}
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.WriteHeader(http.StatusOK)
	_ = page.Execute(rw, pageData{
		Title:   "localghost",
		Host:    "localhost",
		Port:    port,
		Welcome: true,
		Logo:    logoURL,
		Known:   pageEntries(entries),
	})
}

func (f *Fallback) respondGhost(rw http.ResponseWriter, e entry, wantsHTML bool) {
	relative := relativeTime(e.LastStarted)
	quotedDirectory := shellQuote(e.Directory)
	if !wantsHTML {
		rw.Header().Set("Content-Type", "text/plain; charset=utf-8")
		rw.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprintf(rw, "%s is offline. Last started %s from %s.\nRun: cd %s && uvx localghost run\n",
			e.Name, relative, e.Directory, quotedDirectory)
		return
	}
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.WriteHeader(http.StatusServiceUnavailable)
	_ = page.Execute(rw, pageData{
		Title: e.Name + " is offline",
		Logo:  logoURL,
		Ghost: &pageEntry{
			Hostname:        e.Hostname,
			Name:            e.Name,
			Directory:       e.Directory,
			QuotedDirectory: quotedDirectory,
			Type:            e.Type,
			Relative:        relative,
		},
	})
}

func (f *Fallback) respondUnknown(rw http.ResponseWriter, host, port string, entries []entry, wantsHTML bool) {
	if !wantsHTML {
		rw.Header().Set("Content-Type", "text/plain; charset=utf-8")
		rw.WriteHeader(http.StatusNotFound)
		fmt.Fprintf(rw, "nothing is running at %s\n", host)
		return
	}
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.WriteHeader(http.StatusNotFound)
	_ = page.Execute(rw, pageData{
		Title: "Nothing running here",
		Host:  host,
		Port:  port,
		Logo:  logoURL,
		Known: pageEntries(entries),
	})
}
