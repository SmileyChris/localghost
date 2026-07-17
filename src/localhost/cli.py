"""Click command-line interface."""

from __future__ import annotations

import importlib.resources as resources
import os
import re
import subprocess
import sys
from pathlib import Path

import click

from .compose import resolve_compose
from .feedback import info, routes, run_plan, success, warning
from .generator import (
    Candidate,
    choose_port,
    create_dockerfile_compose,
    create_host_bridge_compose,
    create_override,
    extend_override,
    load_override,
    rank_services,
    render_override,
    validate_project_name,
    validate_project_name_value,
    validate_proxy_configuration,
    write_extended,
    write_new,
)
from .routes import active_routes, proxy_is_running
from .runner import (
    RunPlan,
    build_plan,
    django_settings_warnings,
    execute,
    find_route_collision,
)


@click.group(invoke_without_command=True)
@click.version_option(package_name="localhost")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Connect local applications to the shared development proxy."""
    if ctx.invoked_subcommand is None:
        port = _proxy_http_port()
        was_running = proxy_is_running()
        _run_proxy("up", already_running=was_running)
        suffix = "" if port == 80 else f":{port}"
        if was_running:
            success(f"Shared proxy is already running at http://traefik.localhost{suffix}")
        else:
            success(f"Started shared proxy at http://traefik.localhost{suffix}")
        try:
            routes((route.hostname, route.location) for route in active_routes())
        except click.ClickException as exc:
            warning("Route listing unavailable", [exc.message])
        info("To stop and remove it, run: uvx localhost down")


@cli.command()
def down() -> None:
    """Stop and remove the shared development proxy."""
    _run_proxy("down")
    success("Proxy stopped and removed.")


@cli.command()
@click.option("name", "--name", help="Public project name used for NAME.localhost.")
@click.option(
    "working_directory",
    "--directory",
    "-C",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Application directory to detect and run (defaults to the current directory).",
)
@click.option(
    "framework",
    "--framework",
    type=click.Choice(["django", "vite"]),
    help="Resolve otherwise ambiguous framework detection.",
)
@click.option("port", "--port", type=click.IntRange(1, 65535), help="Host HTTP port.")
@click.option(
    "dry_run",
    "--dry-run",
    is_flag=True,
    help="Print the plan without starting anything.",
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def run(
    name: str | None,
    working_directory: Path | None,
    framework: str | None,
    port: int | None,
    dry_run: bool,
    command: tuple[str, ...],
) -> None:
    """Run a detected host application behind an ephemeral local bridge."""
    cwd = working_directory or Path.cwd()
    plan = build_plan(cwd, name, framework, port, command)
    if dry_run:
        _print_run_plan(plan, dry_run=True)
        return
    collision = find_route_collision(plan.name)
    if collision:
        raise click.ClickException(
            f"{plan.name}.localhost is already claimed by container {collision}; "
            f"remove it with: docker rm -f {collision}"
        )
    django_warnings = django_settings_warnings(plan, cwd)
    if django_warnings:
        warning("Django settings", django_warnings)
    _print_run_plan(plan, dry_run=False)
    status = execute(plan, lambda: _run_proxy("up"), cwd=cwd)
    if status:
        raise click.exceptions.Exit(status)
    success("Application stopped.")


def _print_run_plan(plan: RunPlan, dry_run: bool) -> None:
    suffix = "" if _proxy_http_port() == 80 else f":{_proxy_http_port()}"
    run_plan(
        framework=plan.framework,
        command=plan.command,
        port=plan.port,
        url=f"http://{plan.name}.localhost{suffix}",
        dry_run=dry_run,
    )
    if dry_run:
        click.echo(plan.bridge_yaml, nl=False)
    else:
        info("Starting foreground application; press Ctrl+C to stop it.")


def _run_proxy(action: str, *, already_running: bool = False) -> None:
    resource = resources.files("localhost").joinpath("proxy_compose.yaml")
    with resources.as_file(resource) as compose_file:
        command = [
            "docker",
            "compose",
            "--project-name",
            "localhost",
            "--file",
            str(compose_file),
            action,
        ]
        if action == "up":
            command.extend(["--detach", "--wait", "--wait-timeout", "60"])

        verb = "Reconciling" if already_running else "Starting"
        if action == "down":
            verb = "Stopping"
        info(f"{verb} shared proxy…")
        try:
            result = subprocess.run(
                command, check=False, capture_output=True, text=True
            )
        except FileNotFoundError as exc:
            raise click.ClickException("docker is required") from exc

    if result.returncode:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        if detail:
            click.echo(detail, err=True)
        raise click.exceptions.Exit(result.returncode)


def _proxy_http_port() -> int:
    value = os.environ.get("LOCALHOST_HTTP_PORT") or "80"
    if not value.isascii() or not value.isdecimal():
        raise click.ClickException(
            "LOCALHOST_HTTP_PORT must be an integer from 1 to 65535"
        )
    port = int(value)
    if not 1 <= port <= 65535:
        raise click.ClickException(
            "LOCALHOST_HTTP_PORT must be an integer from 1 to 65535"
        )
    return port


@cli.command()
@click.option(
    "files",
    "--file",
    "-f",
    type=click.Path(path_type=Path, dir_okay=False),
    multiple=True,
    help="Compose file to inspect; repeat for an existing file stack.",
)
@click.option("service_name", "--service", "-s", help="Service to expose.")
@click.option(
    "port",
    "--port",
    "-p",
    type=click.IntRange(1, 65535),
    help="Container HTTP port.",
)
@click.option(
    "output",
    "--output",
    "-o",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Output path (defaults to compose.override.yaml or compose.yaml).",
)
@click.option(
    "mode",
    "--mode",
    type=click.Choice(["dockerfile", "host"]),
    help="No-Compose mode: build a Dockerfile or bridge to a host process.",
)
@click.option(
    "--extend",
    is_flag=True,
    help="Extend an existing output without prompting when it is safe.",
)
@click.option("--dry-run", is_flag=True, help="Print YAML without writing it.")
@click.option(
    "--no-input",
    is_flag=True,
    help="Use detected defaults and never prompt.",
)
def generate(
    files: tuple[Path, ...],
    service_name: str | None,
    port: int | None,
    output: Path | None,
    mode: str | None,
    extend: bool,
    dry_run: bool,
    no_input: bool,
) -> None:
    """Generate Compose configuration for the current application."""
    interactive = _is_interactive(no_input)
    if not files and not _has_compose_file():
        if extend:
            raise click.ClickException("--extend requires an existing Compose project")
        _generate_without_compose(
            service_name=service_name,
            port=port,
            output=output or Path("compose.yaml"),
            mode=mode,
            dry_run=dry_run,
            interactive=interactive,
        )
        return

    if mode is not None:
        raise click.ClickException(
            "--mode can only be used when no Compose file is present"
        )

    output = output or Path("compose.override.yaml")
    output_exists = output.exists()

    inspection_files = files
    if output_exists and files and output not in files:
        inspection_files = (*files, output)
    model = resolve_compose(inspection_files)
    project_name = validate_project_name(model)
    candidates = rank_services(model, project_name)
    candidate = _select_candidate(candidates, service_name, interactive)
    selected_port = _select_port(candidate, port, interactive)
    validate_proxy_configuration(model, project_name, candidate, selected_port)

    if output_exists:
        should_extend = extend or dry_run
        if not should_extend and interactive:
            should_extend = click.confirm(
                f"{output} already exists. Extend it safely?",
                default=False,
            )
        if not should_extend:
            raise click.ClickException(
                f"refusing to overwrite existing '{output}'; use --extend, "
                "--dry-run, or another --output"
            )
        document = load_override(output)
        changed = extend_override(
            document, model, project_name, candidate, selected_port
        )
        if dry_run:
            click.echo(render_override(document), nl=False)
        elif changed:
            backup = write_extended(output, document)
            success(
                f"Extended {output} for service '{candidate.name}' on container "
                f"port {selected_port}."
            )
            info(f"Backup: {backup}")
        else:
            info(f"{output} already contains the requested proxy configuration.")
    else:
        document = create_override(project_name, candidate, selected_port)
        if dry_run:
            click.echo(render_override(document), nl=False)
        else:
            write_new(output, document)
            success(
                f"Created {output} for service '{candidate.name}' on container "
                f"port {selected_port}."
            )

    if not dry_run:
        info(
            "Review the override, ignore it in Git if local-only, then run "
            "docker compose up."
        )


def _generate_without_compose(
    service_name: str | None,
    port: int | None,
    output: Path,
    mode: str | None,
    dry_run: bool,
    interactive: bool,
) -> None:
    if output.exists() and not dry_run:
        raise click.ClickException(f"refusing to overwrite existing '{output}'")
    validate_project_name_value(_local_project_name())

    default_mode = "dockerfile" if Path("Dockerfile").is_file() else "host"
    if mode is None and interactive:
        mode = click.prompt(
            "No Compose file found. Application type",
            default=default_mode,
            type=click.Choice(["dockerfile", "host"]),
            show_choices=True,
        )
    mode = mode or default_mode
    service_name = service_name or "app"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service_name):
        raise click.ClickException(f"'{service_name}' is not a valid service name")

    if port is None:
        if not interactive:
            raise click.ClickException(
                f"no Compose file found; --mode {mode} requires --port"
            )
        prompt = "Container HTTP port" if mode == "dockerfile" else "Host HTTP port"
        port = click.prompt(prompt, type=click.IntRange(1, 65535))

    if mode == "dockerfile":
        if not Path("Dockerfile").is_file():
            raise click.ClickException(
                "--mode dockerfile requires a Dockerfile in the current directory"
            )
        document = create_dockerfile_compose(service_name, port)
        description = "Dockerfile application"
    else:
        document = create_host_bridge_compose(service_name, port)
        description = f"host application on port {port}"

    if dry_run:
        click.echo(render_override(document), nl=False)
        return

    write_new(output, document)
    success(f"Created {output} for the {description}.")
    if mode == "host":
        info(
            "Ensure the host process listens on a Docker-reachable interface "
            "such as 0.0.0.0."
        )
    info("Start the shared proxy, then run docker compose up.")


def _has_compose_file() -> bool:
    return bool(os.environ.get("COMPOSE_FILE")) or any(
        Path(filename).is_file()
        for filename in (
            "compose.yaml",
            "compose.yml",
            "docker-compose.yaml",
            "docker-compose.yml",
        )
    )


def _is_interactive(no_input: bool) -> bool:
    return not no_input and sys.stdin.isatty()


def _local_project_name() -> str:
    if project_name := os.environ.get("COMPOSE_PROJECT_NAME"):
        return project_name

    dotenv = Path(".env")
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*COMPOSE_PROJECT_NAME\s*=\s*(.*?)\s*$", line)
            if match:
                return match.group(1).strip("'\"")

    normalized = re.sub(r"[^a-z0-9_-]+", "", Path.cwd().name.lower())
    return normalized.lstrip("-_")


def _select_candidate(
    candidates: list[Candidate], requested: str | None, interactive: bool
) -> Candidate:
    by_name = {candidate.name: candidate for candidate in candidates}
    if requested:
        try:
            return by_name[requested]
        except KeyError as exc:
            choices = ", ".join(sorted(by_name))
            raise click.ClickException(
                f"service '{requested}' does not exist; choose one of: {choices}"
            ) from exc

    likely = candidates[0]
    if not interactive:
        info(f"Selected likely service: {likely.name}", err=True)
        return likely

    click.echo("Services:")
    for candidate in candidates:
        ports = ", ".join(str(port) for port in candidate.ports) or "none declared"
        marker = " (likely)" if candidate is likely else ""
        click.echo(f"  {candidate.name}: ports {ports}{marker}")
    selected = click.prompt(
        "Service",
        default=likely.name,
        type=click.Choice([candidate.name for candidate in candidates]),
        show_choices=False,
    )
    return by_name[selected]


def _select_port(candidate: Candidate, requested: int | None, interactive: bool) -> int:
    selected = choose_port(candidate, requested)
    if selected is not None:
        return selected

    if not interactive:
        if candidate.ports:
            choices = ", ".join(str(port) for port in candidate.ports)
            detail = f"multiple possible ports ({choices})"
        else:
            detail = "no declared container ports"
        raise click.ClickException(
            f"service '{candidate.name}' has {detail}; rerun with --port"
        )

    default = candidate.ports[0] if candidate.ports else None
    return click.prompt(
        "Container HTTP port",
        default=default,
        type=click.IntRange(1, 65535),
        show_default=default is not None,
    )
