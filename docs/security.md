# local<span class="brand-accent">ghost</span> security and trust

This project is designed for a trusted local development machine. It is not a
production security design and should not be exposed to a LAN or the internet.

## Current safeguards

The shipped configuration reduces accidental exposure by:

- publishing HTTP only on `127.0.0.1`;
- leaving Traefik's insecure API mode disabled;
- serving the dashboard through `api@internal` on the loopback-bound `web`
  entrypoint;
- setting `exposedByDefault=false` and requiring `traefik.enable=true`;
- selecting the fixed `localghost` network for backend traffic;
- pinning Traefik to an exact image version; and
- disabling anonymous usage reporting and automatic version checks.

These controls limit network exposure and accidental routing. They do not make
the Docker API or an untrusted container safe.

## Docker socket access

Traefik needs Docker metadata and events for label-based discovery. Localghost mounts
`/var/run/docker.sock` read-only into the Traefik container.

Read-only is a filesystem mount property, not a complete authorization boundary
for the Docker API. The API reveals sensitive information about containers,
images, networks, labels, mounts, and host configuration. A vulnerability in
Traefik or its dependencies could expose that metadata and create a serious
trust problem.

Only run reviewed Traefik versions on machines where every user able to modify
Docker container labels or images is already trusted. A restricted Docker
socket proxy is a possible future hardening layer, but localghost does not promise one.

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
uvx localghost@1.0.0
```

An unpinned `uvx localghost` invocation may reuse a cached release. Use
`uvx --refresh localghost` when you intentionally want the newest
published release. Dependency update pull requests should be reviewed and pass
the integration suite before release.

## Application responsibility

Opting into Traefik makes a container reachable from local browsers through the
hub. Applications remain responsible for trusted hosts, CSRF, CORS, callback
URLs, authentication, cookies, and safe handling of development data.

The shared Docker network also permits network connections between attached
containers. Do not attach sensitive or untrusted workloads casually. Keep
databases and internal dependencies only on application-private networks unless
they specifically need the shared network.

## HTTPS trust

HTTPS is an explicit local-development opt-in. `localghost trust install` asks
mkcert to install one public development root into the system and NSS stores;
it prints the root fingerprint and explains the scope before the operating
system asks for authorization. The private root and intermediate signing keys
are never passed to mkcert or written to the host state directory.

The command keeps HTTP available when trust setup cannot complete. `localghost
trust remove` first disables the HTTPS listener, then removes the exact root
selected by its fingerprint. Browser trust anchors are powerful: enable this
only on a machine where you trust the installed package and its local Docker
users.

Removing host trust does not delete the private CA Docker volumes or the public
root copy in Localghost's host state directory. This is intentional so a later
opt-in can reuse the same root, but it means `localghost hub down` and
`localghost trust remove` are not complete data removal. Follow the
[complete-removal procedure](operations.md#stop-and-remove) after removing the
public root from host trust stores.

## Out of scope

Localghost does not include public ACME, non-`.localhost` certificates, authentication
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

Managed host runs store their checkout path in a Docker label so the hub's
route listing can identify their location. Anyone with Docker inspection access
can read that label; do not use a sensitive checkout path.

Broader features require separate designs and threat analysis rather than ad
hoc production adaptation of this local configuration.

## Tailnet hosting

Tailnet hosting intentionally expands exposure from one machine to authorized
tailnet devices. The gateway runs Tailscale's userspace networking stack and
does not publish a LAN or public host port. It does not use Tailscale Funnel or
Serve. Tailnet policy must restrict access to the gateway's device tag when the
whole tailnet should not reach development applications.

The enable command exchanges a scoped OAuth client credential for a temporary
access token, creates a single-use short-lived device auth key, and updates
split DNS. The access token and auth key are held only in process memory. The
client id and secret are saved in the operating system keyring — never in
localghost state files — so disable can restore DNS without re-entry, and
disable deletes them again. The prior split-DNS map is saved because
disable can restore the suffix's prior value. The API update is a domain-scoped
PATCH, so unrelated DNS mappings are untouched. Treat the saved map as
administrative metadata, even though it contains no secret.

The tailnet suffix has its own CA and signer volumes. Each participating client
explicitly installs that public root with `localghost trust` while tailnet
hosting is enabled.
This grants the development hub authority for names under that suffix on the
client, so a suffix naming a public TLD or reserved zone (`dev`, `com`, any
two-letter country code, `internal`, …) is refused at validation — a root
scoped to a real TLD could impersonate real websites on every client — and it
should not double as a general organizational DNS suffix either.

A tailnet root is itself name-constrained to its suffix, not only the online
signer beneath it, so the anchor a colleague installs cannot vouch for any
other name even if the offline root key is stolen. The signer refuses to
operate from an unconstrained root posing as a tailnet authority; only the
`.localhost` authority accepts its legacy unconstrained roots, and that root
is installed only on the machine that hosts it.

Clients should pin the download: enable and `localghost tailscale status`
print a `--fingerprint` trust command that installs the root only when it
matches, which removes the residual trust in tailnet DNS configuration.

Trusting a tailnet root replaces any earlier root for the same suffix: the
superseded certificate is removed from this client's trust stores before the
new one is written, because both mkcert and certutil identify an installed
authority by the certificate they are handed. When a store refuses the
removal, localghost reports which one, and the superseded root must be removed
with that client's own trust-store tooling.
