# Operating the proxy

Application commands must not include the proxy Compose file. The proxy and
applications are intentionally separate lifecycle domains.

## Start or reconcile

```sh
uvx local-dev-proxy
```

The command runs the Compose configuration bundled with the CLI. It is
idempotent: running it again reconciles the existing `local-dev-proxy` Compose
project rather than creating another proxy, and waits for Traefik to become
healthy.

## Inspect status and logs

```sh
docker ps --filter label=com.docker.compose.project=local-dev-proxy
```

The Traefik container should report `healthy`. Follow its logs with:

```sh
docker logs -f local-dev-proxy-traefik-1
```

The dashboard at `http://traefik.localhost` shows discovered routers, services,
and middleware. It is useful for confirming label discovery, but it does not
replace application logs when a backend itself is failing.

## Stop and remove

```sh
uvx local-dev-proxy down
```

Compose removes the proxy container and attempts to remove its network. Docker
will retain the network if running consumer containers still have endpoints on
it. Stop those applications before removing the shared network completely.

Running `docker compose down` inside an application checkout affects only that
application and leaves the proxy running.

## Upgrade

`uvx` uses its cached tool release by default. To fetch the latest published
CLI and reconcile the proxy, run:

```sh
uvx --refresh local-dev-proxy
```

The top-level project name and shared network name are fixed, so the new bundled
configuration updates the existing proxy. Consumer containers belong to other
Compose projects and are not recreated or restarted.

When stronger source immutability is required, use a reviewed package version,
such as `uvx local-dev-proxy@1.0.0`.

## Use another HTTP port

If loopback port 80 is occupied, consistently prefix every lifecycle command
with the same override:

```sh
LOCAL_DEV_PROXY_HTTP_PORT=8080 uvx local-dev-proxy
```

The proxy still binds only to `127.0.0.1`. URLs include the selected port:

```text
http://my-project.localhost:8080
http://traefik.localhost:8080
```

Framework origin allowlists must include the non-default port. Use the same
environment prefix whenever you reconcile the proxy.

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
