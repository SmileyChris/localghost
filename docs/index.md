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
uvx localghost run --save
```

## Run, save, or do both

Use `save` when you want to persist the detected setup without starting the
application:

```sh
uvx localghost save
```

The three forms share the same detection and planning:

- `localghost run` resolves and executes;
- `localghost save` resolves and persists; and
- `localghost run --save` persists and then executes.

The project type is auto-detected, searching upward to the nearest project
root. `--detach` runs the application in the background and
`localghost manage` inspects and stops those sessions. See
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
uvx localghost trust
```

See [Security and trust](security.md) for certificate handling and
[Operations](operations.md) for lifecycle, status, ports, and upgrades.
