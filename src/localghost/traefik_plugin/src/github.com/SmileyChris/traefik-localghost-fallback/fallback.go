// Package traefik_localghost_fallback implements a Traefik middleware plugin
// serving "ghost pages": when no real router claims a hostname, it answers
// with an offline page for projects the localghost CLI has remembered, or a
// generic not-found page listing what it does remember. It never calls the
// next handler; its router points at noop@internal.
package traefik_localghost_fallback

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"html/template"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

type Config struct {
	RegistryPath string `json:"registryPath,omitempty"`
	// RoutersURL is Traefik's own router API, reached through the dashboard
	// route on the web entrypoint. Empty turns the online check off.
	RoutersURL string `json:"routersURL,omitempty"`
	// TailnetSuffix and TailnetRoot name the private tailnet suffix and its
	// public root, so the tailnet index can show the pinned trust command.
	// Empty, as on a hub without a tailnet authority, leaves it out.
	TailnetSuffix string `json:"tailnetSuffix,omitempty"`
	TailnetRoot   string `json:"tailnetRoot,omitempty"`
}

func CreateConfig() *Config {
	return &Config{RoutersURL: "http://127.0.0.1/api/http/routers?per_page=1000"}
}

type entry struct {
	Hostname    string `json:"hostname"`
	Name        string `json:"name"`
	Directory   string `json:"directory"`
	Type        string `json:"type"`
	LastStarted string `json:"last_started"`
	Detached    bool   `json:"detached"`
}

type Fallback struct {
	name          string
	registryPath  string
	routersURL    string
	tailnetSuffix string
	tailnetRoot   string
	client        *http.Client
}

func New(_ context.Context, _ http.Handler, config *Config, name string) (http.Handler, error) {
	if config == nil || config.RegistryPath == "" {
		return nil, fmt.Errorf("localghost fallback: registryPath is required")
	}
	return &Fallback{
		name:          name,
		registryPath:  config.RegistryPath,
		routersURL:    config.RoutersURL,
		tailnetSuffix: config.TailnetSuffix,
		tailnetRoot:   config.TailnetRoot,
		client:        &http.Client{Timeout: time.Second},
	}, nil
}

func (f *Fallback) ServeHTTP(rw http.ResponseWriter, req *http.Request) {
	host, port := requestHostPort(req)
	v := viewFor(host, port)
	entries := f.entries()
	// Deliberate deviation from the Accept spec's letter: curl and scripts
	// send `Accept: */*` by default and get plain text; only a request that
	// literally asks for text/html (browsers, or `curl -H 'Accept: text/html'`)
	// gets the styled ghost page.
	wantsHTML := strings.Contains(req.Header.Get("Accept"), "text/html")
	// Nothing is ever hosted at the bare loopback names or at localghost.*
	// (a reserved project name), so they serve the hub's welcome page instead
	// of a not-found ghost page.
	if host == "localhost" || host == "127.0.0.1" || host == "localghost."+v.Suffix {
		f.respondWelcome(rw, v, entries, wantsHTML)
		return
	}
	local := v.local(host)
	for _, e := range entries {
		if e.Hostname == local && !v.Remote() {
			f.respondGhost(rw, e, wantsHTML)
			return
		}
	}
	f.respondUnknown(rw, v, host, entries, wantsHTML)
}

// view is where a page is read from: this machine's .localhost names, or a
// tailnet suffix, under which every route answers by the same first labels.
// A tailnet reader is on another device, so the page leaves out what only
// makes sense here: project folders, and projects that are not running,
// which are this machine's to restart and nobody else's business.
type view struct {
	Suffix string
	Port   string
}

func viewFor(host, port string) view {
	suffix := "localhost"
	if i := strings.LastIndex(host, "."); i >= 0 && net.ParseIP(host) == nil {
		suffix = host[i+1:]
	}
	return view{Suffix: suffix, Port: port}
}

func (v view) Remote() bool { return v.Suffix != "localhost" }

// Home is the hub's own page under this view's suffix.
func (v view) Home() string {
	if v.Remote() {
		return "localghost." + v.Suffix
	}
	return "localhost"
}

// name turns a registry hostname, always under .localhost, into this view's.
func (v view) name(hostname string) string {
	return strings.TrimSuffix(hostname, ".localhost") + "." + v.Suffix
}

// local turns a requested hostname back into its registry hostname.
func (v view) local(host string) string {
	return strings.TrimSuffix(host, "."+v.Suffix) + ".localhost"
}

type trust struct {
	Command     string
	Fingerprint string
}

// trust pins this hub's tailnet root for a reader under its suffix, read
// fresh like the registry so a regenerated root shows at once.
func (f *Fallback) trust(v view) *trust {
	if f.tailnetRoot == "" || v.Suffix != f.tailnetSuffix {
		return nil
	}
	value, err := os.ReadFile(f.tailnetRoot)
	if err != nil {
		return nil
	}
	block, _ := pem.Decode(value)
	if block == nil || block.Type != "CERTIFICATE" {
		return nil
	}
	digest := sha256.Sum256(block.Bytes)
	fingerprint := "SHA256:" + strings.ToUpper(hex.EncodeToString(digest[:]))
	return &trust{
		Command:     "uvx localghost tailscale trust " + v.Suffix,
		Fingerprint: fingerprint,
	}
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

var hostRule = regexp.MustCompile("Host\\(\\s*`([^`]+)`\\s*\\)")

// onlineHosts asks Traefik which hostnames a router serves right now: the
// same "is anything answering at this name" a visitor would get. When that
// can't be known it returns nil, and every project reads as not running.
func (f *Fallback) onlineHosts() map[string]bool {
	if f.routersURL == "" {
		return nil
	}
	req, err := http.NewRequest(http.MethodGet, f.routersURL, nil)
	if err != nil {
		return nil
	}
	req.Host = "traefik.localhost"
	resp, err := f.client.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	var routers []struct {
		Rule   string `json:"rule"`
		Status string `json:"status"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&routers); err != nil {
		return nil
	}
	hosts := map[string]bool{}
	for _, router := range routers {
		if router.Status != "enabled" {
			continue
		}
		for _, match := range hostRule.FindAllStringSubmatch(router.Rule, -1) {
			hosts[strings.ToLower(match[1])] = true
		}
	}
	return hosts
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
<link rel="icon" type="image/png" href="{{.Icon}}">
<style>
  @import url("https://fonts.googleapis.com/css2?family=Nunito:wght@600;700;800&display=swap");
  :root {
    --ink: #1f2933; --quiet: #5f6e79;
    --accent: #71d5a7; --link: #267857; --link-deep: #21654d;
    --tint: #f1faf5; --tint-strong: #e3f5eb; --line: #e2e9e5;
    --heading: "Nunito", system-ui, sans-serif;
    --mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; margin: 0; min-height: 100vh;
         display: grid; place-items: center; background: #ffffff; color: var(--ink);
         line-height: 1.5; -webkit-font-smoothing: antialiased; }
  ::selection { background: var(--accent); color: var(--ink); }
  main { width: min(40rem, 100%); padding: 2rem 1.25rem; }
  h1 { font-family: var(--heading); font-weight: 700; font-size: 2rem;
       line-height: 1.15; margin: 0 0 1rem; text-wrap: balance; }
  h1 .quiet { color: var(--quiet); font-weight: 600; }
  h2 { font-family: var(--heading); font-weight: 700; font-size: .8rem;
       letter-spacing: .04em; text-transform: uppercase; color: var(--quiet);
       margin: 2.25rem 0 .5rem; }
  p { margin: 0 0 1rem; }
  .accent { color: var(--accent); }
  .logo { display: block; height: auto; margin: 0 auto 1.5rem; max-width: min(360px, 80vw); }
  .ghost-inline { height: 1.4em; vertical-align: -0.25em; margin-right: .15em; }
  code { font-family: var(--mono); font-size: .9em; background: var(--tint);
         border-radius: .3rem; padding: .15rem .45rem; }
  a { color: var(--link); text-decoration-thickness: 1px; text-underline-offset: .18em; }
  a:hover { color: var(--link-deep); }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: .25rem; }
  .muted { color: var(--quiet); }

  .projects { list-style: none; margin: 0; padding: 0;
              border-top: 1px solid var(--line); }
  .project { display: grid; grid-template-columns: 1fr auto; column-gap: 1rem;
             row-gap: .15rem; align-items: baseline; padding: .8rem .25rem;
             border-bottom: 1px solid var(--line); transition: background-color .15s; }
  .project:last-child { border-bottom: 0; }
  .project:hover { background: var(--tint); }
  .project.idle:hover { background: none; }
  .idle .project-name { color: var(--accent); }
  .project-name { font-family: var(--heading); font-weight: 700; font-size: 1.05rem;
                  text-decoration: none; }
  .project-name:hover { text-decoration: underline; }
  .project-type { justify-self: end; font-family: var(--mono); font-size: .75rem;
                  color: var(--link-deep); background: var(--tint-strong);
                  border-radius: .3rem; padding: .1rem .45rem; }
  .project-dir { grid-column: 1; font-family: var(--mono); font-size: .8rem;
                 color: var(--quiet); overflow-wrap: anywhere; }
  .project-when { grid-column: 2; justify-self: end; font-size: .8rem;
                  color: var(--quiet); white-space: nowrap; }
  .running { color: var(--link); font-weight: 600; }
  .running::before { content: "●"; margin-right: .3em; }
  .empty { color: var(--quiet); border-top: 1px solid var(--line); padding-top: .8rem; }

  .cmd { font-family: var(--mono); max-width: 100%; overflow-wrap: anywhere; text-align: left; background: var(--tint); border: 1px solid var(--accent);
         border-radius: .4rem; padding: .4rem .8rem; cursor: pointer; color: var(--link-deep);
         font-size: .95rem; transition: background-color .15s; }
  .cmd:hover { background: var(--tint-strong); }
  .cmd.copied::after { content: "copied"; margin-left: .6em; padding: .1em .45em;
                       border-radius: .25rem; background: var(--link); color: #fff;
                       font: 700 .65rem/1.4 system-ui, sans-serif; letter-spacing: .05em;
                       text-transform: uppercase; vertical-align: .15em; }
  .cmd-quiet { border-color: #d3dce2; color: var(--quiet); background: #f7f9fa; }
  .cmd-quiet:hover { background: #eef2f4; }

  footer { display: flex; flex-wrap: wrap; align-items: baseline; gap: .5rem 1.5rem;
           margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid var(--line);
           font-size: .875rem; color: var(--quiet); }
  footer .wordmark { font-family: var(--heading); font-weight: 700; font-size: 1rem;
                     color: var(--ink); text-decoration: none; margin-right: auto; }
  footer .wordmark:hover .accent { color: var(--link); }
  footer nav { display: flex; gap: 1.25rem; }
  footer nav a { color: var(--quiet); text-decoration: none; }
  footer nav a:hover { color: var(--link-deep); text-decoration: underline; }

  @media (max-width: 30rem) {
    h1 { font-size: 1.6rem; }
    .project-when { grid-column: 1; justify-self: start; }
  }
</style>
</head>
<body>
<main>
{{if .Welcome}}
  <img class="logo" src="{{.Logo}}" alt="Localghost">
  <h1>local<span class="accent">ghost</span> <span class="quiet">is running</span></h1>
  {{if .Remote}}
  <p>Every project on this hub answers at its own <code>.{{.Suffix}}</code> hostname.</p>
  {{else}}
  <p>Nothing lives at <code>{{.Host}}</code> itself — every project gets its
     own <code>.localhost</code> hostname.</p>
  {{end}}
  {{with .Trust}}
  <h2>Trust this hub's HTTPS</h2>
  <p><button class="cmd" data-cmd="{{.Command}}">{{.Command}}</button></p>
  <p class="muted">It finds this hub through Tailscale, which vouches for it.
     Without the tailscale CLI, pin the fingerprint you get from this hub's
     owner instead, or install the
     <a href="http://{{$.Home}}/.well-known/localghost/root.pem">root certificate</a>
     by hand. It must be <code>{{.Fingerprint}}</code>.</p>
  {{end}}
  {{if .Known}}
  {{if .Trust}}<h2>Projects</h2>{{end}}
  {{template "projects" .}}
  {{else}}
  {{if .Remote}}
  <p class="empty">Nothing is running on this hub right now.</p>
  {{else}}
  <p class="empty">Nothing yet. A project appears here once <code>localghost run</code>
     starts it or <code>localghost save</code> remembers it.</p>
  {{end}}
  {{end}}
{{else if .Ghost}}
  <h1><img class="ghost-inline" src="{{.Icon}}" alt="">{{.Ghost.Name}} <span class="quiet">is offline</span></h1>
  <p>A {{.Ghost.Type}} project last started {{.Ghost.Relative}} from
     <code>{{.Ghost.Directory}}</code>.</p>
  <p>Bring it back:</p>
  <p><button class="cmd" data-cmd="uvx localghost restart {{.Ghost.Name}}">uvx localghost restart {{.Ghost.Name}}</button></p>
  <p class="muted">Or lay it to rest:
     <button class="cmd cmd-quiet" data-cmd="uvx localghost sessions forget {{.Ghost.Name}}">uvx localghost sessions forget {{.Ghost.Name}}</button></p>
{{else}}
  <h1><img class="ghost-inline" src="{{.Icon}}" alt="">Nothing haunts <span class="quiet">{{.Host}}</span></h1>
  <p>No running application and no remembered project answers to this name.</p>
  {{if .Known}}
  <h2>{{if .Remote}}Running on this hub{{else}}Localghost does remember{{end}}</h2>
  {{template "projects" .}}
  {{end}}
{{end}}
<footer>
  {{if .Welcome}}<span class="wordmark">local<span class="accent">ghost</span></span>{{else}}<a class="wordmark" href="//{{.Home}}{{if .Port}}:{{.Port}}{{end}}">local<span class="accent">ghost</span></a>{{end}}
  <nav>
    <a href="//traefik.{{.Suffix}}{{if .Port}}:{{.Port}}{{end}}">Traefik dashboard</a>
    <a href="https://smileychris.github.io/localghost/">Documentation</a>
  </nav>
</footer>
</main>
<script>
for (const chip of document.querySelectorAll(".cmd")) {
  chip.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(chip.dataset.cmd);
    } catch (err) {
      return;
    }
    chip.classList.add("copied");
    setTimeout(() => chip.classList.remove("copied"), 1200);
  });
}
</script>
</body>
</html>
{{define "projects"}}
  <ul class="projects">
  {{range .Known}}
    <li class="project{{if not .Running}} idle{{end}}">
      <a class="project-name" href="//{{.Hostname}}{{if $.Port}}:{{$.Port}}{{end}}">{{.Hostname}}</a>
      <span class="project-type">{{.Type}}</span>
      {{if .Directory}}<span class="project-dir">{{.Directory}}</span>{{end}}
      {{if .Running}}<span class="project-when running">Running {{.Mode}}</span>
      {{else}}<span class="project-when">Last started {{.Relative}}</span>{{end}}
    </li>
  {{end}}
  </ul>
{{end}}`

type pageEntry struct {
	Hostname  string
	Name      string
	Directory string
	Type      string
	Relative  string
	Running   bool
	Mode      string // "in the foreground" or "detached"
}

// The view's values are copied in rather than embedded: Yaegi, which
// interprets this plugin, is unreliable with promoted methods under
// reflection.
type pageData struct {
	Suffix  string
	Port    string
	Home    string
	Remote  bool
	Title   string
	Host    string
	Welcome bool
	Trust   *trust
	Logo    template.URL
	Icon    template.URL
	Ghost   *pageEntry
	Known   []pageEntry
}

var page = template.Must(template.New("ghost").Parse(pageTemplate))

// The URLs are typed template.URL so html/template does not neuter the
// data: scheme in src/href attributes.
var (
	logoURL  = template.URL("data:image/png;base64," + logoBase64)
	ghostURL = template.URL("data:image/png;base64," + ghostBase64)
)

// pageEntries lists running projects first, by name (entries arrive sorted by
// name), then the rest by when they last started, most recent first. A
// remote view lists only the running ones.
func pageEntries(entries []entry, online map[string]bool, v view) []pageEntry {
	entries = append([]entry(nil), entries...)
	sort.SliceStable(entries, func(i, j int) bool {
		a, b := online[entries[i].Hostname], online[entries[j].Hostname]
		if a || b {
			return a && !b
		}
		return started(entries[i]).After(started(entries[j]))
	})
	known := make([]pageEntry, 0, len(entries))
	for _, e := range entries {
		directory := e.Directory
		if v.Remote() {
			if !online[e.Hostname] {
				continue
			}
			directory = ""
		}
		known = append(known, pageEntry{
			Hostname:  v.name(e.Hostname),
			Name:      e.Name,
			Directory: directory,
			Type:      e.Type,
			Relative:  relativeTime(e.LastStarted),
			Running:   online[e.Hostname],
			Mode:      mode(e.Detached),
		})
	}
	return known
}

func started(e entry) time.Time {
	when, _ := time.Parse(time.RFC3339, e.LastStarted)
	return when
}

func mode(detached bool) string {
	if detached {
		return "detached"
	}
	return "in the foreground"
}

func (f *Fallback) respondWelcome(rw http.ResponseWriter, v view, entries []entry, wantsHTML bool) {
	if !wantsHTML {
		rw.Header().Set("Content-Type", "text/plain; charset=utf-8")
		rw.WriteHeader(http.StatusOK)
		fmt.Fprintf(rw, "localghost hub is running (%d remembered project(s))\n", len(entries))
		return
	}
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.WriteHeader(http.StatusOK)
	_ = page.Execute(rw, pageData{
		Suffix:  v.Suffix,
		Port:    v.Port,
		Home:    v.Home(),
		Remote:  v.Remote(),
		Title:   "localghost",
		Host:    "localhost",
		Welcome: true,
		Trust:   f.trust(v),
		Logo:    logoURL,
		Icon:    ghostURL,
		Known:   pageEntries(entries, f.onlineHosts(), v),
	})
}

func (f *Fallback) respondGhost(rw http.ResponseWriter, e entry, wantsHTML bool) {
	relative := relativeTime(e.LastStarted)
	if !wantsHTML {
		rw.Header().Set("Content-Type", "text/plain; charset=utf-8")
		rw.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprintf(rw, "%s is offline. Last started %s from %s.\nRun: uvx localghost restart %s\n",
			e.Name, relative, e.Directory, e.Name)
		return
	}
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.WriteHeader(http.StatusServiceUnavailable)
	_ = page.Execute(rw, pageData{
		Suffix: "localhost",
		Home:   "localhost",
		Title:  e.Name + " is offline",
		Icon:   ghostURL,
		Ghost: &pageEntry{
			Hostname:  e.Hostname,
			Name:      e.Name,
			Directory: e.Directory,
			Type:      e.Type,
			Relative:  relative,
		},
	})
}

func (f *Fallback) respondUnknown(rw http.ResponseWriter, v view, host string, entries []entry, wantsHTML bool) {
	if !wantsHTML {
		rw.Header().Set("Content-Type", "text/plain; charset=utf-8")
		rw.WriteHeader(http.StatusNotFound)
		fmt.Fprintf(rw, "nothing is running at %s\n", host)
		return
	}
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.WriteHeader(http.StatusNotFound)
	_ = page.Execute(rw, pageData{
		Suffix: v.Suffix,
		Port:   v.Port,
		Home:   v.Home(),
		Remote: v.Remote(),
		Title:  "Nothing running here",
		Host:   host,
		Icon:   ghostURL,
		Known:  pageEntries(entries, f.onlineHosts(), v),
	})
}
