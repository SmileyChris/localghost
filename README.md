# Localghost

Localghost gives local Docker Compose projects and host-native development
servers friendly `.localhost` URLs. The `localghost` command keeps one small,
loopback-only [Traefik](https://traefik.io/traefik/) proxy running while each
application keeps its own lifecycle.

This is local-development infrastructure, not a production proxy configuration.
The proxy runs as the `localghost` Compose project on the shared `localghost`
Docker network.

## Quick start

You need Docker Engine or Docker Desktop, Docker Compose 5.x (CI tests 5.1.4),
[uv](https://docs.astral.sh/uv/getting-started/installation/), and loopback
port 80 available. Start the proxy without cloning this repository:

```sh
uvx localghost
```

Open [http://traefik.localhost](http://traefik.localhost) for the dashboard.
The command creates or reconciles the proxy and waits for it to become healthy.
When it is already running, it lists active routes and their sources. To stop
and remove it later, run:

```sh
uvx localghost down
```

To inspect its state without starting or reconciling it, run:

```sh
uvx localghost --status
```

## Documentation

The [Localghost documentation](docs/index.md) covers application integration,
generation, host-native servers, HTTPS, operations, troubleshooting, security,
architecture, and development.

For a local documentation preview, install the development dependencies and run
`uv run zensical serve`.
## License

[MIT](LICENSE)
