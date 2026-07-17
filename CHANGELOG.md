# Changelog

All notable changes to this project will be documented in this file. The project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `localhost run`, a fileless foreground Django and Vite host-server
  workflow with an ephemeral, pinned Caddy bridge.
- Consistent terminal feedback for proxy lifecycle, generated configuration, and
  foreground runs, with Rich summaries in interactive terminals and plain text
  for scripts.

### Changed

- Established Localhost Proxy with `localhost` as its PyPI project,
  executable, Python namespace, and Docker Compose project. The shared Docker
  network is `localhost-proxy`; no compatibility alias is provided because
  the package has not been published.
- Renamed the HTTP-port override to `LOCALHOST_HTTP_PORT` and host-bridge
  ownership labels to the `io.localhost` namespace.

## [1.0.0] - 2026-07-16

### Added

- A self-contained Traefik 3.7.7 Compose project bound to loopback.
- The fixed `localhost-proxy` shared Docker network.
- An internal dashboard route at `http://traefik.localhost`.
- Primary, secondary, and unlabelled consumer examples.
- Linux integration coverage for routing, isolation, lifecycle, health, and port
  binding behavior.
- Focused architecture, integration, operations, troubleshooting, security, and
  development documentation.
- An optional Click CLI, packaged for `uvx`, which creates or safely extends a
  local override and scaffolds Dockerfile or host-native applications.
- A bundled proxy lifecycle command: `localhost` starts or reconciles the
  proxy, and `localhost down` removes it.

### Changed

- Startup guidance uses the documented `uvx localhost down` command and
  distinguishes ordinary cached execution from an explicit package refresh.
- The generator now rejects cross-service router collisions, unsafe settings in
  unresolved custom overrides, malformed Compose data, and incompatible mode
  options without overwriting files, replacing symlinks, or losing permissions.
