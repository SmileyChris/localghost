# Development and releases

## Repository layout

- `compose.yaml` is the stable public interface and must remain self-contained.
- `examples/compose.yaml` supplies primary, secondary, and unlabelled fixtures.
- `scripts/integration-test.sh` exercises the compatibility contract.
- `src/local_dev_proxy/` contains the packaged Click CLI and override generator.
- `pyproject.toml` and `uv.lock` define the build and locked development
  environment.
- `.github/workflows/ci.yml` validates the project on Linux with Docker Compose
  5.1.4.
- `.github/dependabot.yml` proposes reviewed Traefik and GitHub Actions updates.

The proxy Compose file must not gain application-specific mounts, state, or
configuration files. Consumers rely on the fixed project and network names.

## Static validation

Resolve and validate both Compose files:

```sh
docker compose -f compose.yaml config --quiet
COMPOSE_PROJECT_NAME=ldp-fixture-a \
  docker compose -f examples/compose.yaml config --quiet
bash -n scripts/integration-test.sh
uv run ruff check .
uv run pytest
```

Review the fully rendered fixture configuration when changing interpolated
labels:

```sh
COMPOSE_PROJECT_NAME=ldp-fixture-a \
  docker compose -f examples/compose.yaml config
```

## Integration suite

Run:

```sh
./scripts/integration-test.sh
```

The suite is destructive only to Docker resources named `local-dev-proxy`,
`ldp-fixture-a`, and `ldp-fixture-b`. It refuses to begin if any of those
resources already exist and cleans up resources it creates even after failure.
Port 80 and the alternate test port 18080 must be available.

Coverage includes:

- validation and proxy-first external-network failure;
- repeated idempotent proxy startup and container health;
- exact loopback port publication and absence of raw API port publication;
- two concurrent fixture projects with isolated primary routes;
- secondary-service routing and rejection of an unlabelled container;
- explicit backend-port selection when another port is exposed;
- generated bridging to an HTTP application running directly on the host;
- dashboard root redirection and internal dashboard access;
- removal of one application without affecting another or the proxy;
- proxy restart and forced reconciliation without consumer recreation; and
- recreation on a non-default loopback port.

Override test names or ports only when necessary:

```sh
TEST_DEFAULT_PORT=18081 \
TEST_ALTERNATE_PORT=18082 \
./scripts/integration-test.sh
```

CI should retain the default port-80 run because loopback publication on the
public default is part of the release contract.

## Build the CLI package

Build both the source distribution and wheel:

```sh
uv build
```

Test the local package through the same isolated tool mechanism used after PyPI
publication:

```sh
uvx --from . local-dev-proxy --help
uvx --from . local-dev-proxy generate --help
```

## Release-candidate test

The checked-in source test does not prove that Docker Compose can fetch a
particular published Git reference. Before release, create a private or
prerelease tag, then manually run the **CI** workflow with an exact Git Compose
URL in its `tagged_compose_url` input:

```text
https://github.com/SmileyChris/local-dev-proxy.git@v1.0.0-rc.1
```

The workflow uses the remote proxy file while retaining the checked-out fixture
and test script. A successful run exercises the same integration contract
through the public consumption path.

## Release checklist

1. Review dependency changes and security implications.
2. Run static validation and the local integration suite.
3. Pilot two independent checkouts with unique Compose project names (e.g. two checkouts of the same application).
4. Update `CHANGELOG.md`, `pyproject.toml`, and all example version tags.
5. Build with `uv build --no-sources` and test the resulting wheel with `uvx`.
6. Exercise the exact release-candidate Git URL in CI.
7. Publish the immutable SemVer tag, release notes, and matching PyPI package.
8. Re-run the documented quick-start and `uvx` commands using the published
   versions.

Consumer-visible changes to fixed names, labels, hostname conventions, or
lifecycle commands require a major version. Additive compatible features may be
minor releases; compatible fixes and documentation updates may be patch
releases.
