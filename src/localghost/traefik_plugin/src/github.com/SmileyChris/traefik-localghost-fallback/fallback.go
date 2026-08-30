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
	host := requestHost(req)
	entries := f.entries()
	wantsHTML := strings.Contains(req.Header.Get("Accept"), "text/html")
	for _, e := range entries {
		if e.Hostname == host {
			f.respondGhost(rw, e, wantsHTML)
			return
		}
	}
	f.respondUnknown(rw, host, entries, wantsHTML)
}

func requestHost(req *http.Request) string {
	host := req.Host
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	return strings.ToLower(host)
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

const pageTemplate = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{.Title}}</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; min-height: 100vh;
         display: grid; place-items: center; background: #16161d; color: #e8e8ef; }
  main { max-width: 38rem; padding: 2rem; }
  h1 { font-weight: 600; }
  h1 span { opacity: .55; font-weight: 400; }
  code { background: #26262f; border-radius: .3rem; padding: .15rem .45rem; }
  pre  { background: #26262f; border-radius: .5rem; padding: 1rem; overflow-x: auto; }
  ul { padding-left: 1.2rem; } li { margin: .4rem 0; }
  .muted { opacity: .65; }
  a { color: #9ecbff; }
</style>
</head>
<body>
<main>
{{if .Ghost}}
  <h1>👻 {{.Ghost.Name}} <span>is offline</span></h1>
  <p>A {{.Ghost.Type}} project last started {{.Ghost.Relative}} from
     <code>{{.Ghost.Directory}}</code>.</p>
  <p>Bring it back:</p>
  <pre>cd {{.Ghost.Directory}}
uvx localghost run</pre>
  <p class="muted">Forget this page with <code>localghost forget {{.Ghost.Name}}</code>.</p>
{{else}}
  <h1>👻 Nothing haunts <span>{{.Host}}</span></h1>
  <p>No running application and no remembered project answers to this name.</p>
  {{if .Known}}
  <p>Localghost does remember:</p>
  <ul>
  {{range .Known}}
    <li><a href="//{{.Hostname}}">{{.Hostname}}</a>
        <span class="muted">— {{.Type}} in {{.Directory}}, last started {{.Relative}}</span></li>
  {{end}}
  </ul>
  {{end}}
{{end}}
</main>
</body>
</html>
`

type pageEntry struct {
	Hostname  string
	Name      string
	Directory string
	Type      string
	Relative  string
}

type pageData struct {
	Title string
	Host  string
	Ghost *pageEntry
	Known []pageEntry
}

var page = template.Must(template.New("ghost").Parse(pageTemplate))

func (f *Fallback) respondGhost(rw http.ResponseWriter, e entry, wantsHTML bool) {
	relative := relativeTime(e.LastStarted)
	if !wantsHTML {
		rw.Header().Set("Content-Type", "text/plain; charset=utf-8")
		rw.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprintf(rw, "%s is offline. Last started %s from %s.\nRun: cd %s && uvx localghost run\n",
			e.Name, relative, e.Directory, e.Directory)
		return
	}
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.WriteHeader(http.StatusServiceUnavailable)
	_ = page.Execute(rw, pageData{
		Title: e.Name + " is offline",
		Ghost: &pageEntry{
			Hostname:  e.Hostname,
			Name:      e.Name,
			Directory: e.Directory,
			Type:      e.Type,
			Relative:  relative,
		},
	})
}

func (f *Fallback) respondUnknown(rw http.ResponseWriter, host string, entries []entry, wantsHTML bool) {
	if !wantsHTML {
		rw.Header().Set("Content-Type", "text/plain; charset=utf-8")
		rw.WriteHeader(http.StatusNotFound)
		fmt.Fprintf(rw, "nothing is running at %s\n", host)
		return
	}
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
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.WriteHeader(http.StatusNotFound)
	_ = page.Execute(rw, pageData{Title: "Nothing running here", Host: host, Known: known})
}
