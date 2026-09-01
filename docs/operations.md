# Operating local<span class="brand-accent">ghost</span>

Application commands must not include the hub Compose file. The hub and
applications are intentionally separate lifecycle domains.

## Start or reconcile

```sh
uvx localghost hub up
```

The command runs the Compose configuration bundled with the CLI. It is
idempotent: running it again reconciles the existing `localghost` Compose
project rather than creating another hub, and waits for Traefik to become
healthy.

To inspect the current hub state and routes without starting or reconciling
it, run:

```sh
uvx localghost status
```

## Optional trusted HTTPS

The hub begins HTTP-only. Trusted HTTPS requires `mkcert` on the host. In an
interactive terminal, `localghost trust install` explains that a sudo password
may be requested because the public development root is being added to the
system trust store. In an
interactive terminal with `mkcert` installed, the first start offers to enable
HTTPS and names the public root fingerprint before any privilege prompt
appears. Otherwise, the successful HTTP startup ends by showing the command to
enable HTTPS after installing `mkcert`. The explicit equivalent is:

```sh
uvx localghost trust install
```

`trust install` runs `mkcert` with `TRUST_STORES=system,nss` and a `CAROOT`
containing only the hub's exported `rootCA.pem`. The private root and the online
intermediate remain in Docker volumes. It also imports the exact public root
into detected Zen NSS profiles, because Zen is not reliably discovered by
mkcert. A missing `mkcert`, declined authorization, or failed verification
leaves HTTPS unpublished and HTTP working.

When the hub is already running, a successful trust change reconciles it to
the corresponding HTTP or HTTPS configuration. Neither `trust install` nor
`trust remove` starts a stopped hub.

Check the state without modifying a trust store:

```sh
uvx localghost trust status
```

To disable the HTTPS listener and remove only this root from the stores managed
by the command:

```sh
uvx localghost trust remove
```

Restart browsers after trust changes when their NSS implementation requires it.
Leaf certificates are issued and renewed by the bundled Traefik local provider;
renewal does not invoke `sudo`, change the root, or require browser action.

Generated routes include matching `web` and `websecure` routers. Hand-written
application labels need the secure router explicitly; see
[Optional HTTPS for integrations](integrating-applications.md#optional-https).

## Inspect status and logs

```sh
docker ps --filter label=com.docker.compose.project=localghost
```

The Traefik container should report `healthy`. Follow its logs with:

```sh
uvx localghost hub logs -f
```

If the CLI cannot reach Docker, the raw fallback is
`docker logs -f localghost-traefik-1`.

The dashboard at `http://traefik.localhost` shows discovered routers, services,
and middleware. It is useful for confirming label discovery, but it does not
replace application logs when a backend itself is failing.

## Ghost pages

Nothing is ever hosted at bare `localhost`, so
[http://localhost](http://localhost) serves the hub's welcome page: the
logo, the list of remembered projects, and links to the Traefik dashboard
and this documentation. The page is styled to match these docs.

The hub remembers every project it has routed. Visiting a remembered
hostname whose application is stopped returns **503 Service Unavailable**
with a page naming the project, its directory, when it last started, and
the command to start it again. Hostnames the hub has never routed return
**404 Not Found** with a page listing the remembered projects.

Bring a remembered project back from anywhere — no need to find its
directory first:

```sh
uvx localghost summon <name>
```

Bare `uvx localghost summon` on a terminal opens an interactive picker:
arrow keys (or `j`/`k`) move, Enter summons the selected project, Delete
(or Backspace) forgets it, `u` undoes the last forget, and `q` leaves.
When output is piped it prints a plain listing instead. The commands on a ghost page are click-to-copy.

Entries are JSON files under the state directory's `registry/` folder,
written on every `run` and `save` and mounted read-only into the hub.
Remove one with:

```sh
uvx localghost forget <name>
```

or clear them all with `uvx localghost forget --all`. Status codes are
unchanged from a hub without ghost pages, so scripts and health checks
keep working; only response bodies differ.

On an HTTPS hub, remembered hostnames keep their certificates after their
applications stop, so ghost pages serve over `https://` without browser
warnings. Hostnames the hub has never routed still present Traefik's
default self-signed certificate.

A hub started manually from the repository's self-contained `compose.yaml`
carries no registry mount, so it serves only the generic not-found page.
The first CLI command that reconciles such a hub recreates its container
once to add the mount; after that the container is stable across runs.

## Shell completion

Enable Click's standard completion script for your shell so `summon` and
`forget` can complete remembered project names. For zsh:

```sh
eval "$(_LOCALGHOST_COMPLETE=zsh_source localghost)"
```

Add that line to your shell startup file to keep completion enabled in future
sessions. Replace `zsh_source` with `bash_source` or `fish_source` for those
shells.

## Stop and remove

```sh
uvx localghost hub down
```

Compose removes the hub container, the one-shot `bootstrap` container that mints
the CA, and attempts to remove the network. Docker will retain the network if
running consumer containers still have endpoints on it. Stop those applications
before removing the shared network completely.

The `bootstrap` container sits behind a Compose profile, so `down` passes
`--profile bootstrap` to reach it. Without that it stayed behind after `down`,
and Docker went on reporting the `localghost` project as existing.

`down` deliberately preserves the `localghost_localghost-ca-root` and
`localghost_localghost-ca-signer` Docker volumes. This keeps the same trusted
root available when the hub is restarted, avoiding another host trust-store
change. `trust remove` disables HTTPS and removes the public root from the
managed host stores, but leaves those private Docker volumes and the public
`rootCA.pem` copy in Localghost's state directory available for an intentional
re-enable.

For complete removal, remove host trust first, stop the hub, then delete the
two CA volumes:

```sh
uvx localghost trust remove
uvx localghost hub down
docker volume rm \
  localghost_localghost-ca-root \
  localghost_localghost-ca-signer
```

Finally, delete the Localghost state directory if no other state has been added
there. It is `LOCALGHOST_STATE_DIR` when that override is set, otherwise
`${XDG_STATE_HOME:-$HOME/.local/state}/localghost`. The retained `rootCA.pem` is
public, but removing it completes the local cleanup.

Deleting the CA volumes is irreversible. A later `localghost trust install`
creates a new root and requires that new public root to be installed. If Docker
reports a volume is in use, stop remaining `localghost` project containers
before retrying; do not force-remove a volume from a running hub.

Running `docker compose down` inside an application checkout affects only that
application and leaves the hub running.

## Upgrade

The ordinary command may reuse a cached CLI release. To fetch the newest
published release and reconcile the hub when you choose, run:

```sh
uvx --refresh localghost hub up
```

The top-level project name and shared network name are fixed, so the new bundled
configuration updates the existing hub. Consumer containers belong to other
Compose projects and are not recreated or restarted.

When stronger source immutability is required, use a reviewed package version,
such as `uvx localghost@1.0.0`.

## Use another HTTP port

If loopback port 80 is occupied, consistently prefix every lifecycle command
with the same override:

```sh
LOCALGHOST_HTTP_PORT=8080 uvx localghost hub up
```

The hub still binds only to `127.0.0.1`. URLs include the selected port:

```text
http://my-project.localhost:8080
http://traefik.localhost:8080
```

Framework origin allowlists must include the non-default port. Use the same
environment prefix whenever you reconcile the hub.

When HTTPS is enabled, `LOCALGHOST_HTTPS_PORT` similarly changes its loopback
port (default `443`). Use the matching `https://` URL and allowlist that port.

## Inspect the local checkout

Contributors working from a clone can validate the resolved configuration:

```sh
LOCALGHOST_IMAGE_TAG="v$(uv run localghost --version)" docker compose -f compose.yaml config
```

To confirm the actual host binding of a running local checkout:

```sh
container_id=$(LOCALGHOST_IMAGE_TAG="v$(uv run localghost --version)" docker compose -f compose.yaml ps -q traefik)
docker port "$container_id" 80/tcp
```

The result should contain only `127.0.0.1:<port>`. There should be no published
mapping for container port 8080.
