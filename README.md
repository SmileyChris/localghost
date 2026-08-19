# localghost

Localghost gives local servers friendly `.localhost` URLs instead of port
numbers:

- `https://storefront.localhost`
- `https://admin.storefront.localhost`
- `https://blog.localhost`

Just a single local proxy routing all your development apps by hostname.

```
          ▒█████████████▒
        ███▒           ▒███░
      ▓██░               ░███           ██                          ██
     ██▒                   ▒██          ██░                         ██░
    ▓█▒                     ░██         ██░ ░█████░  ▒█████  █████▓ ██░
   ░██    ███▓       ████    ▓█▒        ██░░██   ██░▒█▓     ██░ ░█▓ ██░
   ██░    ███▓       ▓███    ░██        ██░░██░  ██░▒██     ██▒ ░█▓ ██░
   ██░         █   █          ██        ██░ ░█████░  ░█████  █████▓ ██░
   ██░         ▒███▒          ██░
   ██                         ██░            ░▒▒                       ░
   ██                 ▓█████████▒            ░▒▒                      ▒▒
   ██              ▓██▒▒▒▒▒▒▒▒██▒    ░▒▒▒▒▒▒ ░▒▒▒▒▒▒░  ▒▒▒▒▒░ ░▒▒▒▒░ ▒▒▒▒▒
  ░██░          ░██▓▒▒▒▒▒▒▒▒▒▒▒██   ░▒▒   ▒▒ ░▒▒  ░▒░ ▒▒░  ░▒░ ░▒░    ▒▒
  ▒████▓     ▒███▒▒▒▒▒▒▒▒▒▒▒▒▒▒██   ░▒▒   ▒▒ ░▒▒  ░▒▒ ▒▒░  ░▒░    ░▒░ ▒▒░
  ██▒▒▒▒▓▓█▓▓▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒██░    ▒▒▒▒▒▒ ░▒▒  ░▒▒  ░▒▒▒▒░ ░▒▒▒▒░  ░▒▒▒
 ░██▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒██  ░▒   ░▒▒
 ░██▒▒▒▒███████▓▒▒▒▒███████▒▒▒▒▒██    ░▒▒▒
  ▒█████▓     ▒█████▓     ▒██████
```

This is local-development infrastructure, not a production proxy. It runs a
single, loopback-only [Traefik](https://traefik.io/traefik/) proxy as the
`localghost` Compose project on the shared `localghost` Docker network.

## Quick start

You need Docker Engine or Docker Desktop, Docker Compose 5.x+,
[uv](https://docs.astral.sh/uv/getting-started/installation/), and
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

## Running local apps

`localghost run` detects Django, Vite, Astro, CakePHP, Laravel, and Docker
Compose projects and serves them behind the proxy with zero configuration:

```sh
cd my-project
uvx localghost run
```

The app is available at `https://my-project.localhost` (or `http://` if HTTPS
is not configured). Press Ctrl+C to stop it.

Run it in the background with `--detach` and manage it afterwards:

```sh
uvx localghost run --detach
uvx localghost manage list
uvx localghost manage stop SESSION_ID
```

Repeatable settings live in a `.localghost.toml` file; see
[Running host applications](docs/running-host-apps.md).

## Documentation

The [localghost documentation](docs/index.md) covers application integration,
generation, host-native servers, HTTPS, operations, troubleshooting, security,
architecture, and development.

For a local documentation preview, install the development dependencies and run
`uv run zensical serve`.
