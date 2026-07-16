# Operating the proxy

Use the same exact tagged remote Compose file for every command. Examples use
`v1.0.0`; substitute the exact version you have reviewed and chosen.

Application commands must not include the proxy Compose file. The proxy and
applications are intentionally separate lifecycle domains.

## Start or reconcile

```sh
docker compose \
  -f https://github.com/SmileyChris/local-dev-proxy.git@v1.0.0 \
  up -d
```

The command is idempotent. Running it again reconciles the existing
`local-dev-proxy` Compose project rather than creating another proxy.

## Inspect status and logs

```sh
docker compose \
  -f https://github.com/SmileyChris/local-dev-proxy.git@v1.0.0 \
  ps
```

The Traefik container should report `healthy`. Follow its logs with:

```sh
docker compose \
  -f https://github.com/SmileyChris/local-dev-proxy.git@v1.0.0 \
  logs -f
```

The dashboard at `http://traefik.localhost` shows discovered routers, services,
and middleware. It is useful for confirming label discovery, but it does not
replace application logs when a backend itself is failing.

## Stop and remove

```sh
docker compose \
  -f https://github.com/SmileyChris/local-dev-proxy.git@v1.0.0 \
  down
```

Compose removes the proxy container and attempts to remove its network. Docker
will retain the network if running consumer containers still have endpoints on
it. Stop those applications before removing the shared network completely.

Running `docker compose down` inside an application checkout affects only that
application and leaves the proxy running.

## Upgrade

Review the release notes and the new tagged Compose file before changing
versions. Then pull and reconcile using only the new exact tag:

```sh
docker compose \
  -f https://github.com/SmileyChris/local-dev-proxy.git@v1.1.0 \
  pull
docker compose \
  -f https://github.com/SmileyChris/local-dev-proxy.git@v1.1.0 \
  up -d
```

The top-level project name and shared network name are fixed, so the new source
updates the existing proxy. Consumer containers belong to other Compose
projects and are not recreated or restarted.

When stronger source immutability is required, replace the release tag with a
reviewed commit SHA. Never operate the shared proxy from `main` or `latest`.

## Use another HTTP port

If loopback port 80 is occupied, consistently prefix every lifecycle command
with the same override:

```sh
LOCAL_DEV_PROXY_HTTP_PORT=8080 docker compose \
  -f https://github.com/SmileyChris/local-dev-proxy.git@v1.0.0 \
  up -d
```

The proxy still binds only to `127.0.0.1`. URLs include the selected port:

```text
http://my-project.localhost:8080
http://traefik.localhost:8080
```

Framework origin allowlists must include the non-default port. Apply the same
environment prefix to `ps`, `logs`, `pull`, `up`, and `down` so Compose always
evaluates an identical project definition.

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

