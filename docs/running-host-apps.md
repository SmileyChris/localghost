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

### Configured and detached runs

Projects may keep repeatable run settings in `.localghost.toml`:

```toml
[run]
mode = "host"
name = "my-app"
port = 8080
command = ["./server", "--port", "{port}"]
```

Command-line options take precedence over this file. Use `--config PATH` for a
different configuration. `--detach` records the process outside the project
directory and keeps its output in Localghost's state directory:

```sh
localghost run --detach
localghost manage list
localghost manage attach SESSION_ID
localghost manage stop SESSION_ID
```

`localghost manage clean` removes stale metadata and only the recorded,
Localghost-managed host bridge. `localghost down` continues to control only
the shared proxy.

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
