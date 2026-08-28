"""Click command-line interface."""

from __future__ import annotations

import importlib.metadata
import importlib.resources as resources
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import click

from . import statusbar
from .compose import resolve_compose, routing_problem
from .config import (
    CONFIG_NAME,
    RunConfig,
    load_config,
    render_run_config,
    write_run_config,
)
from .feedback import (
    action,
    choices,
    compose_dry_run,
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
from .paths import state_directory
from .roots import discover_config, resolve_root
from .routes import active_routes, proxy_is_running
from .runner import (
    RUN_TYPES,
    RunPlan,
    _compose_file,
    build_plan,
    discover_type,
    django_settings_warnings,
    execute,
    find_route_collision,
    resolve_pinned_type,
    start_bridge,
    stop_bridge,
)
from .sessions import alive as session_alive
from .sessions import clean as clean_sessions
from .sessions import create as create_session
from .sessions import find_matching, sessions
from .sessions import stop as stop_session
from .trust import MkcertInstaller, PublicCertificate, TrustError, ZenNssInstaller

LOCALGHOST_VERSION = importlib.metadata.version("localghost")
TRAEFIK_IMAGE = f"localghost-traefik:v{LOCALGHOST_VERSION}"
SAVE_TYPES = (*RUN_TYPES, "dockerfile")


@dataclass(frozen=True)
class ResolvedApplication:
    """One resolution shared by run, save, and run --save."""

    cwd: Path
    config_file: Path | None
    requested_type: str | None
    selected_type: str
    root: Path
    name: str | None
    port: int | None
    command: tuple[str, ...]
    plan: RunPlan | None
    explicit_run_settings: bool


@click.group(invoke_without_command=True)
@click.version_option(package_name="localghost", message="%(version)s")
@click.option(
    "show_status",
    "--status",
    is_flag=True,
    help="Report hub state without starting or changing anything.",
)
@click.pass_context
def cli(ctx: click.Context, show_status: bool) -> None:
    """Give local applications friendly .localhost URLs."""
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
            success(f"Hub is already ready at {scheme}://traefik.localhost{suffix}")
        else:
            success(f"Hub is ready at {scheme}://traefik.localhost{suffix}")
        try:
            routes((route.hostname, route.location) for route in active_routes())
        except click.ClickException as exc:
            warning("Route listing unavailable", [exc.message])
        next_actions(https_enabled=https_enabled)


def _proxy_status() -> None:
    """Report only observable hub state; never reconcile the hub."""
    running = proxy_is_running()
    https_state = "enabled" if _https_configured() else "HTTP only"
    details(
        [
            ("Hub", "running" if running else "stopped"),
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
    """Stop and remove the hub."""
    title()
    _run_proxy("down", https_enabled=_https_configured())
    success("Hub stopped and removed.")


@cli.group(invoke_without_command=True)
@click.pass_context
def manage(ctx: click.Context) -> None:
    """Inspect and control detached application sessions."""
    if ctx.invoked_subcommand is None:
        _manage_list(False)


@manage.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print the session records as JSON instead of a table.",
)
def manage_list(as_json: bool) -> None:
    """List detached sessions and whether each one is still running."""
    _manage_list(as_json)


def _manage_list(as_json: bool) -> None:
    records = []
    for session in sessions():
        status = "running" if session_alive(session) else "stopped"
        item = session.as_dict()
        item["status"] = status
        records.append(item)
    if as_json:
        click.echo(json.dumps(records, indent=2))
        return
    if not records:
        click.echo("No managed sessions.")
        return
    for item in records:
        click.echo(
            f"{item['id']}  {item['mode']}  {item['name']}.localhost  "
            f"{item['status']}  {item['log']}"
        )


@manage.command("attach")
@click.argument("session_id")
def manage_attach(session_id: str) -> None:
    """Print the captured log of a detached session."""
    session = next((item for item in sessions() if item.id == session_id), None)
    if session is None:
        raise click.ClickException(f"unknown session '{session_id}'")
    log = Path(session.log)
    if log.exists():
        click.echo(log.read_text(encoding="utf-8", errors="replace"), nl=False)
    else:
        click.echo(f"Session {session.id} has no log yet.")


@manage.command("stop")
@click.argument("session_id", required=False)
@click.option(
    "--all", "stop_all", is_flag=True, help="Stop every detached session."
)
def manage_stop(session_id: str | None, stop_all: bool) -> None:
    """Stop one detached session, or every session with --all.

    A host session is asked to exit with SIGTERM and force-quit with SIGKILL
    after a two second grace period; its bridge is removed either way.
    """
    if bool(session_id) == stop_all:
        raise click.UsageError("provide a session ID or --all")
    targets = (
        sessions()
        if stop_all
        else [item for item in sessions() if item.id == session_id]
    )
    if not targets:
        raise click.ClickException("no matching managed session")
    stopped = 0
    failures = []
    for session in targets:
        # One stubborn process must not strand the sessions after it.
        try:
            stop_session(session)
        except click.ClickException as exc:
            failures.append(exc.message)
        else:
            stopped += 1
    if stopped:
        success(f"Stopped {stopped} session(s).")
    if failures:
        raise click.ClickException("; ".join(failures))


@manage.command("clean")
def manage_clean() -> None:
    """Remove records and bridges left behind by sessions that already exited."""
    success(f"Removed {clean_sessions()} stale session(s).")


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
    """Install, remove, or inspect the hub's public development root."""
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
    info("Preparing HTTPS trust…")
    _enable_https()
    if was_running and not was_configured:
        _run_proxy("up", already_running=True, https_enabled=True)
        success("Trusted HTTPS is enabled for the running hub.")
    elif was_running:
        success("The hub was already configured for HTTPS.")
    else:
        success("Trusted HTTPS is configured.")
        action("Start the hub", "localghost")


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
    "selected_type",
    "--type",
    type=click.Choice(RUN_TYPES),
    help="Project type; detected from the directory when omitted.",
)
@click.option(
    "framework",
    "--framework",
    type=click.Choice(RUN_TYPES),
    hidden=True,
    help="Deprecated alias for --type.",
)
@click.option(
    "root",
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    help="Treat this directory as the project root instead of searching.",
)
@click.option("port", "--port", type=click.IntRange(1, 65535), help="Host HTTP port.")
@click.option(
    "detach",
    "--detach",
    is_flag=True,
    help="Run in the background and manage it later.",
)
@click.option(
    "save_setup",
    "--save",
    is_flag=True,
    help="Save this project's Localghost setup before running.",
)
@click.option(
    "service_name",
    "--service",
    help="Compose service to expose when saving its integration.",
)
@click.option(
    "config",
    "--config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Run configuration TOML path.",
)
@click.option(
    "dry_run",
    "--dry-run",
    is_flag=True,
    help="Print the plan without starting anything.",
)
@click.option(
    "no_status_bar",
    "--no-status-bar",
    is_flag=True,
    help="Do not pin the public URL to the bottom of the terminal.",
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def run(
    name: str | None,
    working_directory: Path | None,
    selected_type: str | None,
    framework: str | None,
    root: Path | None,
    port: int | None,
    detach: bool,
    save_setup: bool,
    service_name: str | None,
    config: Path | None,
    dry_run: bool,
    no_status_bar: bool,
    command: tuple[str, ...],
) -> None:
    """Run a configured host or Compose application behind the hub."""
    if framework is not None:
        if selected_type is not None:
            raise click.UsageError("--type and --framework cannot both be given")
        warning("Deprecated option", ["--framework is deprecated; use --type"])
        selected_type = framework
    resolved = _resolve_application(
        working_directory=working_directory,
        name=name,
        selected_type=selected_type,
        root=root,
        port=port,
        config=config,
        command=command,
    )
    if resolved.selected_type == "compose":
        # Compose owns the application's configuration, so host-only
        # settings are rejected; --root is orthogonal and stays allowed.
        if resolved.command or (resolved.port is not None and not save_setup):
            raise click.ClickException(
                "compose does not accept a host command or --port; Compose "
                "owns them"
            )
        project = resolved.name or _local_project_name(resolved.root)
        if save_setup:
            _persist_resolved_application(
                resolved,
                service_name=service_name,
                port=resolved.port,
                output=None,
                extend=True,
                dry_run=dry_run,
                interactive=_is_interactive(False),
                final_hint=False,
            )
            if dry_run:
                return
        else:
            _check_compose_routing(resolved.root, project)
        if dry_run:
            compose_dry_run(project=project, url=_proxy_origin(project))
            return
        _run_compose(
            resolved.root,
            resolved.name,
            detach,
            status_bar=not no_status_bar,
        )
        return
    plan = resolved.plan
    assert plan is not None
    if save_setup:
        _persist_resolved_application(
            resolved,
            service_name=service_name,
            port=resolved.port,
            output=None,
            extend=True,
            dry_run=dry_run,
            interactive=_is_interactive(False),
            final_hint=False,
        )
        if dry_run:
            return
    matching = find_matching(name=plan.name, cwd=plan.project_root or resolved.cwd)
    if matching:
        message = (
            f"Session {matching.id} is already running; attach with: "
            f"localghost manage attach {matching.id}"
        )
        if resolved.explicit_run_settings:
            raise click.ClickException(message)
        click.echo(message)
        return
    if dry_run:
        _print_run_plan(plan, dry_run=True)
        return
    title()
    collision = find_route_collision(plan.name)
    if collision:
        _reclaim_route(collision, plan.name)
    django_warnings = django_settings_warnings(
        plan,
        plan.working_directory or resolved.cwd,
        public_origin=_proxy_origin(plan.name),
    )
    if django_warnings:
        warning("Django settings", django_warnings)
    _print_run_plan(plan, dry_run=False, detach=detach)
    if detach:
        _detach_host(plan, resolved.cwd)
        return
    status = execute(
        plan,
        lambda: _run_proxy("up", https_enabled=_https_configured()),
        cwd=plan.working_directory or resolved.cwd,
        public_origin=_proxy_origin(plan.name),
        status_bar=not no_status_bar,
    )
    if status:
        _report_application_exit(status)
        raise click.exceptions.Exit(status)
    success("Application stopped.")


def _resolve_application(
    *,
    working_directory: Path | None,
    name: str | None,
    selected_type: str | None,
    root: Path | None,
    port: int | None,
    config: Path | None,
    command: tuple[str, ...],
) -> ResolvedApplication:
    """Resolve the effective application once for running and persistence."""
    cwd = working_directory or Path.cwd()
    requested_type = selected_type
    config_file = config or discover_config(cwd)
    settings = load_config(config_file) if config_file else RunConfig()
    config_dir = config_file.parent if config_file else None
    explicit_run_settings = bool(command) or any(
        value is not None for value in (name, selected_type, port, config)
    )
    command = command or settings.command
    name = name or settings.name
    selected_type = selected_type or settings.type
    port = port or settings.port
    if command and selected_type in {"compose", "dockerfile"}:
        raise click.ClickException(
            f"a command cannot be combined with --type {selected_type}"
        )
    root_from_flag = root is not None
    pinned = resolve_root(
        start=cwd,
        flag=root,
        configured=settings.root,
        config_dir=config_dir,
    )
    resolved_root: Path | None = None
    compose_from_environment = (
        not command and selected_type is None and bool(os.environ.get("COMPOSE_FILE"))
    )
    if compose_from_environment:
        selected_type, resolved_root = "compose", cwd.resolve()
    elif not command:
        if pinned is not None:
            selected_type, resolved_root = resolve_pinned_type(
                pinned,
                selected_type,
                from_flag=root_from_flag,
            )
        else:
            selected_type, resolved_root = discover_type(cwd, selected_type)
    if selected_type == "compose":
        return ResolvedApplication(
            cwd=cwd,
            config_file=config_file,
            requested_type=requested_type,
            selected_type=selected_type,
            root=resolved_root or pinned or cwd,
            name=name,
            port=port,
            command=command,
            plan=None,
            explicit_run_settings=explicit_run_settings,
        )
    plan = build_plan(
        pinned or cwd,
        name,
        selected_type,
        port,
        command,
        pinned=pinned,
        pinned_from_flag=root_from_flag,
    )
    return ResolvedApplication(
        cwd=cwd,
        config_file=config_file,
        requested_type=requested_type,
        selected_type=plan.type,
        root=plan.project_root or pinned or cwd,
        name=name,
        port=plan.port,
        command=command,
        plan=plan,
        explicit_run_settings=explicit_run_settings,
    )


def _session_log_path(name: str) -> Path:
    return _state_directory() / "sessions" / f"{name}.log"


def _detach_host(plan: RunPlan, cwd: Path) -> None:
    _run_proxy("up", https_enabled=_https_configured())
    start_bridge(plan)
    log = _session_log_path(plan.name)
    child: subprocess.Popen[bytes] | None = None
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as handle:
            # `start_new_session` makes the child a process-group leader, which
            # is what lets `sessions.stop` signal the group by its recorded pid.
            child = subprocess.Popen(
                list(plan.command),
                cwd=plan.working_directory or cwd,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            create_session(
                mode="host",
                name=plan.name,
                port=plan.port,
                cwd=plan.project_root or cwd,
                command=plan.command,
                log=log,
                pid=child.pid,
                bridge_project=plan.project,
                bridge_yaml=plan.bridge_yaml,
            )
    except OSError as exc:
        if child is not None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(child.pid, signal.SIGTERM)
        stop_bridge(plan)
        raise click.ClickException(
            f"could not start detached application: {exc}"
        ) from exc
    success(f"Started detached session for {plan.name}.localhost.")


def _check_compose_routing(compose_root: Path, project: str) -> None:
    """Refuse a compose run whose project isn't wired to the hub.

    Reads the merged model via `resolve_compose`, never a file on disk,
    since the routing labels may arrive from an override file or a `-f`
    stack that only the resolved model reflects. `docker compose config`
    is read-only, so this is safe to run on the `--dry-run` path too.

    Never passes an explicit `--file`: that disables Compose's automatic
    `compose.override.yaml` merge, so a project `save` just fixed would
    still be refused. `_run_compose`'s real `docker compose up` also passes
    no `--file`, relying on Compose's own discovery against `cwd`, so this
    matches it exactly -- including running against `compose_root` rather
    than the process's own working directory, for `-C`/`--root` runs.
    """
    compose_file = _compose_file(compose_root)
    problem = routing_problem(resolve_compose((), cwd=compose_root))
    if problem is None:
        return
    label = compose_file.name if compose_file else "the Compose project"
    raise click.ClickException(
        f"found {label} but {problem}, so nothing would be reachable at "
        f"{_proxy_origin(project)}; run localghost run --save to save the "
        "routing setup and start the application"
    )


def _run_compose(
    cwd: Path, name: str | None, detach: bool, *, status_bar: bool = True
) -> None:
    # Same precedence save uses -- COMPOSE_PROJECT_NAME, then .env, then
    # the directory -- so this and a plain `docker compose up` agree on the
    # project rather than building two stacks from one directory.
    project = name or _local_project_name(cwd)
    title()
    public_origin = _proxy_origin(project)
    command = ["docker", "compose", "--project-name", project, "up"]
    if detach:
        command.append("--detach")
    with statusbar.pinned(
        public_origin,
        # A detached `up` returns immediately, so there is no foreground
        # lifetime for a bar to span.
        enabled=status_bar and not detach,
        # Compose owns its own ports, so readiness has to be asked of the
        # route itself rather than of a port we chose.
        probe=statusbar.http_probe(public_origin),
        message="starting hub",
    ) as bar:
        # The hub has to be reconciled before the application comes up,
        # otherwise a foreground `compose up` blocks before anything can
        # route to it.
        _run_proxy("up", https_enabled=_https_configured())
        info(f"Public URL: {public_origin}")
        bar.status("starting")
        result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode:
        _report_application_exit(result.returncode)
        raise click.exceptions.Exit(result.returncode)
    if detach:
        log = _session_log_path(project)
        create_session(
            mode="compose",
            name=project,
            port=0,
            cwd=cwd,
            command=(),
            log=log,
            pid=None,
            project=project,
        )
        success(f"Started detached Compose session for {project}.")


def _report_application_exit(status: int) -> None:
    """Leave a stable diagnosis after the transient status display is gone."""
    warning(
        "Application exited",
        [
            f"The application command exited with status {status}.",
            "Its output is above; use --no-status-bar for plain terminal output.",
        ],
    )


def _print_run_plan(plan: RunPlan, dry_run: bool, detach: bool = False) -> None:
    public_origin = _proxy_origin(plan.name)
    run_plan(
        type=plan.type,
        command=plan.command,
        port=plan.port,
        url=public_origin,
        dry_run=dry_run,
        project_root=plan.project_root,
        working_directory=plan.working_directory,
    )
    if dry_run:
        click.echo(plan.bridge_yaml, nl=False)
    elif detach:
        info(
            "Starting the application in the background; inspect and stop it "
            "with localghost manage."
        )
    else:
        info(
            "Starting foreground application; press Ctrl+C to stop it. "
            "Terminal detach (Ctrl-B D) is unavailable in this environment; "
            "use --detach with localghost manage instead."
        )


def _reclaim_route(container_id: str, name: str) -> None:
    """Remove a stale managed bridge container and continue."""
    try:
        inspection = subprocess.run(
            ["docker", "inspect", container_id],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise click.ClickException("docker is required") from exc
    if inspection.returncode:
        info(f"Container {container_id} no longer exists; continuing.")
        return
    try:
        labels = json.loads(inspection.stdout)[0].get("Config", {}).get("Labels", {})
    except (IndexError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            f"could not inspect container {container_id}"
        ) from exc
    if (
        labels.get("io.localghost.managed") == "true"
        and labels.get("io.localghost.kind") == "host-run-bridge"
    ):
        result = subprocess.run(
            ["docker", "rm", "-f", container_id],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise click.ClickException(
                f"{name}.localhost is claimed by container {container_id}; "
                f"failed to remove: {result.stderr.strip()}"
            )
        info(f"Removed stale bridge for {name}.localhost.")
    else:
        raise click.ClickException(
            f"{name}.localhost is already claimed by container {container_id}; "
            f"remove it with: docker rm -f {container_id}"
        )


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
        if action == "down":
            # `bootstrap` sits behind a profile, so a plain `down` leaves its
            # exited container behind and Docker still reports the project as
            # existing.
            command[-1:-1] = ["--profile", "bootstrap"]
        if action == "up":
            command.extend(["--detach", "--wait", "--wait-timeout", "60"])
            if force_recreate:
                command.append("--force-recreate")

        verb = "Reconciling" if already_running else "Starting"
        if action == "down":
            verb = "Stopping"
        info(f"{verb} hub…")
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
            warning("Hub command failed", [detail])
        raise click.exceptions.Exit(result.returncode)


def _state_directory() -> Path:
    return state_directory()


def _public_root_path() -> Path:
    return _state_directory() / "rootCA.pem"


def _trust_marker() -> Path:
    return _state_directory() / "https-enabled"


def _trust_fingerprint_path() -> Path:
    return _state_directory() / "root-fingerprint"


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
    if (
        _is_interactive(False)
        and shutil.which("mkcert")
        and click.confirm(
            "HTTPS is optional. Enable trusted https://*.localhost URLs now?",
            default=False,
        )
    ):
        _enable_https()
        return True
    return False


def _detect_root_rotation(certificate: PublicCertificate) -> bool:
    """Return True when the installed root CA has changed identity."""
    path = _trust_fingerprint_path()
    if not path.is_file():
        return False
    try:
        previous = path.read_text().strip()
    except OSError:
        return False
    return previous and previous != certificate.fingerprint


def _enable_https() -> None:
    was_configured = _https_configured()
    certificate = _bootstrap_public_root()
    certificate_path = _public_root_path()
    details(
        [
            (
                "Authorization",
                "sudo password may be requested for system trust",
            ),
            ("Installer", "mkcert, limited to the hub's public root"),
            ("Trust stores", "system,nss"),
            ("public-root fingerprint", certificate.fingerprint),
            ("public-root file", str(certificate_path)),
            ("private keys", "not exported or passed to mkcert"),
        ],
        title="HTTPS setup",
    )
    mkcert_installer = MkcertInstaller(certificate_path)
    zen_installer = ZenNssInstaller(certificate_path)
    root_rotated = _detect_root_rotation(certificate)
    try:
        mkcert_installer.install(force=root_rotated)
        zen_installer.install()
    except TrustError as exc:
        if was_configured:
            raise click.ClickException(
                f"existing HTTPS configuration was retained, but trust refresh "
                f"failed: {exc}"
            ) from exc
        _trust_marker().unlink(missing_ok=True)
        _trust_fingerprint_path().unlink(missing_ok=True)
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
    _trust_fingerprint_path().write_text(certificate.fingerprint)


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
        raise click.ClickException(f"{name} must be an integer from 1 to 65535")
    port = int(value)
    if not 1 <= port <= 65535:
        raise click.ClickException(f"{name} must be an integer from 1 to 65535")
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
@click.option("name", "--name", help="Local application name for NAME.localhost.")
@click.option(
    "working_directory",
    "--directory",
    "-C",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help=(
        "Application directory to detect and save "
        "(defaults to the current directory)."
    ),
)
@click.option(
    "root",
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    help="Treat this directory as the project root instead of searching.",
)
@click.option(
    "config",
    "--config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Run configuration TOML path.",
)
@click.option(
    "port",
    "--port",
    "-p",
    type=click.IntRange(1, 65535),
    help="HTTP port on which the selected application listens.",
)
@click.option(
    "output",
    "--output",
    "-o",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Compose output path when saving Compose or a Dockerfile.",
)
@click.option(
    "selected_type",
    "--type",
    type=click.Choice(SAVE_TYPES),
    help="Project type; detected from the directory when omitted.",
)
@click.option(
    "--extend",
    is_flag=True,
    help="Safely update an existing saved setup without prompting.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the saved configuration without writing it.",
)
@click.option(
    "--no-input",
    is_flag=True,
    help="Use detected defaults and never prompt.",
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def save(
    files: tuple[Path, ...],
    service_name: str | None,
    name: str | None,
    working_directory: Path | None,
    root: Path | None,
    config: Path | None,
    port: int | None,
    output: Path | None,
    selected_type: str | None,
    extend: bool,
    dry_run: bool,
    no_input: bool,
    command: tuple[str, ...],
) -> None:
    """Save Localghost setup for the current application."""
    if not dry_run:
        title()
    interactive = _is_interactive(no_input)
    if files:
        if selected_type not in (None, "compose"):
            raise click.ClickException("--file can only be used with --type compose")
        if command or name is not None or root is not None or config is not None:
            raise click.ClickException(
                "--file cannot be combined with host run settings"
            )
        _save_compose_project(
            cwd=working_directory or Path.cwd(),
            files=files,
            service_name=service_name,
            port=port,
            output=output,
            extend=extend,
            dry_run=dry_run,
            interactive=interactive,
        )
        return
    if selected_type == "dockerfile":
        if command:
            raise click.ClickException(
                "a command cannot be combined with --type dockerfile"
            )
        _save_dockerfile_project(
            working_directory=working_directory,
            root=root,
            service_name=service_name,
            port=port,
            output=output,
            dry_run=dry_run,
        )
        return
    try:
        resolved = _resolve_application(
            working_directory=working_directory,
            name=name,
            selected_type=selected_type,
            root=root,
            port=port,
            config=config,
            command=command,
        )
    except click.ClickException as run_error:
        if selected_type is not None or command:
            raise
        start = working_directory or Path.cwd()
        try:
            fallback_type, fallback_root = discover_type(
                start,
                None,
                allowed=SAVE_TYPES,
            )
        except click.ClickException:
            raise run_error from None
        if fallback_type != "dockerfile":
            raise run_error from None
        _save_dockerfile_project(
            working_directory=fallback_root,
            root=fallback_root,
            service_name=service_name,
            port=port,
            output=output,
            dry_run=dry_run,
        )
        return
    _persist_resolved_application(
        resolved,
        service_name=service_name,
        port=resolved.port,
        output=output,
        extend=extend,
        dry_run=dry_run,
        interactive=interactive,
    )


def _save_compose_project(
    *,
    cwd: Path,
    files: tuple[Path, ...],
    service_name: str | None,
    port: int | None,
    output: Path | None,
    extend: bool,
    dry_run: bool,
    interactive: bool,
    final_hint: bool = True,
) -> None:
    """Persist the Compose setup shared by ``save`` and ``run --save``."""
    output = output or cwd / "compose.override.yaml"
    output_exists = output.exists()

    inspection_files = files
    if output_exists and files and output not in files:
        inspection_files = (*files, output)
    model = (
        resolve_compose(inspection_files)
        if cwd.resolve() == Path.cwd().resolve()
        else resolve_compose(inspection_files, cwd=cwd)
    )
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
            info(f"{output} already contains the requested bridge configuration.")
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

    if not dry_run and final_hint:
        info(
            "Run it with localghost run, or start the hub and keep using "
            "docker compose up."
        )


def _persist_resolved_application(
    resolved: ResolvedApplication,
    *,
    service_name: str | None,
    port: int | None,
    output: Path | None,
    extend: bool,
    dry_run: bool,
    interactive: bool,
    final_hint: bool = True,
) -> None:
    """Persist the exact resolution used by both save entry points."""
    if resolved.selected_type == "compose":
        if output is not None and resolved.config_file is not None:
            raise click.ClickException(
                "--output cannot be combined with a run configuration file"
            )
        _save_compose_project(
            cwd=resolved.root,
            files=(),
            service_name=service_name,
            port=port,
            output=output,
            extend=extend,
            dry_run=dry_run,
            interactive=interactive,
            final_hint=final_hint,
        )
        if resolved.requested_type == "compose":
            compose_config = RunConfig(type="compose", name=resolved.name)
            if dry_run:
                click.echo(render_run_config(compose_config), nl=False)
            else:
                _save_run_config(
                    resolved.root,
                    compose_config,
                    extend=extend,
                    interactive=interactive,
                    target=resolved.config_file,
                )
        return
    if service_name is not None:
        raise click.ClickException("--service can only be used with Compose")
    if output is not None:
        raise click.ClickException(
            "--output can only be used with Compose or a Dockerfile"
        )
    plan = resolved.plan
    assert plan is not None
    saved_config = _run_config_from_plan(
        plan,
        explicit_name=resolved.name,
        source_command=resolved.command,
    )
    if dry_run:
        click.echo(render_run_config(saved_config), nl=False)
        return
    _save_run_config(
        resolved.root,
        saved_config,
        extend=extend,
        interactive=interactive,
        target=resolved.config_file,
    )


def _save_dockerfile_project(
    *,
    working_directory: Path | None,
    root: Path | None,
    service_name: str | None,
    port: int | None,
    output: Path | None,
    dry_run: bool,
) -> None:
    start = working_directory or Path.cwd()
    project_root = resolve_root(
        start=start,
        flag=root,
        configured=None,
        config_dir=None,
    )
    if project_root is None:
        _, project_root = discover_type(
            start,
            "dockerfile",
            allowed=SAVE_TYPES,
        )
    if port is None and _is_interactive(False):
        port = click.prompt(
            "Container HTTP port",
            type=click.IntRange(1, 65535),
        )
    if port is None:
        raise click.ClickException("--type dockerfile requires --port")
    service_name = service_name or "app"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service_name):
        raise click.ClickException(f"'{service_name}' is not a valid service name")
    validate_project_name_value(_local_project_name(project_root))
    output = output or project_root / "compose.yaml"
    if output.exists() and not dry_run:
        raise click.ClickException(f"refusing to overwrite existing '{output}'")
    document = create_dockerfile_compose(service_name, port)
    if dry_run:
        click.echo(render_override(document), nl=False)
        return
    write_new(output, document)
    success(f"Created {output} for the Dockerfile application.")
    info("Run it with localghost run.")


def _run_config_from_plan(
    plan: RunPlan,
    *,
    explicit_name: str | None = None,
    source_command: tuple[str, ...] = (),
) -> RunConfig:
    return RunConfig(
        type=None if plan.type == "custom" else plan.type,
        name=explicit_name,
        port=plan.port,
        command=(source_command or plan.command) if plan.type == "custom" else (),
    )


def _save_run_config(
    root: Path,
    config: RunConfig,
    *,
    extend: bool,
    interactive: bool,
    target: Path | None = None,
) -> None:
    target = target or root / CONFIG_NAME
    existed = target.exists()
    backup = write_run_config(
        target, config, extend=extend, interactive=interactive
    )
    shown = (
        Path(CONFIG_NAME)
        if target.parent.resolve() == Path.cwd().resolve()
        else target
    )
    success(f"{'Updated' if existed else 'Created'} {shown}.")
    if backup:
        shown_backup = (
            Path(backup.name)
            if backup.parent.resolve() == Path.cwd().resolve()
            else backup
        )
        info(f"Backup: {shown_backup}")


def _has_compose_file(root: Path | None = None) -> bool:
    root = root or Path.cwd()
    return bool(os.environ.get("COMPOSE_FILE")) or any(
        (root / filename).is_file()
        for filename in (
            "compose.yaml",
            "compose.yml",
            "docker-compose.yaml",
            "docker-compose.yml",
        )
    )


def _is_interactive(no_input: bool) -> bool:
    return not no_input and sys.stdin.isatty()


def _local_project_name(root: Path | None = None) -> str:
    if project_name := os.environ.get("COMPOSE_PROJECT_NAME"):
        return project_name

    root = root or Path.cwd()
    dotenv = root / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*COMPOSE_PROJECT_NAME\s*=\s*(.*?)\s*$", line)
            if match:
                return match.group(1).strip("'\"")

    normalized = re.sub(r"[^a-z0-9_-]+", "", root.name.lower())
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
