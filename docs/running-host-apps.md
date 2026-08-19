# Running host applications with local<span class="brand-accent">ghost</span>

`localghost run` lets you serve a framework application running directly on
your host machine behind the proxy, without a Dockerfile or Compose file.

## Supported frameworks

| Framework | Detection | Default port |
|-----------|-----------|-------------|
| **Django** | `manage.py` in the project directory | 8000 |
| **Vite** | `package.json` with a `dev` script and `vite` dependency | 5173 |
| **Astro** | `package.json` with a `dev` script and `astro` dependency | 4321 |
| **CakePHP 3+** | `bin/cake` and a `cakephp/cakephp` Composer dependency | 8765 |
| **CakePHP 2** | `app/Config/core.php` and `app/webroot/index.php` | 8765 |
| **Laravel** | `artisan` and a `laravel/framework` Composer dependency | 8000 |

Detection runs automatically. If more than one framework is detected (for
example, a Django project that also has a `package.json` with Vite), use
`--framework` to resolve the ambiguity:

```sh
uvx localghost run --framework django
```

Localghost searches upward from the selected directory to the nearest
framework root, stopping at the Git worktree boundary. This means it can be
run from directories such as `webroot`, `public`, or a nested source package
without naming the application after that directory. The detected root supplies
the default public name and application configuration. Frameworks may still
use a different working directory for their server process.

## Usage

Start the proxy first (if it isn't already running), then run your app:

```sh
cd my-django-project
uvx localghost run
```

### Host and Compose runs

`localghost run` serves either a host process or a Docker Compose project. The
mode is detected from the directory: a Compose file beside the invocation
selects `compose`, and an application root found by the framework search
selects `host`. When both are present in the same directory, name the one you
want:

```sh
uvx localghost run --mode host
uvx localghost run --mode compose
```

In `compose` mode Localghost starts the shared proxy, then hands the project to
`docker compose up`. Because Compose owns the application's configuration, the
host-only options (`--framework`, `--port`, and a `--` command) are rejected in
this mode.

!!! note

    `run --mode` chooses how an application is *started* and takes `host` or
    `compose`. It is unrelated to `generate --mode`, which chooses what
    configuration to *write* and takes `dockerfile` or `host`.

### Configured runs

Projects may keep repeatable run settings in `.localghost.toml`:

```toml
[run]
mode = "host"
name = "my-app"
framework = "django"
port = 8080
command = ["./server", "--port", "{port}"]
```

| Key | Type | Meaning |
|-----|------|---------|
| `mode` | `"host"` or `"compose"` | How to start the application. Detected when omitted. |
| `name` | string | Public name, serving the app at `NAME.localhost`. Defaults to the project root's name. |
| `framework` | `django`, `vite`, `astro`, `cakephp`, or `laravel` | Resolves otherwise ambiguous detection, exactly like `--framework`. |
| `port` | integer, 1–65535 | Host HTTP port. The framework default is used when omitted. |
| `command` | array of strings | Argv to run instead of the framework's server. `{port}` is replaced with the selected port. |

Every key is optional, and an unrecognised key is an error rather than being
ignored — a misspelled setting fails loudly instead of silently doing nothing.

Settings are layered: a command-line option wins over `.localghost.toml`, which
wins over automatic detection. Use `--config PATH` to read a different file.

`localghost generate` writes this file for you when given a command:

```sh
uvx localghost generate --port 8080 -- ./server --port 8080
```

It refuses to overwrite an existing `.localghost.toml` unless `--extend` is
given, keeps a `.bak` copy when it does rewrite one, and prints the
configuration without writing anything under `--dry-run`.

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
records and bridges left by sessions that already exited, leaving running ones
alone. `localghost down` continues to control only the shared proxy.

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

### Explicit framework

Skip auto-detection and specify the framework:

```sh
uvx localghost run --framework vite
```

### Custom command

Run an arbitrary process with a custom port:

```sh
uvx localghost run --port 8080 -- my-custom-server --port 8080
```

## How it works

`localghost run` creates an ephemeral Caddy reverse-proxy container on the
shared `localghost` network. The Caddy container forwards requests from Traefik
to the host process via `host.docker.internal`. When the foreground process
exits, the Caddy bridge is removed automatically.

If a previous `localghost run` was interrupted and left a stale bridge
container, it is detected and removed automatically before the new one starts.

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
application root.

PHP framework detection requires both framework-specific files and Composer
dependency metadata where modern projects provide it. A generic
`composer.json`, `public`, or `webroot` directory is not enough to select a
runner automatically.

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
