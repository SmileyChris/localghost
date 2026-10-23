# local<span class="brand-accent">ghost</span>

<img class="localghost-home-logo" src="assets/logo.png" alt="Localghost" width="360">

Localghost is a loopback-only Docker Compose hub that gives local
applications friendly `.localhost` URLs.

## Quick start

Requirements: Docker Engine or Docker Desktop, Docker Compose 5.x (CI tests
5.1.4), [uv](https://docs.astral.sh/uv/getting-started/installation/), and
loopback port 80 available.

From an application directory, run:

```sh
uvx localghost run
```

Localghost detects the project, starts the shared hub when needed, and reports
the application's `.localhost` URL. An unconfigured Compose project tells you
to save its routing setup and continue with:

```sh
uvx localghost save --run
```

## Run, save, or do both

`run` starts an application without touching project files. `save` persists
the detected setup (`.localghost.toml` for host apps, a Compose override for
Compose projects) and `save --run` does both. See
[Save project setup](saving-setup.md).

## Optional extras

HTTP is always available. For trusted HTTPS, install `mkcert` and run:

```sh
uvx localghost trust install
```

To reach the same routes from your Tailscale devices, run:

```sh
uvx localghost tailscale share
```

Both are opt-in; the ordinary `.localhost` routes stay loopback-only.

## Where to go next

**Use localghost**

- [Run host applications](running-host-apps.md): the project types localghost
  detects, default ports, `--type`, detached sessions, and `.localghost.toml`.
- [Save project setup](saving-setup.md): what `save` writes, custom commands,
  and updating a saved setup safely.
- [Integrate applications](integrating-applications.md): the Compose contract
  behind it all, for hand-written labels and secondary services.
- [Host on a tailnet](tailscale.md): the same routes on your Tailscale
  devices through an opt-in gateway and split DNS.

**Operate localghost**

- [Operations](operations.md): hub lifecycle, status, ports, trusted HTTPS,
  and upgrades.
- [Troubleshooting](troubleshooting.md): symptoms and fixes, starting with the
  hub status and logs.
- [Security and trust](security.md): what the loopback-only design does and
  does not protect, and how certificates are handled.
- [Architecture](architecture.md): how the hub, bridges, and request path fit
  together.

**Develop localghost**

- [Development](development.md): repository layout, checks, and releases.
