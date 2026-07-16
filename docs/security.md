# Security and trust

This project is designed for a trusted local development machine. It is not a
production security design and should not be exposed to a LAN or the internet.

## Current safeguards

The v1 configuration reduces accidental exposure by:

- publishing HTTP only on `127.0.0.1`;
- leaving Traefik's insecure API mode disabled;
- serving the dashboard through `api@internal` on the loopback-bound `web`
  entrypoint;
- setting `exposedByDefault=false` and requiring `traefik.enable=true`;
- selecting the fixed `local-dev-proxy` network for backend traffic;
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

## Remote Compose trust

A remote Compose file is executable host-level instruction. It can request
privileged containers, Docker socket access, arbitrary host bind mounts, and
published ports.

Before first use or an upgrade:

1. Read the release notes.
2. Inspect the exact tagged `compose.yaml` and repository history.
3. Confirm the tag or commit belongs to the expected repository.
4. Use an exact release tag, or a reviewed commit SHA for stronger immutability.

For example:

```sh
curl --fail --location \
  https://raw.githubusercontent.com/SmileyChris/local-dev-proxy/v1.0.0/compose.yaml
```

Never use `main` or `latest` for a machine-wide shared proxy. Dependency update
pull requests should be reviewed and pass the integration suite before release.

## Application responsibility

Opting into Traefik makes a container reachable from local browsers through the
proxy. Applications remain responsible for trusted hosts, CSRF, CORS, callback
URLs, authentication, cookies, and safe handling of development data.

The shared Docker network also permits network connections between attached
containers. Do not attach sensitive or untrusted workloads casually. Keep
databases and internal dependencies only on application-private networks unless
they specifically need the shared network.

## Out of scope for v1

V1 does not include HTTPS, local certificate management, authentication for the
dashboard, a general project generator, or a restricted socket proxy. The CLI's
scaffolding is limited to local Compose integration.

The optional host bridge uses a pinned Caddy image and connects to
`host.docker.internal`. A host application must listen on a Docker-reachable
interface; binding it to `0.0.0.0` may also expose that application port to the
LAN. Prefer a Docker-specific host interface where available and use a host
firewall on untrusted networks.

Broader features require separate designs and threat analysis rather than ad
hoc production adaptation of this local configuration.
