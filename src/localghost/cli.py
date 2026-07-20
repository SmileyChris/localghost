"""Click command-line interface."""

from __future__ import annotations

import importlib.resources as resources
from importlib.metadata import version as package_version
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click

from .compose import resolve_compose
from .feedback import (
    action,
    choices,
    details,
    info,
    next_actions,
    routes,
    run_plan,
    success,
    title,
    warning,
)
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
from .trust import MkcertInstaller, PublicCertificate, TrustError, ZenNssInstaller

LOCALGHOST_VERSION = package_version("localghost")
TRAEFIK_IMAGE = f"localghost-traefik:v{LOCALGHOST_VERSION}"


@click.group(invoke_without_command=True)
@click.version_option(package_name="localghost")
@click.option(
    "show_status",
    "--status",
    is_flag=True,
    help="Report proxy state without starting or changing anything.",
)
@click.pass_context
def cli(ctx: click.Context, show_status: bool) -> None:
    """Connect local applications to the shared development proxy."""
    if show_status:
        if ctx.invoked_subcommand is not None:
            raise click.UsageError("--status cannot be combined with a subcommand")
        title()
        _proxy_status()
        return
    if ctx.invoked_subcommand is None:
        _proxy_http_port()
        was_running = proxy_is_running()
        first_launch = not _managed_image_is_available()
        title(welcome=first_launch)
        https_enabled = _ensure_https_or_warn()
        _run_proxy("up", already_running=was_running, https_enabled=https_enabled)
        scheme = "https" if https_enabled else "http"
        port = _proxy_https_port() if https_enabled else _proxy_http_port()
        default_port = 443 if https_enabled else 80
        suffix = "" if port == default_port else f":{port}"
        if was_running:
            success(f"Shared proxy is already ready at {scheme}://traefik.localhost{suffix}")
        else:
            success(f"Shared proxy is ready at {scheme}://traefik.localhost{suffix}")
        try:
            routes((route.hostname, route.location) for route in active_routes())
        except click.ClickException as exc:
            warning("Route listing unavailable", [exc.message])
        next_actions()


def _proxy_status() -> None:
    """Report only observable proxy state; never reconcile the proxy."""
    running = proxy_is_running()
    https_state = "enabled" if _https_configured() else "HTTP only"
    details(
        [
            ("Proxy", "running" if running else "stopped"),
            ("HTTPS configuration", https_state),
        ],
        title="Localghost status",
    )
    if running:
        try:
            routes((route.hostname, route.location) for route in active_routes())
        except click.ClickException as exc:
            warning("Route listing unavailable", [exc.message])
    action("Trust details", "localghost trust --status")


@cli.command()
def down() -> None:
    """Stop and remove the shared development proxy."""
    title()
    _run_proxy("down", https_enabled=_https_configured())
    success("Proxy stopped and removed.")


@cli.command()
@click.option(
    "remove",
    "--remove",
    is_flag=True,
    help="Remove the managed root and disable HTTPS.",
)
@click.option(
    "show_status",
    "--status",
    is_flag=True,
    help="Show the managed public-root state without changing it.",
)
def trust(remove: bool, show_status: bool) -> None:
    """Install, remove, or inspect this proxy's public development root."""
    if remove and show_status:
        raise click.UsageError("--remove and --status cannot be used together")
    title()
    if show_status:
        _trust_status()
        return
    if remove:
        _remove_trust()
        return
    was_configured = _https_configured()
    was_running = proxy_is_running()
    _enable_https()
    if was_running and not was_configured:
        _run_proxy("up", already_running=True, https_enabled=True)
        success("Trusted HTTPS is enabled for the running shared proxy.")
    elif was_running:
        success("The shared proxy was already configured for HTTPS.")
    else:
        success("Trusted HTTPS is configured.")
        action("Start the proxy", "localghost")


def _remove_trust() -> None:
    """Disable HTTPS before removing only the managed public root."""
    was_configured = _https_configured()
    was_running = proxy_is_running()
    if was_running and was_configured:
        _run_proxy(
            "up",
            already_running=True,
            https_enabled=False,
            force_recreate=True,
        )
    _trust_marker().unlink(missing_ok=True)
    certificate_path = _public_root_path()
    if certificate_path.exists():
        try:
            ZenNssInstaller(certificate_path).uninstall()
            MkcertInstaller(certificate_path).uninstall()
        except TrustError as exc:
            raise click.ClickException(str(exc)) from exc
    if was_running and was_configured:
        success("HTTPS is disabled and the local root was removed from managed stores.")
    else:
        success("The local root was removed from managed stores.")


def _trust_status() -> None:
    """Show the local HTTPS state without changing trust stores."""
    certificate_path = _public_root_path()
    if not certificate_path.exists():
        details(
            [("HTTPS", "disabled (no local public root has been bootstrapped)")],
            title="Trust status",
        )
        return
    try:
        certificate = PublicCertificate.parse(certificate_path.read_bytes())
    except TrustError as exc:
        raise click.ClickException(f"invalid local public root: {exc}") from exc
    state = "enabled" if _https_configured() else "disabled"
    details(
        [
            ("HTTPS", state),
            ("Public root", str(certificate_path)),
            ("Fingerprint", certificate.fingerprint),
            ("Managed stores", "system,nss; Zen profiles when present"),
        ],
        title="Trust status",
    )


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
    title()
    collision = find_route_collision(plan.name)
    if collision:
        raise click.ClickException(
            f"{plan.name}.localhost is already claimed by container {collision}; "
            f"remove it with: docker rm -f {collision}"
        )
    django_warnings = django_settings_warnings(
        plan, cwd, public_origin=_proxy_origin(plan.name)
    )
    if django_warnings:
        warning("Django settings", django_warnings)
    _print_run_plan(plan, dry_run=False)
    status = execute(
        plan,
        lambda: _run_proxy("up", https_enabled=_https_configured()),
        cwd=cwd,
    )
    if status:
        raise click.exceptions.Exit(status)
    success("Application stopped.")


def _print_run_plan(plan: RunPlan, dry_run: bool) -> None:
    public_origin = _proxy_origin(plan.name)
    run_plan(
        framework=plan.framework,
        command=plan.command,
        port=plan.port,
        url=public_origin,
        dry_run=dry_run,
    )
    if dry_run:
        click.echo(plan.bridge_yaml, nl=False)
    else:
        info("Starting foreground application; press Ctrl+C to stop it.")


def _proxy_origin(hostname: str) -> str:
    https_enabled = _https_configured()
    port = _proxy_https_port() if https_enabled else _proxy_http_port()
    default_port = 443 if https_enabled else 80
    suffix = "" if port == default_port else f":{port}"
    scheme = "https" if https_enabled else "http"
    return f"{scheme}://{hostname}.localhost{suffix}"


def _run_proxy(
    action: str,
    *,
    already_running: bool = False,
    https_enabled: bool = False,
    force_recreate: bool = False,
) -> None:
    with _proxy_resource_directory() as resource_root:
        compose_file = resource_root / "proxy_compose.yaml"
        command = [
            "docker",
            "compose",
            "--project-name",
            "localghost",
            "--file",
            str(compose_file),
            action,
        ]
        if https_enabled:
            command[6:6] = ["--file", str(resource_root / "proxy_compose_https.yaml")]
        if action == "up":
            command.extend(["--detach", "--wait", "--wait-timeout", "60"])
            if https_enabled or force_recreate:
                command.append("--force-recreate")

        verb = "Reconciling" if already_running else "Starting"
        if action == "down":
            verb = "Stopping"
        info(f"{verb} shared proxy…")
        try:
            environment = os.environ.copy()
            environment["LOCALGHOST_IMAGE_TAG"] = f"v{LOCALGHOST_VERSION}"
            result = subprocess.run(
                command, check=False, capture_output=True, text=True, env=environment
            )
        except FileNotFoundError as exc:
            raise click.ClickException("docker is required") from exc

    if result.returncode:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        if detail:
            warning("Proxy command failed", [detail])
        raise click.exceptions.Exit(result.returncode)


def _state_directory() -> Path:
    configured = os.environ.get("LOCALGHOST_STATE_DIR")
    if configured:
        return Path(configured)
    state_home = os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    return Path(state_home) / "localghost"


def _public_root_path() -> Path:
    return _state_directory() / "rootCA.pem"


def _trust_marker() -> Path:
    return _state_directory() / "https-enabled"


def _https_configured() -> bool:
    return _public_root_path().is_file() and _trust_marker().is_file()


def _managed_image_is_available() -> bool:
    """Use Docker's image cache as the first-launch cue for interactive feedback."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", TRAEFIK_IMAGE],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _ensure_https_or_warn() -> bool:
    if _https_configured():
        return True
    if _is_interactive(False) and click.confirm(
        "HTTPS is optional. Enable trusted https://*.localhost URLs now?",
        default=False,
    ):
        _enable_https()
        return True
    warning(
        "HTTPS disabled",
        [
            "HTTP remains available for your local apps.",
        ],
    )
    action(
        "Enable HTTPS",
        "localghost trust",
        " interactively to install the local root and enable HTTPS.",
        err=True,
    )
    return False


def _enable_https() -> None:
    was_configured = _https_configured()
    certificate = _bootstrap_public_root()
    certificate_path = _public_root_path()
    details(
        [
            ("Authorization", "system authorization is required now"),
            ("Installer", "mkcert, limited to this proxy's public root"),
            ("Trust stores", "system,nss"),
            ("public-root fingerprint", certificate.fingerprint),
            ("public-root file", str(certificate_path)),
            ("private keys", "not exported or passed to mkcert"),
        ],
        title="HTTPS setup",
    )
    mkcert_installer = MkcertInstaller(certificate_path)
    zen_installer = ZenNssInstaller(certificate_path)
    try:
        mkcert_installer.install()
        zen_installer.install()
    except TrustError as exc:
        if was_configured:
            raise click.ClickException(
                f"existing HTTPS configuration was retained, but trust refresh "
                f"failed: {exc}"
            ) from exc
        _trust_marker().unlink(missing_ok=True)
        rollback_errors = []
        for name, installer in (
            ("Zen NSS", zen_installer),
            ("mkcert", mkcert_installer),
        ):
            try:
                installer.uninstall()
            except TrustError as rollback_exc:
                rollback_errors.append(f"{name}: {rollback_exc}")
        message = f"HTTPS remains disabled: {exc}"
        if rollback_errors:
            message += "; automatic trust rollback also failed: " + "; ".join(
                rollback_errors
            )
        raise click.ClickException(message) from exc
    _trust_marker().parent.mkdir(parents=True, exist_ok=True)
    _trust_marker().touch(mode=0o600, exist_ok=True)


def _bootstrap_public_root() -> PublicCertificate:
    with _proxy_resource_directory() as resource_root:
        command = [
            "docker",
            "compose",
            "--project-name",
            "localghost",
            "--file",
            str(resource_root / "proxy_compose.yaml"),
            "--file",
            str(resource_root / "proxy_compose_https.yaml"),
            "run",
            "--rm",
            "bootstrap",
            "--print-root",
        ]
        try:
            result = subprocess.run(command, check=False, capture_output=True)
        except FileNotFoundError as exc:
            raise click.ClickException("docker is required") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise click.ClickException(
            detail or "could not bootstrap the local certificate authority"
        )
    try:
        certificate = PublicCertificate.parse(result.stdout)
    except TrustError as exc:
        raise click.ClickException(
            f"bootstrap returned an invalid public root: {exc}"
        ) from exc
    _write_public_root(_public_root_path(), certificate.pem)
    return certificate


def _write_public_root(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".root-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _proxy_resource_directory() -> Iterator[Path]:
    """Provide a real directory for Compose's relative build context.

    Installed wheels are normally unpacked, but a zip-based importer cannot be
    passed to Docker. Python 3.11 cannot materialize a resource directory with
    ``importlib.resources.as_file``, so copy the small bundled build context
    when necessary.
    """
    resource_root = resources.files("localghost")
    if isinstance(resource_root, Path):
        yield resource_root
        return
    with tempfile.TemporaryDirectory(prefix="localghost-") as temporary:
        destination = Path(temporary) / "localghost"
        _copy_resource_tree(resource_root, destination)
        yield destination


def _copy_resource_tree(source, destination: Path) -> None:
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            _copy_resource_tree(child, destination / child.name)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_stream, destination.open("wb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream)


def _proxy_http_port() -> int:
    return _environment_port("LOCALGHOST_HTTP_PORT", 80)


def _proxy_https_port() -> int:
    return _environment_port("LOCALGHOST_HTTPS_PORT", 443)


def _environment_port(name: str, default: int) -> int:
    value = os.environ.get(name) or str(default)
    if not value.isascii() or not value.isdecimal():
        raise click.ClickException(
            f"{name} must be an integer from 1 to 65535"
        )
    port = int(value)
    if not 1 <= port <= 65535:
        raise click.ClickException(
            f"{name} must be an integer from 1 to 65535"
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
    if not dry_run:
        title()
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
            available = ", ".join(sorted(by_name))
            raise click.ClickException(
                f"service '{requested}' does not exist; choose one of: {available}"
            ) from exc

    likely = candidates[0]
    if not interactive:
        info(f"Selected likely service: {likely.name}", err=True)
        return likely

    choices(
        "Services",
        (
            (
                candidate.name,
                "ports " + ", ".join(str(port) for port in candidate.ports)
                if candidate.ports
                else "no declared ports",
                candidate is likely,
            )
            for candidate in candidates
        ),
    )
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
