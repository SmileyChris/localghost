# Troubleshooting local<span class="brand-accent">ghost</span>

Start with the hub status and logs:

```sh
uvx localghost status
uvx localghost hub logs --tail 100
```

If the CLI cannot reach Docker, use the raw commands:

```sh
docker ps --filter label=com.docker.compose.project=localghost
docker logs --tail=100 localghost-traefik-1
```

## External network not found

Typical error:

```text
network localghost declared as external, but could not be found
```

The hub has not yet created its shared network. Run `uvx localghost hub up`
once, then rerun the application's `docker compose up` command.

Do not change the application network to a normal, implicitly created network.
That would create a project-scoped network that Traefik cannot share reliably.

## Network exists but every route is unavailable

Consumer containers can run while the hub container is stopped. Check that
the hub is running and healthy with `ps`, then inspect its logs. Reconcile it
with `uvx localghost hub up` if needed.

Also confirm the URL uses the configured `LOCALGHOST_HTTP_PORT` when it is
not 80.

## Route returns 404

A Traefik `404 page not found` usually means no router matched the request.
Confirm that:

- the project name rendered by Compose is unique and DNS-safe;
- the container has `traefik.enable=true`;
- it is attached to the external `localghost` network;
- `traefik.docker.network=localghost` is present;
- the router name is unique; and
- the `Host(...)` rule exactly matches the browser hostname.

Inspect resolved labels rather than only the source file:

```sh
docker compose config
docker inspect "$(docker compose ps -q web)" --format '{{json .Config.Labels}}'
```

The dashboard at `http://traefik.localhost` should list the expected router.
An unlabelled container intentionally produces a 404.

## A hostname shows "is offline" instead of my app

The hub remembers hostnames it has routed before. If the application behind
one is stopped or was never started this session, the hub answers with a
ghost page (`503 Service Unavailable`) instead of routing to it. Start the
application with `localghost run`, or drop the stale entry with
`localghost forget <name>` if the project is gone for good.

## Route returns 502

A 502 normally means the router matched but Traefik could not reach a valid
backend. Confirm that:

- the application listens on `0.0.0.0`, not container loopback;
- the load-balancer label uses the application's container port;
- the process is actually listening on that port;
- the container is running; and
- both Traefik and the container are attached to `localghost`.

Inspect network membership with:

```sh
docker network inspect localghost
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
`LOCALGHOST_HTTP_PORT` consistently as described in
[Operating the hub](operations.md#use-another-http-port). The hub binds to
`127.0.0.1`, but a process bound to `0.0.0.0:80` still conflicts with it.

## HTTP works but HTTPS does not

Inspect the managed trust and listener state first:

```sh
uvx localghost trust status
uvx localghost status
```

HTTPS requires `mkcert`, an installed Localghost public root, and a running hub
with the `websecure` entrypoint enabled. The application also needs a secure
router with `websecure`, its normal `Host(...)` rule and service, and `tls=true`.
Generated configurations include this router; compare hand-written labels with
[Optional HTTPS](integrating-applications.md#optional-https).

If a custom `LOCALGHOST_HTTPS_PORT` is configured, include it in the URL. When
only a browser rejects the certificate, restart it and recheck its system or NSS
trust store. When every client fails to connect, inspect the hub logs and host
port publication instead.

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

## Route already exists

`localghost run` refuses to replace an existing container route for the
same hostname. Stop the other application normally, or inspect the reported
container and remove a stale bridge explicitly:

```sh
docker rm -f <container>
```

The foreground command cleans up its bridge on exit, Ctrl+C, and SIGTERM. A
hard kill or Docker failure can still leave a stale container.

## Framework rejects an otherwise working route

An application-generated invalid-host, CSRF, CORS, or origin error is outside
Traefik routing. Add the generated hostname and origin to the framework's local
development settings. See [Framework configuration](integrating-applications.md#framework-configuration).

The most common shape of this is a Django project where browsing works and
every form post returns `403 Forbidden`: `GET` requests never consult
`CSRF_TRUSTED_ORIGINS`, so routing looks healthy until the first `POST`. A
related pair, when the hub serves HTTPS, is a login that silently never stays
logged in, or `http://` links generated inside an HTTPS page — both mean
Django has not been told to trust the hub's forwarded scheme. See
[Django](integrating-applications.md#django) for all three settings.

For `localghost run`, Django needs its generated `<name>.localhost` in
`ALLOWED_HOSTS` and, when applicable, CSRF trusted origins. Vite HTTP, HMR, and
WebSocket traffic use the same bridge, and pass through the loopback relay
unaltered when one is in use.

## The application is listening somewhere the hub cannot reach

```
the application is listening on 127.0.0.1:5173, which the hub cannot reach
```

The application bound loopback only, and no relay could be raised from the
Docker gateway address to stand in for a wider bind. Localghost stops the
application rather than leave it running behind a URL that cannot resolve.
Make it listen on all interfaces. For a Vite or Astro project the usual cause
is a `dev` script that wraps the tool and drops the `--host` localghost passes:
forward `"$@"` to the tool in the script, or name the command directly:

```sh
localghost run --port 5173 -- npx vite --host 0.0.0.0 --port 5173
```

See [Loopback-bound servers](running-host-apps.md#loopback-bound-servers).

## Host port is already in use

```
host port 5173 is already in use
```

Something else is listening on the port the run planned, so `run` refuses to
start rather than put its URL in front of whatever is already there. On Linux
the error names the process and its working directory. Stop that process, or
pass another port with `--port`. Without `--port`, the run walks to the next
free port and warns about the one it skipped; a dev server that then ignores
`--port` and comes up on its own default is the wrapper-script case described
in [Wrapper scripts](running-host-apps.md#wrapper-scripts).

## The OAuth credential was not stored

`localghost tailscale enable` warns when it cannot save the credential in the
operating system keyring. Everything else still works; the cost is that
`tailscale disable` (and a later re-enable) will prompt for the client id and
secret again, or read them from `TAILSCALE_CLIENT_ID` and
`TAILSCALE_CLIENT_SECRET`.

The usual cause is a headless or minimal Linux session with no Secret Service
provider on D-Bus. Install and unlock one — `gnome-keyring` or KWallet on a
desktop, or `keyring` alternatives such as `keyrings.alt` where a desktop
service is not an option — and re-run enable, or simply keep supplying the
environment variables. A stored credential lives under the keyring service
name `localghost-tailscale` and can be inspected or removed with the system's
own keyring tools; `localghost tailscale disable` also deletes it.

## The tailnet gateway is not ready

When the hub starts but only `tailscale-gateway` fails its healthcheck —
typically because the machine is offline or Tailscale is unreachable —
`localghost` warns and continues with the local `.localhost` routes. The
gateway container keeps retrying in the background; `localghost tailscale
status` shows its progress under `Gateway health`. No action is needed once
connectivity returns.

## Split DNS already points at another address

`localghost tailscale enable` refuses to replace an existing split-DNS entry
for the chosen suffix, naming the addresses it found. Usually another machine
is already hosting that suffix: pick a distinct `--suffix`, or pass
`--takeover` to replace the mapping deliberately — the other machine's routes
for that suffix stop resolving when you do.

## Hub start fails after pruning Docker volumes

Removing the `localghost-*-ca-*` volumes deletes the certificate signers that
Traefik's providers require. A failed HTTPS start now re-runs the idempotent
CA bootstraps once and retries automatically. If the offline root volume was
among those removed, the recreated tailnet authority is a new identity and
every client must run `localghost tailscale trust` again; the fingerprint in
`localghost tailscale status` shows whether it changed.

## Terminal only scrolls part of the screen

After a `localghost run` was killed outright — `kill -9`, a crashed terminal
emulator, an out-of-memory kill — the shell scrolls only its upper rows and a
stale Localghost status bar sits frozen on the last line.

The status bar reserves the bottom row by setting a terminal scrolling region
over the rows above it, and releases that region when the run ends. The
release cannot run when the process is killed without a chance to clean up:
ordinary exits, `Ctrl+C`, `SIGTERM`, and errors all restore the terminal, but
nothing survives `SIGKILL`. The terminal is not damaged; it is still holding
the region.

Release it:

```sh
printf '\033[r'
```

`reset` also clears it, at the cost of wiping the screen and its scrollback.
To avoid the region entirely, run with `--no-status-bar` as described in
[Running host applications](running-host-apps.md#the-status-bar).
