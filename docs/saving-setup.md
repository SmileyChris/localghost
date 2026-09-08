# Saving a local<span class="brand-accent">ghost</span> setup

Localghost separates resolving an application from the two things you may want
to do with that resolution:

```sh
uvx localghost run          # run without changing project files
uvx localghost save         # save without running
uvx localghost save --run   # save, then run
```

`run` never writes project files — that only ever happens through `save`.
`save` takes the same `--type` as `run` and detects the project type when it
is omitted, so `uvx localghost save` does the right thing for the project in
front of you.

The saved artifact follows the project. Host applications and custom commands
use `.localghost.toml`; Docker Compose applications use
`compose.override.yaml`. A Dockerfile without Compose can be saved as a new
`compose.yaml`. Localghost names every file it creates or changes.

## Save host run defaults

For a detected Django, Vite, Astro, CakePHP, Laravel, or plain PHP application:

```sh
uvx localghost save
```

This writes the detected type and port to `.localghost.toml`. Name the type
explicitly, give the project a public name, or pin its root:

```sh
uvx localghost save --type laravel
uvx localghost save --name checkout
uvx localghost save --project-root backend
```

Save a custom command with its required HTTP port:

```sh
uvx localghost save --port 8080 -- ./server --port 8080
```

Add `--run` to either form to save and then start the application. Future
runs need only `uvx localghost run`:

```sh
uvx localghost save --run
uvx localghost save --port 8080 --run -- ./server --port 8080
```

`save` uses the same resolver as `run`, applying saved settings, project
detection, command validation, and port selection in the same order. If no
runnable type can be detected, `save` reports the same error as `run` instead
of guessing a framework.

## Save Docker Compose integration

An unconfigured Compose project cannot be routed safely, so a plain
`localghost run` explains that setup must first be saved. Do both operations:

```sh
uvx localghost save --run
```

Or save without starting the project:

```sh
uvx localghost save
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

### Projects that already have an override

Compose merges exactly one override file — the first of these that exists,
and the others are ignored outright rather than merged in behind it:

1. `compose.override.yml`
2. `compose.override.yaml`
3. `docker-compose.override.yml`
4. `docker-compose.override.yaml`

So a project keeping its development configuration in
`docker-compose.override.yml` would lose all of it the moment the default
`compose.override.yaml` appeared beside it. `save` refuses rather than let
that happen, and names the file to save into instead:

```sh
uvx localghost save --output docker-compose.override.yml --extend
```

`--extend` merges the routing into the existing document and leaves a `.bak`
alongside it. Because Compose loads that file on its own, `--run` works with
it too — unlike an `--output` pointing somewhere Compose never reads.

A Compose project does not take `--name`: its public name always comes from
Docker — `COMPOSE_PROJECT_NAME`, the `.env` file, or the directory name —
the same precedence `docker compose` itself uses, and the routers `save`
creates are derived from that resolved model. A `--name` here would only
rename the ghost-page registry entry, leaving it pointing at a hostname no
router actually serves, so `save` rejects it.

When selection is ambiguous, Localghost prompts in an interactive terminal.
Make either choice explicit for automation:

```sh
uvx localghost save --service app --port 8000 --no-input
```

`save --run` validates the Compose routing before starting the application —
the same check a plain `localghost run` performs on an already-saved project —
so a misconfigured save is caught immediately instead of starting a project
that would not be reachable.

After saving, either let Localghost own the application lifecycle:

```sh
uvx localghost run
```

or start the hub and retain the normal Compose lifecycle:

```sh
uvx localghost hub up
docker compose up
```

## Options by project type

Every option parses on `save`; the ones that only make sense for one kind of
project are rejected, naming the resolved type, when used with another:

| Option | Host | Compose | Dockerfile |
| --- | --- | --- | --- |
| `--port` | host port | container port | container port |
| `--name`, `--config`, trailing command | yes | | |
| `--file` | | yes | |
| `--service`, `--output` | | yes | yes |
| `--extend` | yes | yes | |

`-C/--directory`, `--type`, `--project-root`, `--dry-run`, `--no-input`, and
`--run` apply to every project type.

## Multiple project types

When a root contains more than one runnable type, Localghost refuses to guess:

```text
both compose and vite were detected; rerun with --type compose or --type vite
```

Choose once with `run --type`, or remember the choice by saving it:

```sh
uvx localghost run --type vite
uvx localghost save --type vite
uvx localghost save --type compose
```

`run --type` decides one run. `save --type` decides every later run:
`save --type vite` writes `type = "vite"` to `.localghost.toml`, and
`save --type compose` without `--file` or `--output` writes
`type = "compose"` there as well as the `compose.override.yaml` holding the
integration itself. The two files keep distinct responsibilities —
`.localghost.toml` records which type this project is, `compose.override.yaml`
records how the Compose project reaches the hub — and afterwards a plain
`uvx localghost run` needs no flag at all.

A detected type writes no Compose pin. Detection only succeeds when it was
unambiguous, so there is nothing to remember. `save --file ...` also writes
no pin: later runs need the same `COMPOSE_FILE` stack in their environment,
because Localghost deliberately does not persist an explicit Compose file
stack in `.localghost.toml`. An explicit `--output` writes no pin either —
including one of the override names Compose merges by itself. The flag names
an output file rather than a project root, so the pin has no root it can be
sure of; a project that needs one can still write it with a plain
`save --type compose`.

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

A dry run prints each file's contents to stdout and a `Would write <file>:`
label to stderr before it, so a preview that covers two files reads as two
files while `--dry-run > file` still captures contents alone.

Use `--file` repeatedly to inspect an explicit Compose file stack, or
`--output` to choose a Compose output filename. A nonstandard output is not
loaded automatically by Compose and must later be passed with `--file`.

## Dockerfile projects

When a Dockerfile exists without Compose, save a new `compose.yaml` by naming
the container's HTTP port:

```sh
uvx localghost save --type dockerfile --port 8080
```

A lone Dockerfile is also detected by a plain `save`, which then prompts for
the port. The resulting project builds the Dockerfile, joins the external
network, and contains the same project-scoped routing labels. It can then be
started with `localghost run` or ordinary `docker compose up` after the hub is
ready.
