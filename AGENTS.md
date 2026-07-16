# Contributor guide

## Purpose and boundaries

This repository provides one persistent, loopback-only Traefik proxy for
independent local Docker Compose projects. It is local-development
infrastructure, not a production deployment.

- `compose.yaml` is the public, self-contained interface. Do not add
  application-specific mounts, state, or configuration files to it.
- The Compose project name and Docker network are both fixed as
  `local-dev-proxy`.
- Consumer applications own their own lifecycle and must never include the
  proxy Compose file in their normal `up` or `down` commands.
- Preserve loopback-only HTTP publication, explicit Traefik opt-in
  (`traefik.enable=true`), and `exposedByDefault=false`.
- Keep the Traefik image pinned to an exact version; update its version,
  documentation, tests, and changelog together when needed.

## Repository map

- `compose.yaml` — proxy configuration and compatibility contract.
- `docs/` and `README.md` — user-facing operating and integration guidance.
- `examples/compose.yaml` — integration fixtures for primary, secondary, and
  unlabelled services.
- `src/local_dev_proxy/` — Click CLI and Compose override generator.
- `tests/` — unit, generator, and Compose-contract tests.
- `scripts/integration-test.sh` — Docker end-to-end contract suite.

## Development commands

Use the locked environment with `uv`:

```sh
uv run ruff check .
uv run pytest
./tests/generator-test.sh
docker compose -f compose.yaml config --quiet
COMPOSE_PROJECT_NAME=ldp-fixture-a docker compose -f examples/compose.yaml config --quiet
bash -n scripts/integration-test.sh
```

Run the full Docker integration suite only when Docker is available and ports
80 and 18080 are free:

```sh
./scripts/integration-test.sh
```

The integration script deliberately creates and removes only resources named
`local-dev-proxy`, `ldp-fixture-a`, and `ldp-fixture-b`; it refuses to run if
those already exist. Do not weaken its cleanup or isolation checks.

For package changes, also run:

```sh
uv build --no-sources
uvx --from . local-dev-proxy --help
```

## Change expectations

- Keep generated Compose YAML round-trip safe: the CLI should extend an
  existing override without discarding comments or unrelated configuration.
- Keep router and service names project-scoped, and retain explicit backend
  port labels and the `local-dev-proxy` Docker-network label.
- Treat changes to fixed names, hostname conventions, labels, or lifecycle
  commands as potential breaking changes; update the compatibility docs and
  tests in the same change.
- The default bootstrap uses the current `uvx` package release. Document a
  version-pinned `uvx` command when reproducibility or review is important.
- Update `CHANGELOG.md`, `pyproject.toml`, docs, and release examples together
  for a release.

## Before release

Follow `docs/development.md`: run static checks and the integration suite,
pilot two independent checkouts with distinct project names, build and install
the wheel, and exercise an exact release-candidate Git Compose URL in CI before
publishing a SemVer tag and matching PyPI package.
