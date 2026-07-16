# Changelog

All notable changes to this project will be documented in this file. The project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- A self-contained Traefik 3.7.7 Compose project bound to loopback.
- The fixed `local-dev-proxy` shared Docker network.
- An internal dashboard route at `http://traefik.localhost`.
- Primary, secondary, and unlabelled consumer examples.
- Linux integration coverage for routing, isolation, lifecycle, health, and port
  binding behavior.
- Focused architecture, integration, operations, troubleshooting, security, and
  development documentation.
- An optional Click CLI, packaged for `uvx`, which creates or safely extends a
  local override and scaffolds Dockerfile or host-native applications.
- A bundled proxy lifecycle command: `local-dev-proxy` starts or reconciles the
  proxy, and `local-dev-proxy down` removes it.
