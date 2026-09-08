# Changelog

All notable changes to this project will be documented in this file. The project
uses [Semantic Versioning](https://semver.org/).

## [3.0.0] - 2026-09-01

### Added

- Ghost pages: the hub now remembers every project it routes and serves a
  friendly offline page (HTTP 503) for stopped projects instead of a bare
  404, including the directory and command needed to start them again.
  Unknown hostnames get a page listing remembered projects. New
  `localghost forget` command drops entries. A hub started from the
  self-contained `compose.yaml` is recreated once by the first CLI
  reconcile, which adds the registry mount.
- The bare `http://localhost` hostname now serves the hub's welcome page —
  logo, remembered projects, and links to the Traefik dashboard and
  documentation. Ghost pages share the documentation site's styling.
- On an HTTPS hub, the certificate authority now keeps issuing certificates
  for remembered hostnames, so ghost pages serve over `https://` without
  browser warnings after a project stops.
- New `localghost summon <name>` runs a remembered project from its recorded
  directory, from anywhere; bare `summon` opens an interactive picker on a
  terminal (Enter summons, Delete forgets) and prints a plain listing when
  piped. Ghost pages now offer `summon`/`forget` as click-to-copy command
  chips.
- `localghost hub` groups the shared Traefik container's commands: `hub up`,
  `hub down`, and a new `hub logs [-f]` that no longer requires knowing the
  container's generated name.
- `localghost status` reports hub state, HTTPS, active routes, and remembered
  projects, with `--json` for scripts.
- `localghost trust` gained `install`, `remove`, and `status` subcommands.
- `localghost sessions logs` follows a detached session's output with `-f`, and
  reads Compose sessions' logs from Compose, which `manage attach` never could.
- `summon` now forwards run options (`--port`, `--detach`, `--dry-run`, and the
  rest) instead of ignoring them, and both `summon` and `forget` complete
  remembered project names in the shell.

- Added opt-in tailnet hosting with `localghost tailscale enable`, `status`,
  `trust`, and `disable`. A dedicated tagged userspace gateway, split DNS, and
  suffix-specific CA mirror every supported `.localhost` project, secondary,
  and dashboard route without publishing the hub to the LAN or internet.
- Added a generic suffix mode to the local CA provider. The tailnet provider
  derives routes from the same Docker labels as localhost and references the
  original Docker services and middleware, avoiding a second application
  configuration path.
- `localghost tailscale enable` defaults to the OAuth credential's own tailnet
  and infers its short suffix from the connected Tailscale client; both remain
  available as explicit overrides.
- `localghost tailscale enable` now guides the one-time admin-console setup
  when no credential is present — the device-tag policy entry and the scoped
  OAuth client — and translates tag and scope rejections from the Tailscale
  API into the exact console fix. A working credential is stored in the system
  keyring so `disable` and re-enable need no re-entry; `disable` deletes it.
- While tailnet hosting is enabled, the pinned status bar of a foreground
  run shows the mirrored tailnet URL beside the `.localhost` URL. On narrow
  windows the hint is dropped first, then the tailnet URL, so the primary URL
  always survives.
- The tailnet gateway now carries a Docker healthcheck — the gateway binary
  probing its own health listener, which starts only once the node is
  enrolled and every tailnet listener is up — and `localghost tailscale
  status` reports that observed state as `Gateway health`.

### Changed

- Foreground runs now leave a persistent exit-status diagnosis after the
  transient URL display is removed, with a plain-output fallback hint.
- Trust-store operations stage each selected public root in an isolated mkcert
  `CAROOT`, so managing one public root cannot silently target another.
- Zen NSS nicknames now carry the authority's suffix, so a client can trust the
  `.localhost` root and a tailnet root at the same time. Installing one no
  longer sweeps the other out of the profile; each authority only replaces its
  own superseded entries.
- Trusting a tailnet root now removes a superseded root from this client's
  trust stores before overwriting it on disk. A rotated hub authority — after
  a CA volume purge — no longer leaves an unremovable root trusted under the
  same subject name.
- New tailnet roots are name-constrained to their own suffix, so the anchor a
  client installs cannot vouch for any other name. Existing unconstrained
  roots keep working and gain the constraint when the CA volumes are purged.
- `localghost tailscale trust` accepts an optional `--fingerprint`, verified
  against the downloaded root before anything is installed, so a client can
  pin the value shown by `localghost trust status` on the hosting machine
  instead of trusting the tailnet path alone.
- The certificate-authority bootstrap now runs a binary compiled into the hub
  image instead of `go run` in a throwaway toolchain container, cutting a
  repeat `localghost trust` from about nine seconds to under two and removing
  the Go image download from a first-time setup.
- Starting or reconciling the hub no longer re-checks the build of images that
  already exist. Hub images are tagged with the CLI's version, so the check
  could only ever be a cache hit; skipping it takes a reconcile of a running
  hub from about 2.6 seconds to 1.4. `localghost hub up --rebuild` forces the build
  after editing the bundled Traefik plugin or gateway sources.
- `localghost hub down` now removes containers the current compose files no longer
  describe, matching how application teardown already worked. A lost tailnet
  state file previously left the gateway running and, with it attached, the
  hub network could not be removed either.
- Trust setup now explains before installation that sudo may be requested for
  the system trust store.
- Tailscale enable now offers automatic tailnet trust only when localhost trust
  was already enabled; otherwise it remains non-privileged and points to the
  standard trust command.
- `localghost trust install`, `trust status`, and `trust remove` now manage the
  active tailnet authority alongside `.localhost`; `tailscale trust` remains a
  compatibility alias.
- Tailnet trust downloads use the enrolled gateway address directly, avoiding
  stale public-DNS cache entries for private suffixes such as `.work`.
- Tailnet authorities use suffix-specific certificate subjects and versioned
  CA volumes, preventing trust-store collisions with the `.localhost` root.
- Replaced `localghost generate` with the project-oriented `localghost save`.
  Saving now has one contract across project types: host and custom runs are
  recorded in `.localghost.toml`, Compose integration is written to
  `compose.override.yaml`, and Dockerfile-only projects can be saved as a new
  `compose.yaml`.
- `localghost save` takes the same `--type` as `run` — `compose`,
  `dockerfile`, or a host framework — and detects it when omitted. Options
  that only apply to one kind of project (`--file`, `--service`, `--output`
  for Compose and Dockerfile; `--name`, `--config`, and a trailing command
  for host runs) are rejected with a message naming the resolved type.
  `--project-root` pins a Compose directory just as it does for `run`.
- `localghost run` is now strictly read-only. `run --save` is replaced by
  `save --run`, and `run --service` moves to `save --service`.
- `save` rejects `--name` for a Compose project: its name always comes from
  Docker (`COMPOSE_PROJECT_NAME`, the `.env` file, or the directory name),
  never from a flag — a `--name` there would have moved only the ghost-page
  registry entry, leaving it pointing at a hostname no router serves.
- `save --run` now validates the Compose routing before starting, where the
  removed `run --save` skipped that check.
- `--root` is renamed `--project-root` so it stops reading as a synonym of
  `--directory`.
- Explicit `type = "compose"` run configuration now resolves type ambiguity
  without bypassing validation of the resolved Compose routing model.
- An explicit `save --type` is remembered in `.localghost.toml`, so a Compose
  project that shares a directory with a framework can be pinned once with
  `save --type compose` instead of needing `run --type compose` every time.
  A detected type writes no pin: detection only succeeds when there is
  nothing to remember.
- A bare `localghost` now reports status instead of starting the hub. Use
  `localghost hub up`, or just `localghost run`, which starts the hub itself.
- `localghost down` is now `localghost hub down`. The old spelling read as
  "stop my application" but stops the container every project on the machine
  routes through.
- A bare `localghost trust` now reports trust status instead of installing the
  public root; use `localghost trust install`. Bare lifecycle namespaces now
  report when invoked and never change anything.

### Fixed

- `run --project-root` now discovers `.localghost.toml` inside the explicitly
  pinned root, including when that root is below the invocation directory.
- A Compose save no longer aborts after writing its override when an existing
  `.localghost.toml` refuses the supplementary Compose type pin; it warns and
  still records the saved project.
- `save --output` no longer pins `type = "compose"`: the flag names an
  output file rather than a project root, so there is no root the pin can be
  sure of.
- A Compose save now refuses to write an override that Docker Compose would
  ignore, or one that would silence the override a project already uses.
  Compose merges only the first override name it finds — `compose.override.yml`,
  then `compose.override.yaml`, then `docker-compose.override.yml`, then
  `docker-compose.override.yaml` — so the default output used to disable a
  project's whole `docker-compose.override.yml` (build targets, volumes,
  environment) without a word. The error names the file to `--extend` instead.
- `--output` may now be combined with `--run` when it names an override
  Compose merges by itself, which is the only route a project with an existing
  `docker-compose.override.yml` has to `save ... --run` at all. A genuinely
  nonstandard output is still rejected.
- `--extend` no longer rewraps long lines in services it does not touch. The
  refolded text parsed back to the same value, but it filled the diff under
  review with churn and trailing whitespace.
- The error raised when a service's port cannot be guessed now names the
  whole `localghost save --port` command so it can be pasted as is.
- `--dry-run` now labels each file it would write on stderr, so a save that
  previews two files (a Compose override and its type pin) reads as two
  files. File contents alone still go to stdout.
- Plain (piped) output no longer hard-wraps long paths at 80 columns.
- The foreground `run` banner no longer mentions terminal-multiplexer keys;
  it says to press Ctrl+C.
- `localghost status` suggests `localghost run` when the hub is stopped or
  serving no routes.
- An unconfigured Compose project's error now names `localghost save --run`
  as the single next step.
- `run --detach` on a Compose stack that is already detached now points at
  the live session instead of recording a second one for the same
  containers, where stopping either record took the whole stack down. Both
  branches now also name the `sessions stop` command.

### Removed

- `run --save`, `run --service`, and the deprecated `--framework` alias.
- The `localghost generate` command; use `localghost save`.
- The `--root` spelling; use `--project-root`.
- `localghost --status`, `trust --status`, and `trust --remove`.
- Top-level `localghost down`; use `localghost hub down`.
- The `localghost manage` group; use `localghost sessions`. Its `attach`
  subcommand is now `sessions logs`.

## [2.1.0] - 2026-08-26

### Added

- A foreground `localghost run` now pins the public URL to the last row of the
  terminal for as long as the application runs, rather than printing it once
  and letting the application's own output scroll it away. It is drawn before
  the hub is reconciled, so the URL is on screen for the slowest part of a
  cold start, and it names the step it is waiting on. The scrolling region
  keeps its top margin at row 1, so the terminal's own scrollback is
  unaffected.
- The status bar doubles as a readiness signal. It opens with a spinner and a
  dimmed URL and switches to a solid one once the application first answers —
  a host application is probed on its port, a Compose project through its own
  route — so it reports when the URL is worth opening instead of inviting a
  click that would return a gateway error.
- `localghost run --no-status-bar` turns the bar off. It is also skipped
  automatically when output is not a terminal, on `dumb` terminals, in windows
  narrower than 40 columns, and for `--detach` and `--dry-run` runs, none of
  which have a foreground lifetime to span.
- `localghost run` now warns when something is already serving on the
  application's port, sampled immediately before the application is launched.
  It usually means the application is about to fail to bind, which previously
  surfaced only as the application's own error.

## [2.0.1] - 2026-08-24

### Fixed

- `localghost run` on a Compose project now derives the project name the same
  way `generate` does — `COMPOSE_PROJECT_NAME`, then a `.env` file, then the
  directory — instead of always using the directory name. It previously forced
  its own name onto `docker compose`, so `localghost run` and a plain
  `docker compose up` built two different projects from one directory and
  ignored the name the project had configured for itself.

## [2.0.0] - 2026-08-21

### Breaking

- `generate --mode dockerfile|host` is replaced by `generate --type`, which
  accepts the specific generatable type (`dockerfile`, `django`, `vite`,
  `astro`, `cakephp`, `laravel`, `php`) instead of the generic `host`.
  `compose` is not a valid `generate --type` value: a Compose file is either
  already present, in which case it is detected automatically, or absent, in
  which case one is created from `--type dockerfile`.
- `run --framework` is renamed to `run --type`, now spanning seven values
  (`compose`, `django`, `vite`, `astro`, `cakephp`, `laravel`, `php`) rather
  than the original three. `--framework` keeps working as a hidden,
  deprecated alias that prints a warning.
- A directory containing both a Compose file and a host framework now errors
  and asks for `--type` instead of silently picking one. Previously it ran
  as a host application.
- The new peer types can make a previously-unambiguous project ambiguous.
  The most common case: a default Laravel 11 scaffold — `laravel/framework`
  and `artisan` alongside the stock `package.json`'s Vite dev script — now
  detects as both `laravel` and `vite` and errors asking for `--type`, where
  1.2.0 silently ran it as `vite`. Any directory carrying markers for more
  than one recognized type needs the same disambiguation.
- `generate` no longer falls back to a host bridge when it cannot detect a
  project type and no `--type` is given; it now errors and asks for `--type`
  explicitly. A `generate --no-input --port N` invocation (for example in a
  CI script) that relied on the implicit `host` default in an unrecognized
  directory now fails instead of writing a host-bridge `compose.yaml`.

### Added

- A single `--type` option now spans every project kind localghost
  recognizes: `compose`, `dockerfile` (generate-only), `django`, `vite`,
  `astro`, `cakephp`, `laravel`, and `php`. `php` detects a generic PHP
  application from `composer.json` or a docroot `index.php` and serves it
  with PHP's built-in server (default port 8080); `dockerfile` turns a bare
  `Dockerfile` into a `compose.yaml`.
- `localghost run` now detects modern and legacy CakePHP applications and
  Laravel applications, using their conventional development servers and
  default ports.
- Type detection searches upward from the working directory to the nearest
  project root, stopping after the first VCS marker (`.git`, `.hg`, `.svn`)
  and never adopting a marker at or above `$HOME`. `.localghost.toml` config
  discovery follows that same upward search once a VCS marker is found, but
  does not walk at all without one — only the invocation directory is a
  candidate, since `[run].command` is arbitrary argv that `run` executes, so
  a config file must be inside a project boundary to be trusted. A stray
  `~/package.json` or a forgotten `~/.localghost.toml` can therefore never
  be mistaken for, or adopted as, a project. The detected root controls the
  default hostname while a type may select a different process working
  directory.
- `--root PATH` (and `[run].root` in `.localghost.toml`) pins the project
  root explicitly instead of relying on the upward search.
- Projects may keep repeatable run settings in a `.localghost.toml` file
  (`type`, `name`, `root`, `port`, `command`, with `{port}` interpolated into
  a configured command), which `localghost generate` can write for you and
  `run` discovers automatically using the bounded config-discovery search
  described above.
- `--detach` and the `localghost manage` command (`list`, `attach`, `stop
  [--all]`, `clean`) run and track applications in the background, including
  Compose sessions; a process that survives `SIGKILL` keeps its session
  record with a reported error rather than being silently forgotten, and an
  unreadable record is reported rather than hidden.
- `run --type compose` (or an auto-detected Compose project) hands the
  project to `docker compose up` behind the hub. Before starting anything it
  checks the project is actually wired to the hub — the `localghost` network
  present, with at least one service carrying `traefik.enable=true` on it —
  and refuses with a message naming what's missing rather than starting
  Compose and printing a public URL that would never route. Setting
  `type = "compose"` in `.localghost.toml` skips this check, since a
  committed config file is the project declaring itself already wired.
- Adopt "the hub" and "a bridge" as the names for the two halves of the
  system in documentation, `--help` text, and console messages: the hub is
  the single machine-wide Traefik container, and a bridge is whatever
  connects one application to it.

### Changed

- Dry-run and run summaries show the detected project root and, when different,
  the application process working directory.

### Fixed

- `localghost down` now also removes the profile-gated `bootstrap` one-shot
  container. It previously stayed behind, so Docker still reported the
  `localghost` Compose project as existing.

## [1.2.0] - 2026-08-17

### Fixed

- A second Ctrl+C while `localghost run` is shutting down no longer dumps a
  `_TerminationSignal` traceback. A repeat Ctrl+C within a 2 second grace
  period is ignored as an accidental double-press; after the grace period it
  force-quits with a clean message. Bridge cleanup is retried if interrupted
  mid-teardown.

### Changed

- `localghost run` now selects the first installed manager in a deterministic
  priority order when multiple JavaScript lockfiles are present;
  `package.json`'s `packageManager` field remains the strict override.
- Vite and Astro host runs now forward dev-server flags correctly through pnpm,
  Yarn, and Bun.
- Repeated HTTPS proxy starts no longer force-recreate the Traefik container;
  Docker Compose now recreates it only when its configuration changes.

## [1.1.0] - 2026-07-29

### Added

- Astro framework detection: `localghost run` now auto-detects Astro projects
  (package.json with a dev script and astro dependency), default port 4321.
- `--framework astro` option for explicit selection.
- Auto-cleanup of stale managed bridge containers on `localghost run`.
- PyPI publish workflow: pushing a `v*` tag builds and deploys via trusted
  publishing (OIDC).

### Fixed

- Host-run bridges now include the `io.localghost.tls-domains` label so the
  Traefik CA plugin issues certificates matching the public project hostname
  instead of the Compose project hash.

### Changed

- Generator Compose-model validation now runs as part of the main pytest suite
  instead of through a separate shell script.

## [1.0.2] - 2026-07-21

### Changed

- Proxy images are now tagged with the Localghost release version, ensuring an
  upgrade builds the bundled proxy and plugin source for that release.
- `localghost --version` now prints only the version number for scripting.

## [1.0.1] - 2026-07-21

### Added

- Consistent terminal feedback for proxy lifecycle, generated configuration, and
  foreground runs, with Rich summaries in interactive terminals and plain text
  for scripts.

### Changed

- Expanded HTTPS integration, troubleshooting, trust-state, and complete-removal
  documentation.

### Fixed

- Trusted HTTPS setup now rolls back partial trust-store changes safely, and
  failed removal preserves the desired HTTPS state.
- HTTPS dashboard URLs redirect correctly to `/dashboard/`.
- Django origin checks honor HTTPS and custom proxy ports.
- Route status is scoped to containers attached to the shared `localghost`
  network.
- The bundled certificate provider avoids unnecessary configuration reloads.
- Integration tests are isolated from the developer's persistent HTTPS state.

## [1.0.0] - 2026-07-20

### Added

- A self-contained Traefik 3.7.7 Compose project bound to loopback.
- The fixed `localghost` shared Docker network.
- An internal dashboard route at `http://traefik.localhost`.
- Primary, secondary, and unlabelled consumer examples.
- Linux integration coverage for routing, isolation, lifecycle, health, and port
  binding behavior.
- Focused architecture, integration, operations, troubleshooting, security, and
  development documentation.
- An optional Click CLI, packaged for `uvx`, which creates or safely extends a
  local override and scaffolds Dockerfile or host-native applications.
- A bundled proxy lifecycle command: `localghost` starts or reconciles the
  proxy, and `localghost down` removes it.
- Optional trusted HTTPS: a locally built pinned Traefik image bundles the
  source-loaded provider, `localghost trust`, `localghost trust --remove`, and
  `localghost trust --status` manage the public root through mkcert, and
  failed/declined setup remains HTTP-only.
- `localghost --status` reports proxy state and routes without reconciling it;
  `localghost trust --status` remains the detailed public-root check.
- Generated and host-run routes include a `websecure` TLS router that becomes
  active after HTTPS is enabled.
- `localghost run`, a fileless foreground Django and Vite host-server workflow
  with an ephemeral, pinned Caddy bridge.

### Changed

- Established Localghost with `localghost` as its PyPI project, executable,
  Python namespace, Docker Compose project, and shared Docker network. No
  compatibility alias is provided for the unreleased earlier names.
- Renamed the HTTP-port override to `LOCALGHOST_HTTP_PORT` and host-bridge
  ownership labels to the `io.localghost` namespace.
- Startup guidance uses the documented `uvx localghost down` command and
  distinguishes ordinary cached execution from an explicit package refresh.
- The generator now rejects cross-service router collisions, unsafe settings in
  unresolved custom overrides, malformed Compose data, and incompatible mode
  options without overwriting files, replacing symlinks, or losing permissions.
