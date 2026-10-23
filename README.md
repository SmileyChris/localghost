# localghost

Localghost gives local servers friendly `.localhost` URLs instead of port
numbers:

- `https://storefront.localhost`
- `https://admin.storefront.localhost`
- `https://blog.localhost`

One loopback-only [Traefik](https://traefik.io/traefik/) hub routes every
development app by hostname, so nothing needs its own port.

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

## Quick start

You need Docker Engine or Docker Desktop, Docker Compose 5.x+,
[uv](https://docs.astral.sh/uv/getting-started/installation/), and
port 80 available. From an application directory, run:

```sh
uvx localghost run
```

Localghost detects Django, Vite, Astro, CakePHP, Laravel, plain PHP, static
HTML, and Docker Compose projects, starts the shared hub when needed, and
keeps the local URL visible while the application runs. Press Ctrl+C to stop
the application.

An unconfigured Compose project asks you to save its routing setup first:

```sh
uvx localghost save --run
```

Save a custom command the same way:

```sh
uvx localghost save --port 8080 --run -- ./server --port 8080
```

Use `save` when you want to persist setup without starting anything:

```sh
uvx localghost save
```

## Hub and background runs

The hub starts on demand, but you can manage it directly:

```sh
uvx localghost hub up      # start or reconcile the hub
uvx localghost status      # inspect hub state and routes
uvx localghost hub down    # remove it
```

Its dashboard is at [http://traefik.localhost](http://traefik.localhost).

Add `--detach` to `run` to keep an application going in the background, then
use `sessions`, `sessions logs NAME -f`, `restart NAME`, and
`sessions stop NAME` to manage it.

## Documentation

Start at the [documentation home](docs/index.md), or jump straight in:

- [Run host applications](docs/running-host-apps.md): project types, ports,
  detached sessions, and `.localghost.toml`.
- [Save project setup](docs/saving-setup.md): what `save` writes and how to
  update it safely.
- [Integrate applications](docs/integrating-applications.md): the Compose
  contract, labels, and secondary services.
- [Host on a tailnet](docs/tailscale.md): the same URLs on your Tailscale
  devices via an opt-in gateway.
- [Operations](docs/operations.md), [Troubleshooting](docs/troubleshooting.md),
  [Security and trust](docs/security.md), and
  [Architecture](docs/architecture.md): running, fixing, and understanding the
  hub.
- [Development](docs/development.md): working on localghost itself.

Preview the docs locally with `uv run zensical serve` after installing the
development dependencies.
