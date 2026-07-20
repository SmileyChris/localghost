# Integrating applications with Localghost

An application joins the proxy's external Docker network and describes its
routes using Traefik labels. The proxy and application remain separate Compose
projects.

## Prerequisites

Start the proxy before the first application. This creates the external
`localghost` network:

```sh
uvx localghost
```

Compose automatically derives the project name from the checkout directory and
makes it available to labels as `${COMPOSE_PROJECT_NAME}`. Nothing needs to be
configured when that name is unique and contains only lowercase letters,
digits, and hyphens.

Override it only when the derived name is unsafe, duplicated, or not the
hostname you want. For example, set it in `.env`:

```dotenv
COMPOSE_PROJECT_NAME=my-project
```

You can instead use the `COMPOSE_PROJECT_NAME` environment variable or
Compose's project-name option.

## Primary service

This example publishes a service listening on container port 8000 at
`http://my-project.localhost`:

```yaml
services:
  web:
    networks:
      - default
      - localghost
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network=localghost"
      - "traefik.http.routers.${COMPOSE_PROJECT_NAME}-web.entrypoints=web"
      - "traefik.http.routers.${COMPOSE_PROJECT_NAME}-web.rule=Host(`${COMPOSE_PROJECT_NAME}.localhost`)"
      - "traefik.http.routers.${COMPOSE_PROJECT_NAME}-web.service=${COMPOSE_PROJECT_NAME}-web"
      - "traefik.http.services.${COMPOSE_PROJECT_NAME}-web.loadbalancer.server.port=8000"

networks:
  localghost:
    external: true
```

The application process must listen on `0.0.0.0:8000`, not `127.0.0.1:8000`,
inside its container. The backend port label is a container port; it is not a
host-published port.

Do not add `ports` for a service used only through the proxy. Keeping the
service off host ports avoids collisions and keeps the proxy as the single HTTP
entrypoint. The service can remain on its `default` network for private
application dependencies.

The router's explicit `service` label avoids implicit association. The explicit
load-balancer port remains deterministic if the image or Compose definition
later exposes another port.

## Secondary services

Secondary web interfaces follow `<service>.<project>.localhost`. For a Mailpit
service listening on container port 8025:

```yaml
services:
  mailpit:
    image: axllent/mailpit:v1.27.7 # Choose and review the version used by your project.
    networks:
      - default
      - localghost
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network=localghost"
      - "traefik.http.routers.${COMPOSE_PROJECT_NAME}-mailpit.entrypoints=web"
      - "traefik.http.routers.${COMPOSE_PROJECT_NAME}-mailpit.rule=Host(`mailpit.${COMPOSE_PROJECT_NAME}.localhost`)"
      - "traefik.http.routers.${COMPOSE_PROJECT_NAME}-mailpit.service=${COMPOSE_PROJECT_NAME}-mailpit"
      - "traefik.http.services.${COMPOSE_PROJECT_NAME}-mailpit.loadbalancer.server.port=8025"

networks:
  localghost:
    external: true
```

For a checkout named `my-project`, the URL is
`http://mailpit.my-project.localhost`.

Repeat the pattern for other browser-facing tools. The service segment and the
router/service identifiers must be distinct within the project.

## Multiple checkouts

Two checkouts of the same repository can use the same Compose configuration:

```text
checkout                  primary URL
../my-project             http://my-project.localhost
../my-project-review-123  http://my-project-review-123.localhost
```

Compose project names also isolate container and default-network names. Stopping
one checkout leaves the other checkout and shared proxy running. Override a
name only if two checkout directories have the same basename or a basename is
not DNS-safe.

## Framework configuration

Traefik routing does not bypass application security checks. Configure generated
hostnames in trusted-host, origin, CORS, callback URL, and cookie settings as
required by the framework.

For Django, a checkout named `my-project` normally needs:

```python
ALLOWED_HOSTS = ["my-project.localhost"]
CSRF_TRUSTED_ORIGINS = ["http://my-project.localhost"]
```

When the proxy uses a non-default port, the browser origin includes it:

```python
CSRF_TRUSTED_ORIGINS = ["http://my-project.localhost:8080"]
```

Cookie domain settings often work best when left host-only in local development.
OAuth and other external callbacks must use the generated URL expected by the
browser.

## Failure behavior

Because `localghost` is declared external, application startup fails if
the proxy network has never been created. This is intentional: the application
must not silently create a private network with the same name. Start the proxy,
then rerun `docker compose up` for the application.

If the network exists but Traefik is stopped, application containers can start
and communicate on their other networks, but `.localhost` routes remain
unavailable until the proxy starts again.

Unlabelled containers and containers without `traefik.enable=true` receive no
route even when attached to the shared network.
