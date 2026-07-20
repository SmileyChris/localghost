# Developing Localghost

## Repository layout

- `compose.yaml` is the stable public interface and must remain self-contained.
- `examples/compose.yaml` supplies primary, secondary, and unlabelled fixtures.
- `scripts/integration-test.sh` exercises the compatibility contract.
- `src/localghost/` contains the packaged Click CLI, bundled proxy Compose
  definition, override generator, and source-loaded Traefik provider.
- `pyproject.toml` and `uv.lock` define the build and locked development
  environment.
- `.github/workflows/ci.yml` validates the project on Linux with Docker Compose
  5.1.4, which is the fixed CI baseline rather than a required user-side patch
  version.
- `.github/dependabot.yml` proposes reviewed Python, Traefik, and GitHub Actions
  updates. The pinned Caddy host-bridge image lives in `generator.py`, so review
  it explicitly during dependency maintenance.

The proxy Compose file must not gain application-specific mounts, state, or
configuration files. Consumers rely on the fixed `localghost` project and
`localghost` network names.

## Static validation

Resolve and validate both Compose files:

```sh
docker compose -f compose.yaml config --quiet
COMPOSE_PROJECT_NAME=localghost-fixture-a \
  docker compose -f examples/compose.yaml config --quiet
bash -n scripts/integration-test.sh
uv run ruff check .
uv run pytest
(cd src/localghost/traefik_plugin/src/github.com/SmileyChris/traefik-localghost-ca \
  && go test ./...)
```

Review the fully rendered fixture configuration when changing interpolated
labels:

```sh
COMPOSE_PROJECT_NAME=localghost-fixture-a \
  docker compose -f examples/compose.yaml config
```

## Integration suite

Run:

```sh
./scripts/integration-test.sh
```

The suite is destructive only to Docker resources named `localghost`,
`localghost-fixture-a`, `localghost-fixture-b`, `localghost-fixture-host`, and
`localghost-fixture-dockerfile`. It refuses to begin if any of those resources
already exist and cleans up resources it creates even after failure. Ports 80,
18080, 18443, and 19090 must be available.

Coverage includes:

- validation and proxy-first external-network failure;
- repeated idempotent proxy startup and container health;
- exact loopback port publication and absence of raw API port publication;
- two concurrent fixture projects with isolated primary routes;
- secondary-service routing and rejection of an unlabelled container;
- explicit backend-port selection when another port is exposed;
- generated bridging to an HTTP application running directly on the host;
- trusted HTTPS routing with the bootstrapped public root, without changing the
  host trust store;
- dashboard root redirection and internal dashboard access;
- removal of one application without affecting another or the proxy;
- proxy restart and forced reconciliation without consumer recreation; and
- recreation on a non-default loopback port.

Override test names or ports only when necessary:

```sh
TEST_DEFAULT_PORT=18081 \
TEST_ALTERNATE_PORT=18082 \
TEST_HTTPS_PORT=18444 \
./scripts/integration-test.sh
```

CI should retain the default port-80 run because loopback publication on the
public default is part of the release contract.

## Documentation

Documentation is built with [Zensical](https://zensical.org/). Preview it
locally with:

```sh
uv run zensical serve
```

Build the static site with:

```sh
uv run --frozen zensical build
```

## Build the CLI package

Build both the source distribution and wheel:

```sh
uv build --no-sources
```

Test the local package through the same isolated tool mechanism used after PyPI
publication:

```sh
uvx --from . localghost --help
uvx --from . localghost down --help
uvx --from . localghost generate --help
```

## Release-candidate test

The checked-in source test does not prove that the packaged CLI contains the
proxy definition it starts. Before release, build a candidate wheel, then run
the lifecycle commands from that wheel:

```sh
uv build --no-sources
wheel=$(find dist -maxdepth 1 -name '*.whl' -print -quit)
uvx --isolated --from "$wheel" localghost
uvx --isolated --from "$wheel" localghost down
```

CI performs the same wheel smoke test before the source integration suite.

## PyPI authentication

For releases from GitHub Actions, use PyPI Trusted Publishing (recommended).
After configuring the repository, workflow, and environment in PyPI, `uv
publish --trusted-publishing always` obtains a short-lived credential through
GitHub Actions OIDC; no long-lived PyPI token is stored in GitHub.

For a manual publish, provide a project-scoped PyPI API token to `uv`:

```sh
UV_PUBLISH_TOKEN=pypi-xxxxxxxx uv publish
```

`uv auth login https://upload.pypi.org/legacy/` is an alternative for storing
the token in uv's credential store. Do not commit tokens or credentials. PyPI
does not support account username-and-password uploads; use an API token.

## Release checklist

1. Review dependency changes and security implications, including the Traefik
   proxy image and generated Caddy host-bridge image.
2. Run static validation and the local integration suite.
3. Pilot two independent checkouts with unique Compose project names (e.g. two
   checkouts of the same application).
4. Update `CHANGELOG.md` and `pyproject.toml` version, and all example version
   tags.
5. Make a trial build with `uv build --no-sources`, test the resulting wheel
   with `uvx`, and exercise the lifecycle commands from the release commit in
   CI.
6. After CI passes, use a clean checkout of that exact commit to build the final
   artifacts once, smoke-test the wheel, and record both checksums:
   ```sh
   test -z "$(git status --porcelain)"
   rm -rf dist
   uv build --no-sources
   wheel=$(find dist -maxdepth 1 -name '*.whl' -print -quit)
   sdist=$(find dist -maxdepth 1 -name '*.tar.gz' -print -quit)
   test -n "$wheel" && test -n "$sdist"
   uvx --isolated --from "$wheel" localghost
   uvx --isolated --from "$wheel" localghost down
   sha256sum "$wheel" "$sdist"
   ```
7. Create the immutable SemVer tag and GitHub release for that commit. Publish
   the exact artifacts from step 6 without rebuilding them:
   ```sh
   wheel=$(find dist -maxdepth 1 -name '*.whl' -print -quit)
   sdist=$(find dist -maxdepth 1 -name '*.tar.gz' -print -quit)
   uv publish "$wheel" "$sdist"
   ```
8. Refresh and verify the exact published version, including its lifecycle;
   then confirm a refreshed unpinned resolution selects the same version:
   ```sh
   uvx --refresh localghost@1.0.0 --version
   uvx --refresh localghost@1.0.0
   uvx localghost@1.0.0 down
   uvx --refresh localghost --version
   ```

Consumer-visible changes to fixed names, labels, hostname conventions, or
lifecycle commands require a major version. Additive compatible features may be
minor releases; compatible fixes, reviewed image updates, and documentation
updates may be patch releases.
