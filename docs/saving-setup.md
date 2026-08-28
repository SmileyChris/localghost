# Saving a local<span class="brand-accent">ghost</span> setup

Localghost separates resolving an application from the two things you may want
to do with that resolution:

```sh
uvx localghost run          # run without changing project files
uvx localghost save         # save without running
uvx localghost run --save   # save, then run
```

The saved artifact follows the project. Host applications and custom commands
use `.localghost.toml`; Docker Compose applications use
`compose.override.yaml`. A Dockerfile without Compose can be saved as a new
`compose.yaml`. Localghost names every file it creates or changes.

## Save host run defaults

For a detected Django, Vite, Astro, CakePHP, Laravel, or plain PHP application:

```sh
uvx localghost save
```

This writes the detected type and port to `.localghost.toml`. Save an explicit
choice when more than one type is detected:

```sh
uvx localghost save --type laravel
```

Save a custom command with its required HTTP port:

```sh
uvx localghost save --port 8080 -- ./server --port 8080
```

The equivalent `run --save` forms write the same setup and then start the
application. Future runs need only `uvx localghost run`.

`save` and `run --save` use the same resolver. Both accept `-C/--directory`,
`--root`, and `--config`; both apply saved settings, project detection, command
validation, and port selection in the same order. The only difference is that
`save` stops after persistence. If no runnable type can be detected, `save`
reports the same error as `run` instead of guessing a framework.

## Save Docker Compose integration

An unconfigured Compose project cannot be routed safely, so a plain
`localghost run` explains that setup must first be saved. Do both operations:

```sh
uvx localghost run --save
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

When selection is ambiguous, Localghost prompts in an interactive terminal.
Make either choice explicit for automation:

```sh
uvx localghost save --service app --port 8000 --no-input
```

After saving, either let Localghost own the application lifecycle:

```sh
uvx localghost run
```

or start the hub and retain the normal Compose lifecycle:

```sh
uvx localghost
docker compose up
```

## Multiple project types

When a root contains more than one runnable type, Localghost refuses to guess:

```text
Multiple application types were found: compose, vite
```

Choose once with `run --type`, or remember the choice with `save` or
`run --save`:

```sh
uvx localghost run --type vite
uvx localghost save --type vite
uvx localghost run --type compose --save
```

Selecting Compose may save both `.localghost.toml` (to remember an otherwise
ambiguous type) and `compose.override.yaml` (to store the integration). Each
file has a distinct responsibility.

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
uvx localghost run --save --dry-run
```

Use `--file` repeatedly to inspect an explicit Compose file stack, or
`--output` to choose a Compose output filename. A nonstandard output is not
loaded automatically by Compose and must later be passed with `--file`.

## Dockerfile projects

When a Dockerfile exists without Compose, save a new `compose.yaml` by naming
the container's HTTP port:

```sh
uvx localghost save --type dockerfile --port 8080
```

The resulting project builds the Dockerfile, joins the external network, and
contains the same project-scoped routing labels. It can then be started with
`localghost run` or ordinary `docker compose up` after the hub is ready.
