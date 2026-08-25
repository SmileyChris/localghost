# Running host applications with local<span class="brand-accent">ghost</span>

`localghost run` lets you serve an application behind the hub without a
Dockerfile or Compose file, or hand an existing Compose project to
`docker compose up` with the hub wired in first.

## Project types

Every project has a **type**. Detection runs automatically from the selected
directory; `--type` resolves ambiguity or skips detection entirely.

| Type | Detection | Default port |
|------|-----------|---------------|
| **compose** | `compose.yaml`, `compose.yml`, `docker-compose.yaml`, or `docker-compose.yml` | — (Compose owns it) |
| **django** | `manage.py` in the project directory | 8000 |
| **vite** | `package.json` with a `dev` script and `vite` dependency | 5173 |
| **astro** | `package.json` with a `dev` script and `astro` dependency | 4321 |
| **cakephp** | `bin/cake` with a `cakephp/cakephp` Composer dependency, or a legacy `app/Config/core.php` + `app/webroot/index.php` root | 8765 |
| **laravel** | `artisan` with a `laravel/framework` Composer dependency | 8000 |
| **php** | `composer.json`, or `index.php` in `public/`, `web/`, `htdocs/`, `www/`, or the project root | 8080 |

`dockerfile` is an eighth type, but it is generate-only — see
[Generating an override](generating-an-override.md). Running a Dockerfile
project first needs `localghost generate --type dockerfile` to produce a
Compose file; after that the project is `compose`.

Within PHP, `cakephp` and `laravel` are specializations of `php` rather than
peers: the most specific match wins, and generic `php` only fires when neither
framework matches. Every other type resolves to the nearest match along the
upward search.

If more than one type is detected at the same directory (for example, a
Django project that also has a `package.json` with Vite, or a directory
holding both a Compose file and a framework), use `--type` to resolve the
ambiguity:

```sh
uvx localghost run --type django
uvx localghost run --type compose
```

Localghost searches upward from the selected directory to the nearest project
root, stopping at the search boundary described below. This means it can be
run from directories such as `webroot`, `public`, or a nested source package
without naming the application after that directory. The detected root
supplies the default public name and application configuration; a framework
may still use a different working directory for its server process.

## Usage

Start the hub first (if it isn't already running), then run your app:

```sh
cd my-django-project
uvx localghost run
```

A `compose` type hands the project straight to `docker compose up` after
starting the hub. Because Compose owns the application's configuration, the
host-only options (`--type` for a framework, `--port`, and a `--` command)
are rejected when a `compose` type is selected or detected:

```sh
uvx localghost run --type compose
```

Before starting anything, a compose run checks that the project is actually
wired to the hub — the `localghost` network is present, and at least one
service carries `traefik.enable=true` on that network. A project that has not
run `generate` is refused with an error naming what's missing, rather than
starting and printing a URL that would never route. Setting `type = "compose"`
in `.localghost.toml` skips this check, since a committed config file is the
project declaring itself already wired.

### Configured runs

Projects may keep repeatable run settings in `.localghost.toml`:

```toml
[run]
type = "django"
name = "my-app"
port = 8080
```

Or, to run something other than the type's own server:

```toml
[run]
name = "my-app"
port = 8080
command = ["./server", "--port", "{port}"]
```

| Key | Type | Meaning |
|-----|------|---------|
| `type` | one of the seven types `run --type` accepts (not `dockerfile`, which is generate-only) | Resolves otherwise ambiguous detection, exactly like `--type`. Detected when omitted. Setting `type = "compose"` also skips the routing check described above. |
| `name` | string | Public name, serving the app at `NAME.localhost`. Defaults to the project root's name. |
| `root` | path, relative to the config file | Treat this directory as the project root instead of searching. See [Project root and configuration discovery](#project-root-and-configuration-discovery). |
| `port` | integer, 1–65535 | Host HTTP port. The type's default is used when omitted, except alongside `command`, where it is required. |
| `command` | array of strings | Argv to run instead of the type's own server. `{port}` is replaced with the selected port. |

Setting `command` makes the run a custom one: the type is reported as `custom`,
`type` has no effect, and `port` becomes required because there is no framework
default to fall back on. Set one or the other, not both.

Every key is optional, and an unrecognised key is an error rather than being
ignored — a misspelled setting fails loudly instead of silently doing
nothing. `[run].framework` is still accepted as a deprecated alias for `type`
and prints a warning; `[run].mode` is rejected outright, since `mode` has no
direct equivalent — the error names the migration (`type = "compose"`, or
remove the key and let detection choose).

Settings are layered: a command-line option wins over `.localghost.toml`,
which wins over automatic detection. Use `--config PATH` to read a different
file.

`localghost generate` writes this file for you when given a command:

```sh
uvx localghost generate --port 8080 -- ./server --port 8080
```

It refuses to overwrite an existing `.localghost.toml` unless `--extend` is
given, keeps a `.bak` copy when it does rewrite one, and prints the
configuration without writing anything under `--dry-run`.

### Project root and configuration discovery

The project root supplies the hostname and anchors configuration. Resolution
tries each of the following in order, and the first match wins:

1. `--root PATH` — resolved relative to the process working directory.
2. `[run].root` in a discovered `.localghost.toml` — resolved relative to the
   directory holding that config file.
3. The directory holding the discovered `.localghost.toml`, when one is found
   but sets no `root`.
4. The nearest ancestor of `-C`/`--directory` (inclusive) at which a type is
   detected — the default, unbounded-by-config search.

Both `--root` and `[run].root` accept `..`, so a config file at
`myrepo/tools/.localghost.toml` can set `root = ".."` to point at `myrepo`.
When the root is pinned by one of the first three rules, type detection runs
only at that directory rather than walking upward; a pinned root with no
detectable type is an error naming the available types.

`.localghost.toml` itself is discovered by the same upward search `--type`
detection uses, but only once that search has found a VCS marker. Without
one, config discovery does not walk at all: only the invocation directory
(`-C`) is a candidate, because `[run].command` is arbitrary argv that `run`
executes, so a config file must be inside a project boundary to be trusted.
`--config PATH` overrides discovery entirely, and that file's directory then
anchors the root under rule 3.

**Search boundary.** Type detection walks upward from `-C`, stopping after
the first directory containing `.git`, `.hg`, or `.svn` — that directory is
itself included as a candidate — and never reaches `$HOME` or above: even
when `$HOME` holds a VCS marker, `$HOME` itself and everything above it,
including the filesystem root, are never candidates. Config discovery uses
that same bounded walk *when* a VCS marker is present, but — as above —
does not walk at all without one. A stray `~/package.json` therefore cannot
be adopted as a project's type, and a forgotten `~/.localghost.toml` cannot
be adopted as its config.

### The status bar

A foreground `localghost run` pins the public URL to the bottom row of the
terminal, so it stays visible while the application's own output scrolls
above it:

```
 localghost  ⠹ starting hub  https://my-django-project.localhost   Ctrl+C to stop
```

The bar appears immediately, before the hub is reconciled, and names the step
it is waiting on — `starting hub` while the hub comes up, then `starting` once
the application's own process has been launched.

The spinner and the dimmed URL mean the application is not answering yet.
Localghost probes it — a host application on its port, a Compose project
through the route itself — and the bar switches to a solid URL once the first
probe succeeds, so the bar doubles as the readiness signal rather than
inviting a click that would return a gateway error.

The bar is drawn with a terminal scrolling region that covers every row except
the last, which leaves the terminal's own scrollback intact: scrolling up
still reaches everything the application printed. This holds inside terminal
multiplexers as well — it has been checked against VTE-based terminals, tmux,
and zellij, in each case reaching the first line the application printed. It is skipped when output is
not a terminal, when `TERM` is unset or `dumb`, when the window is narrower
than 40 columns, and on `--detach` and `--dry-run` runs. Pass
`--no-status-bar` to turn it off:

```sh
localghost run --no-status-bar
```

### Detached runs

`--detach` records the process outside the project directory and keeps its
output in Localghost's state directory:

```sh
localghost run --detach
localghost manage list
localghost manage list --json
localghost manage attach SESSION_ID
localghost manage stop SESSION_ID
localghost manage stop --all
```

`localghost manage` on its own lists sessions, the same as `manage list`.
Session records live in `${XDG_STATE_HOME:-$HOME/.local/state}/localghost/sessions`
(or `$LOCALGHOST_STATE_DIR` when set). A host session's liveness is probed by
its recorded process ID and a Compose session's by `docker compose ps`.

`manage stop` asks a host process to exit with `SIGTERM`, then force-quits it
with `SIGKILL` after a two second grace period; it reports an error and keeps
the record if the process somehow survives. `localghost manage clean` removes
records and bridges left by sessions that already exited, leaving running
ones alone. `localghost down` continues to control only the hub.

The app is available at `https://my-django-project.localhost`. Press Ctrl+C to
stop it.

### Custom port

Override the detected port when the default is already in use:

```sh
uvx localghost run --port 9000
```

If the port is free it is used directly. If it is occupied and `--port` was
given explicitly, an error is raised. Without `--port`, the next free port is
chosen automatically.

### Explicit type

Skip auto-detection and specify the type:

```sh
uvx localghost run --type vite
```

`--framework` still works as a deprecated alias for `--type` and prints a
warning; it is hidden from `--help`.

### Custom command

Run an arbitrary process with a custom port:

```sh
uvx localghost run --port 8080 -- my-custom-server --port 8080
```

## How it works

For host types, `localghost run` creates an ephemeral Caddy bridge container
on the `localghost` network. The Caddy container forwards requests from the
hub to the host process via `host.docker.internal`. When the foreground
process exits, the bridge is removed automatically.

If a previous `localghost run` was interrupted and left a stale bridge
container, it is detected and removed automatically before the new one
starts.

For the `compose` type, the bridge is not a container: it is the routing
labels and `localghost` network membership on the application's own service,
which `localghost generate` writes.

## Django runner detection

For Django projects, localghost selects the Python runner in this order:

1. `uv run python` if `uv.lock` is present
2. `poetry run python` if `poetry.lock`
3. `pipenv run python` if `Pipfile` or `Pipfile.lock`
4. The active `VIRTUAL_ENV` python if set
5. `.venv/bin/python` if the directory exists
6. Errors if none of the above are found

## JavaScript package-manager detection

For Vite and Astro projects, `localghost run` first uses the `packageManager`
field in `package.json` when it is present. Otherwise, lockfiles are checked in
this priority order: `bun.lock`/`bun.lockb`, `pnpm-lock.yaml`, `yarn.lock`, then
`package-lock.json`/`npm-shrinkwrap.json`; the first detected manager whose
executable is installed is used. More specific lockfiles are preferred because
they are stronger signals of intentional package-manager use. This makes
projects with more than one lockfile deterministic while allowing an
unavailable higher-priority manager to fall back to another detected manager.
Declare `packageManager` in
`package.json` when a specific manager must be used; a declared manager is not
silently replaced if its executable is missing. Localghost does not inspect
lockfile contents or timestamps, so the priority is a fallback heuristic rather
than proof of which lockfile is current.

## PHP runner detection

Modern CakePHP applications run `bin/cake server` from the application root.
When `bin/cake` is not executable, Localghost uses `php bin/cake.php` if that
entry point is present. Legacy CakePHP 2 applications run PHP's development
server from `app/webroot`, while their hostname remains derived from the
application root. Laravel applications run `php artisan serve` from the
application root. Plain `php` projects run PHP's built-in server
(`php -S 0.0.0.0:{port}`) from their docroot — the first of `public/`,
`web/`, `htdocs/`, `www/` containing `index.php`, otherwise the project root
itself.

PHP framework detection requires both framework-specific files and Composer
dependency metadata where modern projects provide it. A generic
`composer.json`, `public`, or `webroot` directory alone selects the generic
`php` type, not a specific framework runner.

A bare `index.php` at a directory with nothing else is treated as a probable
docroot rather than a project root: the search continues upward, and that
directory is used only if nothing stronger is found above it. This keeps
`app/webroot` from shadowing a CakePHP root one level up, while a
self-contained `services/billing/composer.json` inside a monorepo still stays
at `billing` rather than climbing to an unrelated top-level manifest.

### Wrapper scripts

Localghost passes `--host 0.0.0.0` and `--port` through the package manager so
the actual dev server listens on an interface the bridge can reach. If the
`dev` script calls another script, that wrapper must forward the arguments or
they will be silently discarded, leaving the server bound to localhost.

For example:

```json
{
  "scripts": {
    "dev": "./scripts/dev.sh",
    "dev:vite": "vite dev"
  }
}
```

The wrapper should pass its arguments to the nested package script:

```sh
npm run dev:vite -- "$@"
```

The `"$@"` passes Localghost's arguments through to Vite; without it, the
bridge cannot reach the server.

## Notes

- The host process must listen on `0.0.0.0`, not `127.0.0.1`, so the Caddy
  bridge can reach it.
- The bridge container uses the pinned `caddy:2.11.4-alpine` image.
- Django's `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` may need updating for the
  `.localhost` hostname — localghost warns about missing values when possible.
