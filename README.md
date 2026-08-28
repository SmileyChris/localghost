# localghost

Localghost gives local servers friendly `.localhost` URLs instead of port
numbers:

- `https://storefront.localhost`
- `https://admin.storefront.localhost`
- `https://blog.localhost`

Just a single local hub routing all your development apps by hostname.

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

This is local-development infrastructure, not a production proxy. It runs the
hub — a single, loopback-only [Traefik](https://traefik.io/traefik/) container
— as the `localghost` Compose project on the `localghost` Docker network.

## Quick start

You need Docker Engine or Docker Desktop, Docker Compose 5.x+,
[uv](https://docs.astral.sh/uv/getting-started/installation/), and
port 80 available. From an application directory, run:

```sh
uvx localghost run
```

Localghost detects Django, Vite, Astro, CakePHP, Laravel, plain PHP, and Docker
Compose projects, starts the shared hub when needed, and keeps the local URL
visible while the application runs. Press Ctrl+C to stop the application.

An unconfigured Compose project asks you to save its routing setup first:

```sh
uvx localghost run --save
```

Save a custom command the same way:

```sh
uvx localghost run --save --port 8080 -- ./server --port 8080
```

Use `save` when you want to persist setup without starting anything:

```sh
uvx localghost save
```

## Hub and background runs

Start or reconcile only the shared hub with:

```sh
uvx localghost
```

Open [http://traefik.localhost](http://traefik.localhost) for its dashboard,
inspect it with `uvx localghost --status`, and remove it with
`uvx localghost down`.

Run it in the background with `--detach` and manage it afterwards:

```sh
uvx localghost run --detach
uvx localghost manage list
uvx localghost manage stop SESSION_ID
```

See [Saving project setup](docs/saving-setup.md) for `.localghost.toml`, Compose
overrides, custom commands, explicit type selection, and safe updates.

## Documentation

The [localghost documentation](docs/index.md) covers application integration,
saving setup, host-native servers, HTTPS, operations, troubleshooting, security,
architecture, and development.

For a local documentation preview, install the development dependencies and run
`uv run zensical serve`.
