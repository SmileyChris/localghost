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
uvx localghost save compose --run
```

## Run, save, or do both

`run` resolves and starts an application; it never writes project files. Use
`save` to persist the detected setup without starting anything:

```sh
uvx localghost save
```

Add `--run` to persist and then start in one step:

```sh
uvx localghost save --run
```

The three forms share the same detection and planning:

- `localghost run` resolves and executes;
- `localghost save` resolves and persists; and
- `localghost save --run` persists and then executes.

`save` is a command group — `save host`, `save compose`, and `save dockerfile`
carry the options specific to each project type, and bare `save` still
detects the type and dispatches to the right one.

The project type is auto-detected, searching upward to the nearest project
root. `--detach` runs the application in the background and
`localghost sessions` inspects, follows, and stops those sessions. See
[Running host applications](running-host-apps.md) for the full workflow, custom
ports, explicit type selection, `.localghost.toml` settings, detached
sessions, and Django runner resolution.

For the complete Compose contract, project naming, secondary services, and
saved setup, see [Integrate applications](integrating-applications.md) and
[Save project setup](saving-setup.md).

## Optional trusted HTTPS

HTTP is always available. To install Localghost's local development root and
enable HTTPS, first install `mkcert`, then run:

```sh
uvx localghost trust install
```

See [Security and trust](security.md) for certificate handling and
[Operations](operations.md) for lifecycle, status, ports, and upgrades.

## Optional tailnet hosting

To make the same routes available inside a Tailscale tailnet, enable the
opt-in tagged gateway and split DNS with `uvx localghost tailscale enable`.
The ordinary `.localhost` routes stay local. See
[Host on a tailnet](tailscale.md) for setup, trust, and scope.
