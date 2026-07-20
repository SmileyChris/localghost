# Contributor guide

This repository provides a persistent, loopback-only Traefik proxy for local
Docker Compose projects. It is development infrastructure, not a production
deployment.

## Guardrails

- Keep `compose.yaml` self-contained and free of application-specific state.
- Keep the Compose project named `localghost` and the Docker network named
  `localghost`.
- Consumer projects own their lifecycle; do not include the proxy Compose file
  in their normal `up` or `down` commands.
- Preserve loopback-only HTTP publishing, explicit `traefik.enable=true`
  opt-in, and `exposedByDefault=false`.
- Pin Traefik to an exact version and update docs, tests, and changelog with it.
- Preserve comments and unrelated configuration when extending Compose YAML.
- Keep generated router and service names project-scoped, with explicit backend
  port and `localghost` network labels.

## Checks

Use the locked `uv` environment:

```sh
uv run ruff check .
uv run pytest
./tests/generator-test.sh
docker compose -f compose.yaml config --quiet
COMPOSE_PROJECT_NAME=localghost-fixture-a docker compose -f examples/compose.yaml config --quiet
bash -n scripts/integration-test.sh
```

Run `./scripts/integration-test.sh` only when Docker is available and ports 80
and 18080 are free. Do not weaken its cleanup or isolation checks. For package
changes, also run `uv build --no-sources` and
`uvx --from . localghost --help`.

Treat changes to fixed names, hostnames, labels, or lifecycle commands as
potentially breaking and update compatibility docs and tests. Follow
`docs/development.md` for releases.
