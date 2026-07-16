# Local Development Proxy

A single, persistent [Traefik](https://traefik.io/traefik/) reverse proxy for
independent Docker Compose development projects. Applications opt into a shared
network while keeping their own lifecycle.

This is local-development infrastructure, not a production proxy configuration.

## Quick start

You need Docker Engine or Docker Desktop, Docker Compose 5.x, and loopback port
80 available. Start the proxy without cloning this repository:

```sh
docker compose \
  -f https://github.com/SmileyChris/local-dev-proxy.git@v1.0.0 \
  up -d
```

Open [http://traefik.localhost](http://traefik.localhost) for the dashboard.

Always use an exact release tag or reviewed commit SHA—never `main` or a
floating `latest` tag.

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
      - local-dev-proxy
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network=local-dev-proxy"
      - "traefik.http.routers.${COMPOSE_PROJECT_NAME}-web.rule=Host(`${COMPOSE_PROJECT_NAME}.localhost`)"
      - "traefik.http.services.${COMPOSE_PROJECT_NAME}-web.loadbalancer.server.port=8000"

networks:
  local-dev-proxy:
    external: true
```

The application must listen on `0.0.0.0:8000` inside its container. A checkout
directory named `my-project` is available at `http://my-project.localhost` after
`docker compose up -d`, without DNS or `/etc/hosts` changes.

See [Integrating applications](docs/integrating-applications.md) for the full
contract, explicit service association, secondary services, multiple checkouts,
and framework settings.

Or generate the Compose integration interactively with
`uvx local-dev-proxy generate`; see
[Generating a local override](docs/generating-an-override.md).

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
- [Security and trust](docs/security.md) — Docker socket and remote Compose
  risks
- [Development and releases](docs/development.md) — fixtures, tests, CI, and
  release-candidate checks


## License

[MIT](LICENSE)
