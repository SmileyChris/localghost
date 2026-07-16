# Troubleshooting

Start with the proxy status and logs using the exact tag selected for your
machine:

```sh
docker compose \
  -f https://github.com/SmileyChris/local-dev-proxy.git@v1.0.0 \
  ps
docker compose \
  -f https://github.com/SmileyChris/local-dev-proxy.git@v1.0.0 \
  logs --tail=100
```

## External network not found

Typical error:

```text
network local-dev-proxy declared as external, but could not be found
```

The proxy has not yet created its shared network. Run the tagged proxy `up -d`
command once, then rerun the application's `docker compose up` command.

Do not change the application network to a normal, implicitly created network.
That would create a project-scoped network that Traefik cannot share reliably.

## Network exists but every route is unavailable

Consumer containers can run while the proxy container is stopped. Check that
the proxy is running and healthy with `ps`, then inspect its logs. Reconcile it
with the tagged `up -d` command if needed.

Also confirm the URL uses the configured `LOCAL_DEV_PROXY_HTTP_PORT` when it is
not 80.

## Route returns 404

A Traefik `404 page not found` usually means no router matched the request.
Confirm that:

- the project name rendered by Compose is unique and DNS-safe;
- the container has `traefik.enable=true`;
- it is attached to the external `local-dev-proxy` network;
- `traefik.docker.network=local-dev-proxy` is present;
- the router name is unique; and
- the `Host(...)` rule exactly matches the browser hostname.

Inspect resolved labels rather than only the source file:

```sh
docker compose config
docker inspect "$(docker compose ps -q web)" --format '{{json .Config.Labels}}'
```

The dashboard at `http://traefik.localhost` should list the expected router.
An unlabelled container intentionally produces a 404.

## Route returns 502

A 502 normally means the router matched but Traefik could not reach a valid
backend. Confirm that:

- the application listens on `0.0.0.0`, not container loopback;
- the load-balancer label uses the application's container port;
- the process is actually listening on that port;
- the container is running; and
- both Traefik and the container are attached to `local-dev-proxy`.

Inspect network membership with:

```sh
docker network inspect local-dev-proxy
```

Application logs usually reveal crashes or bind-address mistakes:

```sh
docker compose logs --tail=100 web
```

## Port 80 is already allocated

Identify Docker containers already publishing the port:

```sh
docker ps --filter publish=80
```

Stop the conflicting listener if appropriate, or use
`LOCAL_DEV_PROXY_HTTP_PORT` consistently as described in
[Operating the proxy](operations.md#use-another-http-port). The proxy binds to
`127.0.0.1`, but a process bound to `0.0.0.0:80` still conflicts with it.

## Hostname resolution or HTTP proxy problems

`.localhost` is a special-use loopback domain, but local resolver or corporate
HTTP proxy settings can still interfere. Separate routing from resolution by
sending the Host header directly to loopback:

```sh
curl --noproxy '*' \
  --header 'Host: my-project.localhost' \
  http://127.0.0.1/
```

If this works but `http://my-project.localhost` does not, investigate the host
resolver, browser secure-DNS settings, VPN software, and `HTTP_PROXY`,
`HTTPS_PROXY`, or `NO_PROXY` environment variables.

For a non-default port, include it in the loopback URL and browser URL.

## Framework rejects an otherwise working route

An application-generated invalid-host, CSRF, CORS, or origin error is outside
Traefik routing. Add the generated hostname and origin to the framework's local
development settings. See [Framework configuration](integrating-applications.md#framework-configuration).
