# Saving a local<span class="brand-accent">ghost</span> setup

Localghost separates resolving an application from the two things you may want
to do with that resolution:

```sh
uvx localghost run          # run without changing project files
uvx localghost save         # save without running
uvx localghost save --run   # save, then run
```

`run` never writes project files — that only ever happens through `save`.
`save` is a command group with one subcommand per project type — `save host`,
`save compose`, `save dockerfile` — each carrying the options specific to that
type. Bare `save` still detects the type and dispatches to the matching
subcommand, so `uvx localghost save` is equivalent to running whichever of the
three fits the project in front of you.

The saved artifact follows the project. Host applications and custom commands
use `.localghost.toml`; Docker Compose applications use
`compose.override.yaml`. A Dockerfile without Compose can be saved as a new
`compose.yaml`. Localghost names every file it creates or changes.

## Save host run defaults

For a detected Django, Vite, Astro, CakePHP, Laravel, or plain PHP application:

```sh
uvx localghost save
```

This writes the detected type and port to `.localghost.toml`. Reach the
`save host` subcommand directly for an explicit type, a public name, or a
project root:

```sh
uvx localghost save host --type laravel
uvx localghost save host --name checkout
uvx localghost save host --project-root backend
```

Save a custom command with its required HTTP port:

```sh
uvx localghost save host --port 8080 -- ./server --port 8080
```

Add `--run` to either form to save and then start the application. Future
runs need only `uvx localghost run`:

```sh
uvx localghost save --run
uvx localghost save host --port 8080 --run -- ./server --port 8080
```

`save` and `save host` use the same resolver as `run`, applying saved
settings, project detection, command validation, and port selection in the
same order. Bare `save` only exposes the type-neutral flags —
`-C/--directory`, `--dry-run`, `--no-input`, and `--run`; `--project-root`,
`--config`, `--type`, `--name`, `--extend`, and a trailing command all live on
`save host`. If no runnable type can be detected, `save` reports the same
error as `run` instead of guessing a framework.

## Save Docker Compose integration

An unconfigured Compose project cannot be routed safely, so a plain
`localghost run` explains that setup must first be saved. Do both operations:

```sh
uvx localghost save compose --run
```

Or save without starting the project:

```sh
uvx localghost save compose
```

Localghost resolves the application with `docker compose config`, selects the
most likely HTTP service and container port, and creates
`compose.override.yaml`. Compose merges that file automatically with its base
configuration.

The saved override:

- preserves the service's existing networks;
- adds the external `localghost` network;
- explicitly opts the service into Traefik;
- creates project-scoped HTTP and HTTPS routers;
- selects an explicit container port; and
- leaves every other service unchanged.

`save compose` has no `--name` option: a Compose project's public name always
comes from Docker — `COMPOSE_PROJECT_NAME`, the `.env` file, or the directory
name — the same precedence `docker compose` itself uses, and the routers
`save compose` creates are derived from that resolved model. A `--name` here
would only rename the ghost-page registry entry, leaving it pointing at a
hostname no router actually serves.

When selection is ambiguous, Localghost prompts in an interactive terminal.
Make either choice explicit for automation:

```sh
uvx localghost save compose --service app --port 8000 --no-input
```

`save compose --run` validates the Compose routing before starting the
application — the same check a plain `localghost run` performs on an
already-saved project — so a misconfigured save is caught immediately instead
of starting a project that would not be reachable.

After saving, either let Localghost own the application lifecycle:

```sh
uvx localghost run
```

or start the hub and retain the normal Compose lifecycle:

```sh
uvx localghost hub up
docker compose up
```

## Multiple project types

When a root contains more than one runnable type, Localghost refuses to guess:

```text
both compose and vite were detected; rerun with --type compose or --type vite
```

Choose once with `run --type`, or remember the choice by saving it:

```sh
uvx localghost run --type vite
uvx localghost save host --type vite
uvx localghost save compose
```

`run --type` decides one run. Naming a `save` subcommand decides every later
run: `save host --type vite` writes `type = "vite"` to `.localghost.toml`, and
a directly named `save compose` without `--file` writes `type = "compose"`
there as well as the `compose.override.yaml` holding the integration itself.
The two files keep distinct responsibilities — `.localghost.toml` records which
type this project is, `compose.override.yaml` records how the Compose project
reaches the hub — and afterwards a plain `uvx localghost run` needs no flag at
all.

Bare `uvx localghost save` writes no such pin. It reaches the Compose branch
only when detection was already unambiguous, so there is nothing to remember.
`save compose --file ...` also writes no pin: later runs need the same
`COMPOSE_FILE` stack in their environment, because Localghost deliberately does
not persist an explicit Compose file stack in `.localghost.toml`.

## Existing files and previews

Localghost safely extends compatible Compose overrides, preserves comments,
and creates a `.bak` file before changing an existing artifact. It refuses
configuration that conflicts with the required network or Traefik labels.

An existing `.localghost.toml` is left alone unless `--extend` is supplied or
an interactive update is confirmed. Unknown keys are rejected rather than
silently ignored.

Preview the artifact without writing or running anything:

```sh
uvx localghost save --dry-run
uvx localghost save --run --dry-run
```

Use `--file` repeatedly on `save compose` to inspect an explicit Compose file
stack, or `--output` (on `save compose` or `save dockerfile`) to choose a
Compose output filename. A nonstandard output is not loaded automatically by
Compose and must later be passed with `--file`.

## Dockerfile projects

When a Dockerfile exists without Compose, save a new `compose.yaml` by naming
the container's HTTP port:

```sh
uvx localghost save dockerfile --port 8080
```

The resulting project builds the Dockerfile, joins the external network, and
contains the same project-scoped routing labels. It can then be started with
`localghost run` or ordinary `docker compose up` after the hub is ready.
