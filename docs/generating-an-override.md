# Generating a local override

Compose automatically merges `compose.override.yaml` with `compose.yaml`. The
optional generator creates that local file, adding the most likely HTTP service
to the shared proxy without modifying the application's base Compose file.

The helper requires Docker Compose 5.x and
[uv](https://docs.astral.sh/uv/getting-started/installation/). `uvx` installs the
CLI and its Python dependencies into an isolated, cached environment.

## Generate the override

From the application directory, run:

```sh
uvx local-dev-proxy generate
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
- adds the external `local-dev-proxy` network;
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
uvx local-dev-proxy generate \
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
the resolved Compose model for conflicting network and Traefik settings, and
creates a `.bak` backup before writing. It refuses changes when the existing
configuration cannot be merged safely.

For non-interactive use, pass `--extend` explicitly. Use another output file or
`--dry-run` when you prefer to merge the result manually.

Compose only loads `compose.override.yaml` automatically. A different output
name must be supplied explicitly alongside the base file:

```sh
docker compose -f compose.yaml -f compose.local-dev-proxy.yaml up -d
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
uvx local-dev-proxy generate
```

If a `Dockerfile` exists, the default is a new `compose.yaml` with `build: .`,
the external proxy network, and the primary route. The CLI asks for the
container HTTP port.

For an application running directly on the host, choose `host`. The generated
Compose project runs a pinned Caddy bridge between Traefik and the host port:

```sh
uvx local-dev-proxy generate --mode host --port 3000
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
