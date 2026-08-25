# Changelog

All notable changes to this project will be documented in this file. The project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- A foreground `localghost run` now pins the public URL to the last row of the
  terminal for as long as the application runs, instead of printing it once
  and letting the application's output scroll it away. The bar starts in a
  loading state and flips to a solid URL once the application answers — a host
  application is probed on its port and a Compose project through its route —
  so it also reports when the URL is worth clicking. It is drawn before the
  hub is reconciled, so the URL is on screen for the slowest part of a cold
  start, and it names the step being waited on. The scrolling region
  keeps its top margin at row 1, so terminal scrollback is unaffected. Pass
  `--no-status-bar` to disable it; it is skipped automatically when output is
  not a terminal, on `dumb` terminals, in windows under 40 columns, and for
  `--detach` and `--dry-run` runs.

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
