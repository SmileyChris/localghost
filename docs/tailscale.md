# Tailnet hosting

Localghost can expose every opted-in local route to devices in one Tailscale
tailnet. If the local route is `https://shop.localhost`, enabling the suffix
`tail1234` adds `https://shop.tail1234`; `api.shop.localhost` becomes
`api.shop.tail1234`, and the dashboard becomes `traefik.tail1234`. There is no route at
the bare `tail1234` name.

This is an opt-in extension of the local hub. The normal `.localhost` routes
remain loopback-only and keep working when tailnet hosting is disabled.

## How the pieces fit

Four pieces cooperate, and each has exactly one job:

- **The hub** is the `localghost` Docker Compose project: Traefik plus its
  certificate providers. Docker labels on your containers are the single
  routing source of truth — applications never acquire tailnet-specific
  labels, so localhost and tailnet routing cannot drift apart.
- **The gateway** is one extra container in that same hub. It appears three
  ways that all name the same thing: Compose service `tailscale-gateway`,
  tailnet device `localghost-<suffix>` (what the admin console shows), and
  "the gateway" in this documentation. It joins the tailnet as a userspace
  node and only transports: it answers DNS for the suffix with its own
  tailnet addresses, forwards HTTP to Traefik with the original Host header,
  and passes HTTPS through as raw TCP. It terminates no TLS, holds no offline
  CA material, and has no Docker socket.
- **Two certificate authorities** live in Docker volumes, one for
  `.localhost` and one per tailnet suffix, each split into an offline root
  and a constrained online signer. Traefik's provider plugin — one instance
  per suffix — watches the same Docker labels and issues leaf certificates
  from the matching signer. Because the authorities are separate, trusting a
  tailnet root never widens what the `.localhost` root may sign, and vice
  versa.
- **Split DNS** is the only piece outside your machine: a per-suffix entry in
  the tailnet's DNS configuration that sends `*.<suffix>` lookups to the
  gateway. Enable adds it, disable restores exactly what was there before.

A request from another device therefore flows: tailnet DNS → gateway →
Traefik (certificate chosen by SNI from the suffix authority) → the same
container or host bridge that serves the `.localhost` route.

## Enable it

```sh
localghost tailscale enable
```

Without a credential, enable walks through the one-time admin-console setup
before prompting: allow the device tag (default `tag:localghost`) in the
tailnet [policy file](https://login.tailscale.com/admin/acls/file), for
example

```json
"tagOwners": {"tag:localghost": ["autogroup:admin"]}
```

and create a scoped
[OAuth client](https://tailscale.com/docs/features/oauth-clients) with the
`auth_keys` scope (with that tag allowed) and the `dns:write` scope. Paste the
client id and secret at the prompts, or provide them with
`TAILSCALE_CLIENT_ID`/`TAILSCALE_CLIENT_SECRET` or
`--client-id`/`--client-secret`. When a step was missed, the API errors are
translated into the exact console fix instead of raw HTTP responses.

After a successful enable, the credential is saved in the system keyring
(service `localghost-tailscale`), so `disable` and a later re-enable need no
re-entry; `disable` removes it again. Without a usable keyring, localghost
warns and stores nothing.

The OAuth client's own tailnet is the default. Localghost asks the installed
`tailscale` CLI for a suffix: an explicit one-label search domain wins, then
the tailnet's own MagicDNS label (`taildc3ac3.ts.net` becomes `taildc3ac3`),
and only as a last resort the current machine's name. Pass `--tailnet` or
`--suffix` explicitly when those defaults are not the desired values.

Because the detected suffix is shared by the whole tailnet, enable refuses to
replace an existing split-DNS entry for it — most likely another machine
already hosting that suffix. Choose a distinct `--suffix` per hosting
machine, or pass `--takeover` to replace the mapping deliberately. When a
later enable step fails, the split-DNS entry and saved state are rolled back
so the command can simply be run again.

A suffix that names a public TLD or reserved zone (`dev`, `com`, any
two-letter country code, `internal`, …) is refused outright: even though the
root is name-constrained, an anchor scoped to a real TLD could impersonate
real websites on every machine that trusts it.

Enable performs four bounded operations:

1. bootstraps the localhost and tailnet HTTPS authorities and builds the
   gateway image;
2. creates a single-use, ten-minute auth key — after the build, so a slow
   first build cannot outlive it — and enrolls a persistent tagged
   `localghost-tail1234` userspace node;
3. adds [split DNS](https://tailscale.com/docs/reference/dns-in-tailscale) for
   `tail1234`, preserving the previous suffix mapping; and
4. starts the gateway and suffix-specific HTTPS provider with the hub.

The OAuth access token and auth key are never saved, and the client secret is
kept only in the system keyring. Localghost stores the tailnet name, suffix,
gateway addresses, device tag, and previous split-DNS map in its state
directory so it can remove its split-DNS entry on disable without discarding
unrelated changes made later.

Every client must trust this hub's development roots once:

```sh
localghost trust
```

When tailnet hosting is enabled, the standard trust command installs both the
`.localhost` root and the active tailnet root. It downloads the latter from
`http://trust.tail1234` over the tailnet. Private CA keys never leave Docker volumes.

On another device, name the suffix instead:

```sh
localghost tailscale trust tail1234
```

That download is plain HTTP, and the tailnet is what secures it: split DNS
resolves the name to the tagged gateway, and the connection to it is
WireGuard-encrypted and gated by tailnet policy. Whoever can rewrite the
tailnet's DNS configuration can therefore answer for `trust.tail1234`. To close
that gap, pin the fingerprint: a successful enable — and `localghost
tailscale status` afterwards — prints a ready-to-paste command for other
machines, and the download is installed only if it matches:

```sh
localghost tailscale trust tail1234 --fingerprint SHA256:1A2B…
```

Devices without the localghost CLI (a phone, a colleague's untooled laptop)
can open `http://trust.tail1234` in a browser instead: the gateway serves a
small page with the pinned command, the expected fingerprint, and a direct
link to the root certificate for manual installation.

Trusting a root again replaces the one this client had for that suffix; the
superseded certificate leaves the trust stores first, and the `.localhost`
root is never disturbed.

## Operate and disable it

Normal commands continue to own the hub:

```sh
localghost tailscale status
localghost                 # reconciles localhost and tailnet hosting
localghost hub down        # stops both
```

A foreground `localghost run` pins both URLs to the bottom of the terminal —
`https://shop.localhost · https://shop.tail1234` — so the tailnet address stays
visible for other devices while the application runs.

The gateway carries a Docker healthcheck that reports healthy only once its
tailnet node is enrolled and every listener is up. `localghost tailscale
status` includes that observed state as `Gateway health`, so a gateway that
died after enable shows up there rather than as a timeout on another device.
Routers that cannot be mirrored (custom labels without explicit service and
entrypoints) appear there as `Localhost-only routers`.

An unreachable tailnet never blocks local work: when the hub starts but only
the gateway fails its healthcheck, `localghost` warns and carries on with the
`.localhost` routes while the gateway keeps retrying in the background.

To restore the DNS configuration that existed at enable time and remove the
gateway from the running hub:

```sh
localghost tailscale disable
```

Disabling deliberately does not delete the offline machine record. Remove the
tagged `localghost-<suffix>` device from the Tailscale admin console after you
have confirmed it is the expected node. Trust installed on other clients is
also left in place; run `localghost trust remove` on each to revoke it.

While tailnet hosting is enabled, `localghost trust remove` on the hosting
machine removes local trust but the hub keeps serving HTTPS — tailnet TLS
terminates on Traefik — so a full downgrade needs `localghost tailscale
disable` first.

## Scope and limitations

The first implementation supports one Tailscale tailnet and one suffix. It is
a mesh-only path: the gateway is reachable only through Tailscale, with no
public ingress, Funnel, Serve, or LAN listener. Anyone authorized to reach the
tagged gateway can reach the development applications it routes, so apply a
tailnet ACL or grant appropriate for your development group and data.

This feature mirrors Docker-label routes generated by localghost. Custom
routers must use literal Traefik `Host` rules for `.localhost` plus explicit service and
entrypoint labels to be mirrored safely; unsupported rules remain localhost
only and are reported by the provider.
