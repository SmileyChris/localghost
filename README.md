# localhost

Use friendly `.localhost` URLs for local Docker Compose projects and host-native
development servers. `localhost` keeps one small, loopback-only
[Traefik](https://traefik.io/traefik/) proxy running while each application
keeps its own lifecycle.

This is local-development infrastructure, not a production proxy configuration.
The proxy's Compose project is `localhost`; containers connect through the
shared `localhost-proxy` Docker network.

## Quick start

You need Docker Engine or Docker Desktop, Docker Compose 5.x (CI tests 5.1.4),
[uv](https://docs.astral.sh/uv/getting-started/installation/), and loopback
port 80 available. Start the proxy without cloning this repository:

```sh
uvx localhost
```

Open [http://traefik.localhost](http://traefik.localhost) for the dashboard.
The command creates or reconciles the proxy and waits for it to become healthy.
When it is already running, it also lists active routes and where they come
from: a Compose project and service, or a host application's checkout path. To
stop and remove it later, run:

```sh
uvx localhost down
```

`uvx` may reuse a cached CLI release. Fetch the newest published release when
you need it with:

```sh
uvx --refresh localhost
```

See [Operating the proxy](docs/operations.md#upgrade) for reproducible,
version-specific use.

## Connect an application

Compose uses the checkout directory as the project name. If that name is unique
and contains only lowercase letters, digits, and hyphens, no configuration is
needed.

Attach the service to the shared network and opt into Traefik:

```yaml
services:
  web:
    networks:
      - default
      - localhost-proxy
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network=localhost-proxy"
      - "traefik.http.routers.${COMPOSE_PROJECT_NAME}-web.rule=Host(`${COMPOSE_PROJECT_NAME}.localhost`)"
      - "traefik.http.services.${COMPOSE_PROJECT_NAME}-web.loadbalancer.server.port=8000"

networks:
  localhost-proxy:
    external: true
```

The application must listen on `0.0.0.0:8000` inside its container. A checkout
directory named `my-project` is available at `http://my-project.localhost` after
`docker compose up -d`, without DNS or `/etc/hosts` changes.

See [Integrating applications](docs/integrating-applications.md) for the full
contract, explicit service association, secondary services, multiple checkouts,
and framework settings.

Or generate Compose configuration with `uvx localhost generate`; see
[Generating a local override](docs/generating-an-override.md). Use
`generate --mode host` when you want to keep and manage a bridge Compose file.

## Run a host application

For a Django or Vite development server running directly on your machine, use
the foreground `run` command instead. It writes no files to the checkout:

```sh
uvx localhost run
```

It detects the development command, creates a temporary bridge, and serves the
application at `http://<project>.localhost` until the command exits. Use
`--dry-run` to inspect the command and generated bridge YAML, or provide your
own command with an explicit port:

```sh
uvx localhost run --port 3000 -- npm run dev
```

When running the tool from another checkout, point it at the application:

```sh
uv run localhost run --directory /path/to/application
```

See [Run a host-native server](docs/generating-an-override.md#run-a-host-native-server)
for framework detection, port selection, and Django settings.

## Documentation

- [Architecture](docs/architecture.md) — ownership, discovery, networking, and
  hostname conventions
- [Integrating applications](docs/integrating-applications.md) — complete
  Compose examples and application requirements
- [Generating Compose configuration](docs/generating-an-override.md) — add an
  existing service or scaffold a Dockerfile or host-native application
- [Operating the proxy](docs/operations.md) — lifecycle, upgrades, ports, and
  inspection
- [Troubleshooting](docs/troubleshooting.md) — common failures and diagnostic
  commands
- [Security and trust](docs/security.md) — Docker socket and package-trust
  risks
- [Development and releases](docs/development.md) — fixtures, tests, CI, and
  release-candidate checks


## License

[MIT](LICENSE)
