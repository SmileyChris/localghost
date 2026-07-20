# Generating a local<span class="brand-accent">ghost</span> override

Compose automatically merges `compose.override.yaml` with `compose.yaml`. The
optional generator creates that local file, adding the most likely HTTP service
to the shared proxy without modifying the application's base Compose file.

The helper requires Docker Compose 5.x (CI tests 5.1.4) and
[uv](https://docs.astral.sh/uv/getting-started/installation/). `uvx` installs the
CLI and its Python dependencies into an isolated, cached environment.

## Generate the override

From the application directory, run:

```sh
uvx localghost generate
```

It resolves the application with `docker compose config`, chooses the most
likely service and container HTTP port, and writes `compose.override.yaml`.
Review that file, then start the application normally:

```sh
docker compose config
docker compose up -d
```

The generated override:

- preserves the chosen service's existing network memberships;
- adds the external `localghost` network;
- opts the service into Traefik;
- creates a primary route at `<project>.localhost`;
- selects an explicit container port; and
- leaves every other service unchanged.

The proxy must already be running so the external network exists.

## How selection works

Service selection prefers services named `web`, `app`, `api`, `server`, or
`backend`, as well as a service matching the Compose project name. Services with
declared `ports` or `expose` entries are preferred, while common infrastructure
and worker names are de-prioritized.

In an interactive terminal, the CLI shows every service and its detected ports,
then asks which service to use with the most likely choice as the default.

Port selection uses declared container targets, not published host ports. A
single declared port is selected automatically; common HTTP development ports
are preferred when there are several.

Make either choice explicit when needed:

```sh
uvx localghost generate \
  --service app \
  --port 8000
```

`--port` is also useful when the port is exposed only by a Dockerfile or opened
by the process without being declared in Compose.

Use `--dry-run` to inspect the generated YAML on standard output. Use `--file`
more than once to inspect an existing Compose file stack, or `--output` to choose
a filename.

## Existing overrides

If `compose.override.yaml` already exists, the CLI offers to extend it. It uses
round-trip YAML editing to retain comments and existing configuration, checks
both the resolved Compose model and on-disk override for conflicting network and
Traefik settings, and creates a `.bak` backup before writing. It refuses changes
when the existing configuration cannot be merged safely.

For non-interactive use, pass `--extend` explicitly. Use another output file or
`--dry-run` when you prefer to merge the result manually.

Compose only loads `compose.override.yaml` automatically. A different output
name must be supplied explicitly alongside the base file:

```sh
LOCALGHOST_IMAGE_TAG="v$(uv run localghost --version)" docker compose -f compose.yaml -f compose.localghost.yaml up -d
```

If the override is personal rather than shared project configuration, add it to
the application's `.gitignore` or `.git/info/exclude`.

## Project names

The generated hostname uses Compose's automatically derived project name. The
generator rejects names that are not DNS-safe. Set a safe, unique
`COMPOSE_PROJECT_NAME` in `.env` only when the checkout directory name is unsafe,
duplicated, or not the hostname you want.

## Projects without Compose

The same command provides a guided path when the current directory has no
Compose file:

```sh
uvx localghost generate
```

If a `Dockerfile` exists, the default is a new `compose.yaml` with `build: .`,
the external proxy network, and the primary route. The CLI asks for the
container HTTP port.

For an application running directly on the host, choose `host`. The generated
Compose project runs a pinned Caddy bridge between Traefik and the host port:

```sh
uvx localghost generate --mode host --port 3000
```

The host process must listen on a Docker-reachable interface such as `0.0.0.0`;
a process bound only to host `127.0.0.1` is normally unreachable from the bridge
container. The bridge connects through `host.docker.internal` and does not
publish another host port.

Binding a development server to `0.0.0.0` may also make its host port reachable
from the local network. Prefer binding specifically to a Docker-reachable host
interface when the framework supports it, and use the host firewall on untrusted
networks.

For scripts and other non-interactive use, pass `--no-input` together with
`--mode` and `--port`.

## Run a host-native server

`run` is the ephemeral alternative to persistent `generate --mode host`: it
writes no Compose file in the checkout, starts a foreground application and an
owned Caddy bridge, then removes that bridge when the application exits. The
shared Traefik proxy remains running.

```sh
uvx localghost run
uvx localghost run --framework django --name review-123 --port 8010
uvx localghost run --port 3000 -- npm run custom-dev -- --port 3000
uv run localghost run --directory /path/to/application
```

It detects Django from `manage.py` and Vite from a `package.json` dev script
with a Vite dependency. Django uses the project runner (uv, Poetry, Pipenv, or
a virtualenv); Vite uses the declared or locked package manager. An ambiguous
project needs `--framework`; custom commands always need `--port`. Detected
servers bind to `0.0.0.0`, which can expose the raw development port to a LAN,
and run package scripts with your normal host permissions.

`--name` takes precedence over `COMPOSE_PROJECT_NAME`, then `.env`, then the
normalized checkout name. Detected applications use port 8000 (Django) or 5173
(Vite), choosing the next free port when needed; an explicit `--port` is strict
and fails if occupied. `--dry-run` performs no Docker inspection or startup.

The command removes its bridge on normal exit, Ctrl+C, or SIGTERM, while leaving
the shared proxy running. It refuses an existing route instead of replacing it;
see [route collisions](troubleshooting.md#route-already-exists).

Django projects must allow the `<name>.localhost` host and configure CSRF
trusted origins as appropriate. Vite's HTTP, HMR, and WebSocket traffic all
pass through the bridge; `.localhost` needs no additional Vite host allowlist.
For Django, `run` checks the loaded settings when it can and warns if the host
or HTTP origin is absent; startup still proceeds when settings cannot load.
Use `--dry-run` to print the chosen framework, command, URL, port, and bridge
YAML without starting Docker or the application.
