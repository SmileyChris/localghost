# Operating Localhost Proxy

Application commands must not include the proxy Compose file. The proxy and
applications are intentionally separate lifecycle domains.

## Start or reconcile

```sh
uvx localhost
```

The command runs the Compose configuration bundled with the CLI. It is
idempotent: running it again reconciles the existing `localhost` Compose
project rather than creating another proxy, and waits for Traefik to become
healthy.

To inspect the current proxy state and routes without starting or reconciling
it, run:

```sh
uvx localhost --status
```

## Optional trusted HTTPS

The proxy begins HTTP-only. In an interactive terminal, the first start offers
to enable HTTPS and names the public root fingerprint before any privilege
prompt appears. The explicit equivalent is:

```sh
uvx localhost trust
```

`trust` runs `mkcert` with `TRUST_STORES=system,nss` and a `CAROOT` containing
only this proxy's exported `rootCA.pem`. The private root and the online
intermediate remain in Docker volumes. It also imports the exact public root
into detected Zen NSS profiles, because Zen is not reliably discovered by
mkcert. A missing `mkcert`, declined authorization, or failed verification
leaves HTTPS unpublished and HTTP working.

When the proxy is already running, a successful trust change reconciles it to
the corresponding HTTP or HTTPS configuration. Neither `trust` nor `trust
--remove` starts a stopped proxy.

Check the state without modifying a trust store:

```sh
uvx localhost trust --status
```

To disable the HTTPS listener and remove only this root from the stores managed
by the command:

```sh
uvx localhost trust --remove
```

Restart browsers after trust changes when their NSS implementation requires it.
Leaf certificates are issued and renewed by the bundled Traefik local provider;
renewal does not invoke `sudo`, change the root, or require browser action.

## Inspect status and logs

```sh
docker ps --filter label=com.docker.compose.project=localhost
```

The Traefik container should report `healthy`. Follow its logs with:

```sh
docker logs -f localhost-traefik-1
```

The dashboard at `http://traefik.localhost` shows discovered routers, services,
and middleware. It is useful for confirming label discovery, but it does not
replace application logs when a backend itself is failing.

## Stop and remove

```sh
uvx localhost down
```

Compose removes the proxy container and attempts to remove its network. Docker
will retain the network if running consumer containers still have endpoints on
it. Stop those applications before removing the shared network completely.

Running `docker compose down` inside an application checkout affects only that
application and leaves the proxy running.

## Upgrade

The ordinary command may reuse a cached CLI release. To fetch the newest
published release and reconcile the proxy when you choose, run:

```sh
uvx --refresh localhost
```

The top-level project name and shared network name are fixed, so the new bundled
configuration updates the existing proxy. Consumer containers belong to other
Compose projects and are not recreated or restarted.

When stronger source immutability is required, use a reviewed package version,
such as `uvx localhost@1.0.0`.

## Use another HTTP port

If loopback port 80 is occupied, consistently prefix every lifecycle command
with the same override:

```sh
LOCALHOST_HTTP_PORT=8080 uvx localhost
```

The proxy still binds only to `127.0.0.1`. URLs include the selected port:

```text
http://my-project.localhost:8080
http://traefik.localhost:8080
```

Framework origin allowlists must include the non-default port. Use the same
environment prefix whenever you reconcile the proxy.

When HTTPS is enabled, `LOCALHOST_HTTPS_PORT` similarly changes its loopback
port (default `443`). Use the matching `https://` URL and allowlist that port.

## Inspect the local checkout

Contributors working from a clone can validate the resolved configuration:

```sh
docker compose -f compose.yaml config
```

To confirm the actual host binding of a running local checkout:

```sh
container_id=$(docker compose -f compose.yaml ps -q traefik)
docker port "$container_id" 80/tcp
```

The result should contain only `127.0.0.1:<port>`. There should be no published
mapping for container port 8080.
