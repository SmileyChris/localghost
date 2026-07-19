# Localhost Proxy security and trust

This project is designed for a trusted local development machine. It is not a
production security design and should not be exposed to a LAN or the internet.

## Current safeguards

The v1 configuration reduces accidental exposure by:

- publishing HTTP only on `127.0.0.1`;
- leaving Traefik's insecure API mode disabled;
- serving the dashboard through `api@internal` on the loopback-bound `web`
  entrypoint;
- setting `exposedByDefault=false` and requiring `traefik.enable=true`;
- selecting the fixed `localhost-proxy` network for backend traffic;
- pinning Traefik to an exact image version; and
- disabling anonymous usage reporting and automatic version checks.

These controls limit network exposure and accidental routing. They do not make
the Docker API or an untrusted container safe.

## Docker socket access

Traefik needs Docker metadata and events for label-based discovery. V1 mounts
`/var/run/docker.sock` read-only into the Traefik container.

Read-only is a filesystem mount property, not a complete authorization boundary
for the Docker API. The API reveals sensitive information about containers,
images, networks, labels, mounts, and host configuration. A vulnerability in
Traefik or its dependencies could expose that metadata and create a serious
trust problem.

Only run reviewed Traefik versions on machines where every user able to modify
Docker container labels or images is already trusted. A restricted Docker
socket proxy is a possible future hardening layer, but v1 does not promise one.

## Package trust

A CLI package that starts Compose is executable host-level instruction. The
bundled Compose file can request privileged containers, Docker socket access,
arbitrary host bind mounts, and published ports.

Before first use or an upgrade:

1. Read the release notes.
2. Inspect the matching `compose.yaml` and repository history.
3. Confirm the package belongs to the expected project.
4. Use a reviewed package version when stronger immutability is required.

For example:

```sh
uvx localhost@1.0.0
```

An unpinned `uvx localhost` invocation may reuse a cached release. Use
`uvx --refresh localhost` when you intentionally want the newest
published release. Dependency update pull requests should be reviewed and pass
the integration suite before release.

## Application responsibility

Opting into Traefik makes a container reachable from local browsers through the
proxy. Applications remain responsible for trusted hosts, CSRF, CORS, callback
URLs, authentication, cookies, and safe handling of development data.

The shared Docker network also permits network connections between attached
containers. Do not attach sensitive or untrusted workloads casually. Keep
databases and internal dependencies only on application-private networks unless
they specifically need the shared network.

## HTTPS trust

HTTPS is an explicit local-development opt-in. `localhost trust` asks mkcert to
install one public development root into the system and NSS stores; it prints
the root fingerprint and explains the scope before the operating system asks
for authorization. The private root and intermediate signing keys are never
passed to mkcert or written to the host state directory.

The command keeps HTTP available when trust setup cannot complete. `localhost
trust --remove` first disables the HTTPS listener, then removes the exact root
selected by its fingerprint. Browser trust anchors are powerful: enable this
only on a machine where you trust the installed package and its local Docker
users.

## Out of scope for v1

V1 does not include public ACME, non-`.localhost` certificates, authentication
for the dashboard, or a restricted socket proxy. The CLI's scaffolding is
limited to local Compose integration.

The optional host bridge uses a pinned Caddy image and connects to
`host.docker.internal`. A host application must listen on a Docker-reachable
interface; binding it to `0.0.0.0` may also expose that application port to the
LAN. Prefer a Docker-specific host interface where available and use a host
firewall on untrusted networks.

The foreground `run` command executes detected Django runners and Vite package
scripts with the checkout user's normal host permissions. Review application
scripts as you would when running them directly.

Managed host runs store their checkout path in a Docker label so the proxy's
route listing can identify their location. Anyone with Docker inspection access
can read that label; do not use a sensitive checkout path.

Broader features require separate designs and threat analysis rather than ad
hoc production adaptation of this local configuration.
