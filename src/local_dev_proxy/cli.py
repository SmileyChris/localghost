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
    write_extended,
    write_new,
)


@click.group(invoke_without_command=True)
@click.version_option(package_name="local-dev-proxy")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Connect local applications to the shared development proxy."""
    if ctx.invoked_subcommand is None:
        _run_proxy("up")
        port = os.environ.get("LOCAL_DEV_PROXY_HTTP_PORT", "80")
        suffix = "" if port == "80" else f":{port}"
        click.echo(f"Proxy is running at http://traefik.localhost{suffix}")
        click.echo("To stop and remove it, run: local-dev-proxy down")


@cli.command()
def down() -> None:
    """Stop and remove the shared development proxy."""
    _run_proxy("down")
    click.echo("Proxy stopped and removed.")


def _run_proxy(action: str) -> None:
    resource = resources.files("local_dev_proxy").joinpath("proxy_compose.yaml")
    with resources.as_file(resource) as compose_file:
        command = ["docker", "compose", "--file", str(compose_file), action]
        if action == "up":
            command.extend(["--detach", "--wait", "--wait-timeout", "60"])

        try:
            result = subprocess.run(command, check=False)
        except FileNotFoundError as exc:
            raise click.ClickException("docker is required") from exc

    if result.returncode:
        raise click.exceptions.Exit(result.returncode)


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
        _generate_without_compose(
            service_name=service_name,
            port=port,
            output=output or Path("compose.yaml"),
            mode=mode,
            dry_run=dry_run,
            interactive=interactive,
        )
        return

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
            click.echo(
                f"Extended {output} for service '{candidate.name}' on container "
                f"port {selected_port}."
            )
            click.echo(f"Backup: {backup}")
        else:
            click.echo(f"{output} already contains the requested proxy configuration.")
    else:
        document = create_override(project_name, candidate, selected_port)
        if dry_run:
            click.echo(render_override(document), nl=False)
        else:
            write_new(output, document)
            click.echo(
                f"Created {output} for service '{candidate.name}' on container "
                f"port {selected_port}."
            )

    if not dry_run:
        click.echo(
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
    click.echo(f"Created {output} for the {description}.")
    if mode == "host":
        click.echo(
            "Ensure the host process listens on a Docker-reachable interface "
            "such as 0.0.0.0."
        )
    click.echo("Start the shared proxy, then run docker compose up.")


def _has_compose_file() -> bool:
    return any(
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
        click.echo(f"Selected likely service: {likely.name}", err=True)
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
