# Architecture

Local Development Proxy separates a machine-wide concern from individual
application checkouts. One long-lived Traefik container owns the host HTTP port;
each application remains a separate Docker Compose project.

## Ownership boundaries

The proxy Compose project owns:

- the fixed Compose project name `local-dev-proxy`;
- one Traefik container;
- the fixed Docker network `local-dev-proxy`; and
- loopback publication of the configured HTTP port.

An application Compose project owns its containers, default network, data, and
application-specific configuration. It only references `local-dev-proxy` as an
external network. Application lifecycle commands must never include the proxy
Compose file, so stopping or rebuilding an application cannot recreate the
proxy.

Multiple applications—and multiple checkouts of the same application—can run
at once when every checkout has a unique Compose project name.

## Request path

For a request to `http://my-project.localhost`:

1. `.localhost` resolves to the local loopback interface.
2. Docker forwards the loopback-bound host port to Traefik's `web` entrypoint.
3. Traefik matches the request `Host` header against opted-in container labels.
4. Traefik connects to the selected container over the shared
   `local-dev-proxy` network and its explicitly labelled port.

No application host port is needed in this path. Application containers may
also retain their ordinary Compose default network for databases and other
private dependencies.

## Discovery and isolation

Traefik reads Docker metadata through a read-only bind mount of
`/var/run/docker.sock`. The Docker provider is configured with:

- `exposedByDefault=false`, so unlabelled containers receive no route;
- `network=local-dev-proxy`, so backend traffic uses the shared network; and
- explicit consumer labels for router rules and backend ports.

The network setting controls Traefik's connection path; it does not prevent
Traefik from seeing Docker metadata. See [Security and trust](security.md) for
that distinction.

## Names and hostnames

Compose automatically derives each application project name from its checkout
directory. That name forms part of public local hostnames and Traefik object
names, so it must be unique and contain only lowercase letters, digits, and
hyphens. An explicit override is needed only when the derived name does not meet
those conditions or is not the desired hostname.

| Role | Hostname | Router/service name pattern |
| --- | --- | --- |
| Primary service | `<project>.localhost` | `<project>-web` |
| Secondary service | `<service>.<project>.localhost` | `<project>-<service>` |
| Proxy dashboard | `traefik.localhost` | `local-dev-proxy-dashboard` |

These conventions make router and service identifiers unique across attached
Compose projects. `.localhost` is used because it is reserved for loopback and
does not require a wildcard DNS server.

## Dashboard and health

Traefik's internal API service provides the dashboard through the normal `web`
entrypoint. The bare `http://traefik.localhost` URL redirects to
`/dashboard/`. Port 8080, used by Traefik's insecure API mode, is neither enabled
nor published.

The Traefik container health check calls `traefik healthcheck --ping` against
the enabled internal ping endpoint. Health describes the proxy process; it does
not guarantee that every consumer application is healthy or correctly labelled.

## Host-native applications

The optional CLI can generate a small consumer Compose project for an HTTP
process running directly on the host. A pinned Caddy container joins the shared
network, carries the ordinary Traefik labels, and forwards requests to
`host.docker.internal`. This keeps host-specific routes out of the persistent
proxy configuration and gives the bridge an independent application lifecycle.

The host process must listen on an interface reachable from Docker. Binding only
to host loopback is generally insufficient.
