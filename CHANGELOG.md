# Changelog

## Unreleased

- Add configured host/Compose runs, detached session metadata, and the
  `localghost manage` session commands.

All notable changes to this project will be documented in this file. The project
uses [Semantic Versioning](https://semver.org/).

## Unreleased

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
