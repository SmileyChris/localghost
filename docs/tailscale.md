# Tailnet hosting

Localghost can expose every opted-in local route to devices in one Tailscale
tailnet. If the local route is `https://shop.localhost`, enabling the suffix
`tail1234` adds `https://shop.tail1234`; `api.shop.localhost` becomes
`api.shop.tail1234`, and the dashboard becomes `traefik.tail1234`. The hub's
own page is `http://localghost.tail1234`: it lists the hub's projects under the
suffix and, for a private suffix, how to trust its HTTPS. There is no route at
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
  tailnet device `localghost-<machine>`, named after the hosting machine's
  own tailnet name (what the admin console shows), and "the gateway" in this
  documentation. It joins the tailnet as a userspace node and only
  transports: it answers DNS for the suffix with its own
  tailnet addresses, forwards HTTP to Traefik with the original Host header
  and the requesting device's tailnet address, and passes HTTPS through as
  raw TCP behind a PROXY protocol header naming that device. It also tells the
  hub which tailnet user owns an address, so applications receive
  [Tailscale identity headers](integrating-applications.md#tailnet-identity).
  It terminates no TLS, holds no offline CA material, and has no Docker
  socket.
- **Two certificate authorities** live in Docker volumes, one for
  `.localhost` and one per tailnet suffix, each split into an offline root
  and a constrained online signer. Traefik's provider plugin — one instance
  per suffix — watches the same Docker labels and issues leaf certificates
  from the matching signer. Because the authorities are separate, trusting a
  tailnet root never widens what the `.localhost` root may sign, and vice
  versa.
- **Split DNS** is the only piece outside your machine: a per-suffix entry in
  the tailnet's DNS configuration that sends `*.<suffix>` lookups to the
  gateway. Share adds it, unshare restores exactly what was there before.

A request from another device therefore flows: tailnet DNS → gateway →
Traefik (certificate chosen by SNI from the suffix authority) → the same
container or host bridge that serves the `.localhost` route.

## Share it

```sh
localghost tailscale share
```

Without a stored credential, share walks through the one-time admin-console
setup before prompting: allow the device tag (default `tag:localghost`) in the
tailnet [policy file](https://console.tailscale.com/admin/acls/file), for
example

```json
"tagOwners": {"tag:localghost": ["autogroup:admin"]}
```

(skipped when a device in the network map already carries the tag) and create a scoped
[OAuth client](https://tailscale.com/docs/features/oauth-clients) on the
[Trust credentials](https://console.tailscale.com/admin/settings/trust-credentials)
page with the scopes **DNS: Write** and **Auth Keys: Write** (with that tag
selected). Paste the
Client ID and Client secret at the prompts, or provide them with
`TAILSCALE_CLIENT_ID`/`TAILSCALE_CLIENT_SECRET` or
`--client-id`/`--client-secret`. When a step was missed, the API errors are
translated into the exact console fix instead of raw HTTP responses.

After a successful share, the credential is saved in the system keyring
(service `localghost-tailscale`), so `unshare` and a later share need no
re-entry. It stays there until `localghost tailscale unshare
--forget-credential` removes it, which also works once the hub is unshared.
That only forgets this machine's copy: revoke the OAuth client in the admin
console's Trust credentials page once no other machine uses it.
Without a usable keyring, localghost warns and stores nothing, and `unshare`
prompts for a client with the DNS: Write scope instead.

The OAuth client's own tailnet is the default. Localghost asks the installed
`tailscale` CLI for a suffix: an explicit one-label search domain wins, then
this machine's own tailnet short name (`wrk.taildc3ac3.ts.net` becomes `wrk`,
serving `shop.wrk`), and finally the tailnet's MagicDNS label (`taildc3ac3`)
when the short name is a public word. The short name keeps resolving to the
machine itself alongside the suffix. Pass `--tailnet` or `--suffix`
explicitly when those defaults are not the desired values.

A suffix's split-DNS entry applies to the whole tailnet, so share refuses to
replace an existing one — most likely another machine already hosting that
suffix. Machine short names keep hosting machines apart by default; pass a
distinct `--suffix`, or `--takeover` to replace the mapping deliberately. When a
later share step fails, the split-DNS entry and saved state are rolled back
so the command can simply be run again.

A suffix that names a public TLD or reserved zone (`dev`, `com`, `work`, any
two-letter country code, `internal`, …) still routes, but over HTTP only:
even though the root is name-constrained, an anchor scoped to a real TLD could
impersonate real websites on every machine that trusts it, so no tailnet
authority is minted for such a suffix. Requests are still encrypted by the
tailnet's WireGuard tunnel; what is lost is the browser's secure context
(service workers, `crypto.subtle`, camera access) and the trust details on
the `localghost.<suffix>` page. `share`, `hub up`, and `localghost tailscale status` say so, the
last in place of the trust command a private suffix shows. Detection never picks a public label; one has to
be passed as `--suffix` deliberately. Choose a private label such as
`tail1234` for trusted HTTPS.

Share performs four bounded operations:

1. bootstraps the localhost and tailnet HTTPS authorities and builds the
   gateway image;
2. creates a single-use, ten-minute auth key — after the build, so a slow
   first build cannot outlive it — and enrolls a persistent tagged
   `localghost-<machine>` userspace node, named after this machine's tailnet
   name (its hostname when the `tailscale` CLI is unavailable);
3. adds [split DNS](https://tailscale.com/docs/reference/dns-in-tailscale) for
   `tail1234`, preserving the previous suffix mapping; and
4. starts the gateway and suffix-specific HTTPS provider with the hub.

The OAuth access token and auth key are never saved, and the client secret is
kept only in the system keyring. Localghost stores the tailnet name, suffix,
gateway addresses, device tag, and previous split-DNS map in its state
directory so it can remove its split-DNS entry on unshare without discarding
unrelated changes made later.

Every client must trust this hub's development roots once:

```sh
localghost trust install
```

When the hub is shared, the standard trust command installs both the
`.localhost` root and the active tailnet root. It downloads the latter from
`http://localghost.tail1234` over the tailnet. Private CA keys never leave Docker volumes.

On another device, name the hub — by its suffix, its machine, or its
gateway device:

```sh
uvx localghost tailscale trust tail1234
```

That finds the hub without DNS. The local Tailscale client's network map,
which the coordination server signs off on, lists each online device tagged
`tag:localghost` with its tailnet addresses; localghost asks each gateway at
its address which suffix and root it serves, and WireGuard guarantees which
device answered. Split DNS, which anyone who can edit the tailnet's DNS
settings could point anywhere, plays no part. The setup output names the
device it trusted. With no name and a single hub online, that hub is trusted.

A suffix is only what a gateway claims, so when two hubs claim the same one
the command refuses to choose and lists them; name the device instead
(`localghost-<machine>`), which MagicDNS keeps unique across the tailnet.
What this trusts in the end is the tailnet itself: faking a hub takes control
of its gateway device, or the admin rights to replace it.

Without the tailscale CLI there is no network map to ask, so the command
falls back to downloading from `http://localghost.tail1234` by DNS and warns
that it did. Pin the fingerprint there: a successful share — and
`localghost tailscale status` afterwards — prints it beneath the trust
command, and the download is installed only if it matches:

```sh
uvx localghost tailscale trust tail1234 --fingerprint SHA256:1A2B…
```

Devices without the localghost CLI (a phone, a colleague's untooled laptop)
can open `http://localghost.tail1234` in a browser instead: the hub's page
shows the trust command, the expected fingerprint, and a direct link to the
root certificate for manual installation. That page arrives over the same
DNS, so check the fingerprint against the one the hub's owner reports. Read
over the tailnet, it lists only running projects, linked under the suffix and
without their local folders; stopped projects stay this machine's business.
The older `http://trust.tail1234` is deprecated: it still serves the root to
earlier CLIs and redirects browsers to the hub's page.

Trusting a root again replaces the one this client had for that suffix; the
superseded certificate leaves the trust stores first, and the `.localhost`
root is never disturbed.

## What applications receive

A tailnet request reaches the application like a local one, on the same
route, with two differences. The requesting device's tailnet address, such as
`100.101.102.103`, arrives in `X-Real-Ip` and `X-Forwarded-For` instead of
`127.0.0.1`. And the person behind the device arrives in the
`Tailscale-User-Login`, `Tailscale-User-Name` and `Tailscale-User-Profile-Pic`
headers Tailscale Serve would set, so an application can recognize a
teammate without a sign-in of its own. See
[Tailnet identity](integrating-applications.md#tailnet-identity) for the
values, tagged devices, and what to trust.

## Operate and unshare it

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
died after share shows up there rather than as a timeout on another device.
Routers that cannot be mirrored (custom labels without explicit service and
entrypoints) appear there as `Localhost-only routers`.

An unreachable tailnet never blocks local work: when the hub starts but only
the gateway fails its healthcheck, `localghost` warns and carries on with the
`.localhost` routes while the gateway keeps retrying in the background.

To restore the DNS configuration that existed at share time and remove the
gateway from the running hub:

```sh
localghost tailscale unshare
```

The OAuth credential stays in the system keyring, so sharing again needs no
setup; unshare says so and how to remove it with `--forget-credential`.
(`enable` and `disable` still work as deprecated names for `share` and
`unshare`.)

Unsharing deliberately does not delete the offline machine record. Remove the
tagged `localghost-<machine>` device from the
[Machines](https://console.tailscale.com/admin/machines) page after
you have confirmed it is the expected node. Trust installed on other clients
is also left in place; run `localghost trust remove` on each to revoke it.

### Switch to another suffix

Run share again with the new suffix while the hub is shared:

```sh
localghost tailscale share --suffix wrk
```

Without `--suffix`, share detects the suffix again, so after renaming this
machine in the admin console a plain `localghost tailscale share` moves the
routes to the new short name. That applies to a suffix chosen with
`--suffix` too; when detection gives the current suffix, share just reports
that the hub is already shared.

The gateway keeps its device, addresses, tailnet, and tag, so no auth key is
created and nothing new appears in the admin console. The new suffix's split
DNS entry points at the gateway, and in the same change the old suffix gets
back whatever it resolved to before sharing. The hub is recreated to answer
for the new names. The same `--takeover` rule applies to the new suffix, and
if the hub fails to start the switch is rolled back to the old suffix. The
stored credential is used and kept.

Each suffix has its own HTTPS root, because a root may only sign names under
its suffix. Other machines therefore run the printed `localghost tailscale
trust` command for the new suffix, and `localghost trust remove` if they
should stop trusting the old one.

A hub shared before gateways were named after the machine keeps its
`localghost-<suffix>` device across switches; unshare and share again to
enroll one named after the machine.

While the hub is shared, `localghost trust remove` on the hosting
machine removes local trust but the hub keeps serving HTTPS — tailnet TLS
terminates on Traefik — so a full downgrade needs `localghost tailscale
unshare` first.

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
