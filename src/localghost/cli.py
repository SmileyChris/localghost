"""Click command-line interface."""

from __future__ import annotations

import difflib
import importlib.metadata
import importlib.resources as resources
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import click

from . import forwarder, picker, registry, statusbar
from .compose import resolve_compose, routing_problem
from .config import (
    CONFIG_NAME,
    RunConfig,
    load_config,
    local_config_path,
    render_run_config,
    write_run_config,
)
from .feedback import (
    COMMAND,
    LIME,
    MINT,
    action,
    choices,
    compose_dry_run,
    details,
    failure,
    info,
    next_actions,
    routes,
    run_plan,
    sign_off,
    styled,
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
    GENERIC_NAMES,
    RUN_TYPES,
    RunPlan,
    _compose_file,
    attached_compose_up,
    build_plan,
    detach_key_available,
    discover_type,
    django_settings_warnings,
    execute,
    find_route_collision,
    resolve_pinned_type,
    start_bridge,
    stop_bridge,
)
from .sessions import Session, sessions
from .sessions import alive as session_alive
from .sessions import clean as clean_sessions
from .sessions import create as create_session
from .sessions import discard as discard_session
from .sessions import find as find_session
from .sessions import live as live_session
from .sessions import reap as reap_sessions
from .sessions import stop as stop_session
from .tailscale import (
    API as TailscaleAPI,
)
from .tailscale import (
    DNS_CONSOLE_URL,
    MACHINES_CONSOLE_URL,
    OAUTH_CONSOLE_URL,
    POLICY_CONSOLE_URL,
    TailnetHub,
    TailscaleError,
    TailscaleState,
    tag_in_use,
    tailnet_hubs,
)
from .tailscale import (
    default_node as default_tailscale_node,
)
from .tailscale import (
    delete_credential as delete_tailscale_credential,
)
from .tailscale import (
    describe_hub as describe_tailnet_hub,
)
from .tailscale import (
    detect_suffix as detect_tailscale_suffix,
)
from .tailscale import (
    fetch_public_root as fetch_tailscale_root,
)
from .tailscale import (
    load_credential as load_tailscale_credential,
)
from .tailscale import (
    load_state as load_tailscale_state,
)
from .tailscale import (
    remove_state as remove_tailscale_state,
)
from .tailscale import (
    save_state as save_tailscale_state,
)
from .tailscale import (
    store_credential as store_tailscale_credential,
)
from .tailscale import (
    suffix_is_public as tailscale_suffix_is_public,
)
from .tailscale import (
    validate_suffix as validate_tailscale_suffix,
)
from .trust import (
    MkcertInstaller,
    PublicCertificate,
    TrustError,
    ZenNssInstaller,
    require_mkcert,
)

LOCALGHOST_VERSION = importlib.metadata.version("localghost")
TRAEFIK_IMAGE = f"localghost-traefik:v{LOCALGHOST_VERSION}"
GATEWAY_IMAGE = f"localghost-tailscale-gateway:v{LOCALGHOST_VERSION}"
PROJECT_NAME = "localghost"
# Everything `save` can persist; `run` cannot start a bare Dockerfile.
SAVE_TYPES = (*RUN_TYPES, "dockerfile")
# Compose auto-loads exactly ONE override file: the first of these that
# exists. The rest are ignored outright — they are not merged in behind it.
# Order measured against Docker Compose 5.4.0, and note that `.yml` beats
# `.yaml` here, the reverse of the base-file convention. Writing the wrong
# one of these silently disables a project's real override, so `save compose`
# checks the whole set rather than assuming `compose.override.yaml`.
COMPOSE_OVERRIDE_NAMES = (
    "compose.override.yml",
    "compose.override.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
)


@dataclass(frozen=True)
class ResolvedApplication:
    """One resolution shared by run and save."""

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
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Give local applications friendly .localhost URLs."""
    if ctx.invoked_subcommand is None:
        title()
        _proxy_status()


@cli.command()
@click.option(
    "as_json",
    "--json",
    is_flag=True,
    help="Print the status as JSON instead of a table.",
)
def status(as_json: bool) -> None:
    """Report hub state, HTTPS, routes, and remembered projects."""
    if not as_json:
        title()
    _proxy_status(as_json)


RECENT_PROJECTS = 5


def _started_at(project: dict[str, object]) -> float:
    """When a project last started, for ordering; unreadable sorts last.

    Registry files are read leniently, so the value may be missing, malformed,
    or a naive time from an older writer (taken as local time).
    """
    try:
        return datetime.fromisoformat(str(project["last_started"])).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def _proxy_status(as_json: bool = False) -> None:
    """Report only observable hub state; never reconcile the hub."""
    running = proxy_is_running()
    remembered = _projects()
    if as_json:
        payload: dict[str, object] = {
            "hub": "running" if running else "stopped",
            "https": "enabled" if _https_configured() else "http-only",
            "routes": [],
            # The same project objects `sessions list --json` prints.
            "remembered": remembered,
        }
        if running:
            try:
                payload["routes"] = [
                    {"hostname": route.hostname, "location": route.location}
                    for route in active_routes()
                ]
            except click.ClickException as exc:
                payload["routes_error"] = exc.message
        click.echo(json.dumps(payload, indent=2))
        return
    https_state = "enabled" if _https_configured() else "HTTP only"
    details(
        [
            ("Hub", "running" if running else "stopped"),
            ("HTTPS configuration", https_state),
        ],
    )
    idle = not running
    if running:
        try:
            listed = [(route.hostname, route.location) for route in active_routes()]
        except click.ClickException as exc:
            warning("Route listing unavailable", [exc.message])
        else:
            click.echo()
            routes(listed)
            idle = not listed
    live = [project for project in remembered if project["state"] == "running"]
    if live and not running:
        # A detached app outlives a stopped hub, which is no longer routing it.
        click.echo()
        details(
            [(str(p["hostname"]), str(p["directory"])) for p in live],
            title="Running without the hub",
            label_style="default",
        )
        action("Start the hub", f"{COMMAND} hub up")
    # Running projects are covered above or under Active routes, and a project
    # whose directory has gone cannot be restarted; the rest is `sessions`' job.
    recent = sorted(
        (
            project
            for project in remembered
            if project["state"] == "stopped" and project["directory_exists"]
        ),
        key=_started_at,
        reverse=True,
    )[:RECENT_PROJECTS]
    if recent:
        click.echo()
        details(
            [(str(p["hostname"]), str(p["directory"])) for p in recent],
            title="Recent projects",
            label_style="default",
        )
    if len(recent) + (0 if running else len(live)) < len(remembered):
        action("List all projects", f"{COMMAND} sessions")
    click.echo()
    if idle:
        # A stopped hub, or one serving nothing, has one obvious next step.
        action("Start an application", f"{COMMAND} run", " in its directory.")
    action("Check HTTPS trust", f"{COMMAND} trust")


@cli.group(invoke_without_command=True)
@click.pass_context
def hub(ctx: click.Context) -> None:
    """Start, stop, and inspect the shared Traefik hub."""
    if ctx.invoked_subcommand is None:
        title()
        _proxy_status()


@hub.command("down")
def hub_down() -> None:
    """Stop and remove the hub."""
    title()
    _run_proxy("down", https_enabled=_https_configured())
    success("Hub stopped and removed.")


@hub.command("logs")
@click.option(
    "follow",
    "--follow",
    "-f",
    is_flag=True,
    help="Keep printing new output until interrupted.",
)
@click.option(
    "tail", "--tail", default="100", help="Lines of history to show, or 'all'."
)
def hub_logs(follow: bool, tail: str) -> None:
    """Print the hub's Traefik logs."""
    with _proxy_resource_directory() as resource_root:
        command = [
            "docker",
            "compose",
            "--project-name",
            "localghost",
            "--file",
            str(resource_root / "proxy_compose.yaml"),
            "logs",
        ]
        if follow:
            command.append("--follow")
        command.extend(["--tail", tail, "traefik"])
        try:
            environment = os.environ.copy()
            environment["LOCALGHOST_IMAGE_TAG"] = f"v{LOCALGHOST_VERSION}"
            result = subprocess.run(command, check=False, env=environment)
        except FileNotFoundError as exc:
            raise click.ClickException("docker is required") from exc
    if result.returncode:
        raise click.exceptions.Exit(result.returncode)


@hub.command("up")
@click.option(
    "rebuild",
    "--rebuild",
    is_flag=True,
    help="Rebuild the hub images before starting, even when they exist.",
)
def hub_up(rebuild: bool) -> None:
    """Start the hub."""
    _proxy_http_port()
    was_running = proxy_is_running()
    first_launch = not _managed_image_is_available()
    title(welcome=first_launch)
    https_enabled = _ensure_https_or_warn() or _tailnet_forces_https()
    with suppress(TailscaleError):
        tailnet = load_tailscale_state()
        if tailnet is not None and not tailnet.https:
            _public_suffix_notice(tailnet.suffix)
    _run_proxy(
        "up",
        already_running=was_running,
        https_enabled=https_enabled,
        rebuild=rebuild,
    )
    reap_sessions()
    scheme = "https" if https_enabled else "http"
    port = _proxy_https_port() if https_enabled else _proxy_http_port()
    default_port = 443 if https_enabled else 80
    suffix = "" if port == default_port else f":{port}"
    if was_running:
        success(f"Hub is already ready at {scheme}://traefik.localhost{suffix}")
    else:
        success(f"Hub is ready at {scheme}://traefik.localhost{suffix}")
    _report_tailnet_origin("traefik")
    try:
        routes((route.hostname, route.location) for route in active_routes())
    except click.ClickException as exc:
        warning("Route listing unavailable", [exc.message])
    next_actions(https_enabled=https_enabled)


def _complete_project_name(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[str]:
    """Complete remembered project names; never raise into the shell."""
    try:
        entries = registry.entries()
    except OSError:
        return []
    return [entry.name for entry in entries if entry.name.startswith(incomplete)]


def _restart_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _entry_for_directory(
    entries: list[registry.RegistryEntry], cwd: Path
) -> registry.RegistryEntry | None:
    """The remembered project containing `cwd`, the innermost if nested."""
    cwd = cwd.resolve()
    containing = [
        entry
        for entry in entries
        if cwd.is_relative_to(Path(entry.directory).resolve())
    ]
    return max(
        containing, key=lambda entry: len(Path(entry.directory).parts), default=None
    )


def _restart_target(
    target: str, entries: list[registry.RegistryEntry]
) -> registry.RegistryEntry:
    """Resolve a session ID, then a project name, to what `run` needs."""
    session = find_session(target)
    name = session.name if session else target
    entry = next((entry for entry in entries if entry.name == name), None)
    if entry is not None:
        return entry
    if session is not None:
        # Registry writes are best-effort, so a session can outlive or
        # predate its entry; its record holds enough to start it again.
        return registry.RegistryEntry(
            hostname=f"{session.name}.localhost",
            name=session.name,
            directory=session.cwd,
            type=session.mode,
            last_started="",
            detached=session.detached,
        )
    _no_such_project(target, (entry.name for entry in entries))


def _no_such_project(
    name: str, known: Iterable[str], kind: str = "remembered"
) -> NoReturn:
    """One wording for an unmatched name: suggest near misses, don't dump."""
    close = difflib.get_close_matches(name, sorted(set(known)), n=3)
    hint = f"; did you mean {' or '.join(close)}?" if close else ""
    raise click.ClickException(
        f"no {kind} project '{name}'{hint}\n  List them with: {COMMAND} sessions"
    )


def _choose_entry(
    entries: list[registry.RegistryEntry],
) -> registry.RegistryEntry | None:
    if not entries:
        click.echo("Nothing remembered yet; run a project to give it a ghost.")
        return None
    if _restart_interactive():
        return picker.pick(entries, forget=registry.forget, restore=registry.restore)
    for entry in entries:
        click.echo(f"{entry.hostname}  {entry.type}  {entry.directory}")
    return None


def _restart_entry(
    ctx: click.Context,
    entry: registry.RegistryEntry,
    *,
    foreground: bool,
    **overrides: object,
) -> None:
    directory = Path(entry.directory)
    if not directory.is_dir():
        raise click.ClickException(
            f"remembered directory '{directory}' no longer exists; "
            f"forget it with: {COMMAND} sessions forget {entry.name}"
        )
    running = None if overrides["dry_run"] else live_session(entry.name)
    if running is not None:
        if not running.detached:
            message = f"{entry.name} is running in the foreground in another terminal"
            if not _restart_interactive():
                raise click.ClickException(
                    f"{message}; stop it with: {COMMAND} sessions stop {entry.name}"
                )
            click.confirm(f"{message}. Stop it and run it here?", abort=True)
        stop_session(running)
        info(f"Stopped {entry.name}.")
    if not foreground and not overrides["detach"]:
        overrides["detach"] = entry.detached
    # The recorded name, not whatever the directory would derive: a project
    # first run with --name must come back under that name.
    overrides["name"] = overrides["name"] or entry.name
    flags = entry.flags
    if overrides["selected_type"] is not None:
        # A different type is a different application; what was told to the
        # old one (a command above all) would only conflict with it.
        flags = replace(flags, type=None, port=None, isolate=False, command=())
    overrides["selected_type"] = overrides["selected_type"] or flags.type
    overrides["port"] = overrides["port"] or flags.port
    if overrides["root"] is None and flags.root is not None:
        overrides["root"] = Path(flags.root)
    if overrides["config"] is None and flags.config is not None:
        overrides["config"] = Path(flags.config)
    ctx.invoke(
        run,
        working_directory=directory,
        isolate=flags.isolate,
        command=flags.command,
        **overrides,
    )


@cli.command()
@click.argument("name", required=False, shell_complete=_complete_project_name)
@click.option("app_name", "--name", help="Public project name used for NAME.localhost.")
@click.option(
    "selected_type",
    "--type",
    type=click.Choice(RUN_TYPES),
    help="Project type; detected from the remembered directory when omitted.",
)
@click.option(
    "root",
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    help="Treat this directory as the project root instead of searching.",
)
@click.option(
    "port", "--port", "-p", type=click.IntRange(1, 65535), help="Host HTTP port."
)
@click.option(
    "config",
    "--config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Run configuration TOML path.",
)
@click.option(
    "detach",
    "--detach",
    is_flag=True,
    help="Run in the background, whichever way it last ran.",
)
@click.option(
    "foreground",
    "--foreground",
    is_flag=True,
    help="Run in this terminal, whichever way it last ran.",
)
@click.option(
    "dry_run",
    "--dry-run",
    is_flag=True,
    help="Print the plan without stopping or starting anything.",
)
@click.option(
    "no_status_bar",
    "--no-status-bar",
    is_flag=True,
    help="Do not pin the public URL to the bottom of the terminal.",
)
@click.pass_context
def restart(
    ctx: click.Context, name: str | None, foreground: bool, **overrides: object
) -> None:
    """Restart a project, or start a stopped one.

    NAME is a project name or session ID. Without it, restart the project in
    the current directory, or pick from the remembered projects. A project
    comes back the way it last ran unless --foreground or --detach says
    otherwise.
    """
    if foreground and overrides["detach"]:
        raise click.UsageError("--foreground and --detach cannot both be given")
    overrides["name"] = overrides.pop("app_name")
    entries = registry.entries()
    if name is not None:
        entry = _restart_target(name, entries)
    else:
        entry = _entry_for_directory(entries, Path.cwd()) or _choose_entry(entries)
        if entry is None:
            return
    _restart_entry(ctx, entry, foreground=foreground, **overrides)


cli.add_command(
    click.Command(
        "summon",
        callback=restart.callback,
        params=restart.params,
        help=restart.help,
        hidden=True,
        deprecated=f"Use '{COMMAND} restart' instead.",
    )
)


@cli.group("sessions", invoke_without_command=True)
@click.pass_context
def sessions_group(ctx: click.Context) -> None:
    """List remembered projects and control their sessions."""
    if ctx.invoked_subcommand is None:
        _sessions_list(False)


def _projects() -> list[dict[str, object]]:
    """Remembered projects joined by name with their session records.

    A running session with no registry entry still appears, since registry
    writes are best-effort.
    """
    reap_sessions()
    records = sessions()
    projects = [entry.as_dict() for entry in registry.entries()]
    remembered = {project["name"] for project in projects}
    for session in records:
        if session.name not in remembered and session_alive(session):
            remembered.add(session.name)
            projects.append(
                {
                    "hostname": f"{session.name}.localhost",
                    "name": session.name,
                    "directory": session.cwd,
                    "type": session.mode,
                    "last_started": None,
                    "detached": session.detached,
                }
            )
    for project in projects:
        named = [item for item in records if item.name == project["name"]]
        running = next((item for item in named if session_alive(item)), None)
        latest = running or (find_session(str(project["name"])) if named else None)
        project["state"] = "running" if running else "stopped"
        project["directory_exists"] = Path(str(project["directory"])).is_dir()
        project["session"] = latest.as_dict() if latest else None
    return projects


def _project_mode(project: dict[str, object]) -> str:
    session = project["session"]
    if project["state"] != "running" or not isinstance(session, dict):
        return ""
    mode = "detached" if session["detached"] else "foreground"
    return f"{mode} isolated" if session.get("isolated") else mode


def _sessions_list(as_json: bool, state: str | None = None) -> None:
    projects = [
        project
        for project in _projects()
        if state is None or project["state"] == state
    ]
    if as_json:
        click.echo(json.dumps(projects, indent=2))
        return
    if not projects:
        if state:
            click.echo(f"No {state} projects.")
        else:
            click.echo("Nothing remembered yet; run a project to give it a ghost.")
        return
    # A filter makes the state column redundant, and stopped projects have no
    # mode; dimming only means something next to running projects.
    rows = []
    for project in projects:
        running = project["state"] == "running"
        quiet = "dim" if state is None and not running else "default"
        cells = [(str(project["hostname"]), quiet)]
        if state is None:
            cells.append((str(project["state"]), LIME if running else quiet))
        if state != "stopped":
            cells.append((_project_mode(project), quiet))
        rows.append((project, quiet, cells))
    widths = [
        max(len(cells[column][0]) for _, _, cells in rows)
        for column in range(len(rows[0][2]))
    ]
    for project, quiet, cells in rows:
        parts = [
            (f"{text.ljust(width)}  ", style)
            for (text, style), width in zip(cells, widths, strict=True)
        ]
        parts.append((str(project["directory"]), quiet))
        if not project["directory_exists"]:
            parts.append((" (missing)", "yellow"))
        styled(parts)


@sessions_group.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print the projects and their session records as JSON.",
)
@click.option("running", "--running", is_flag=True, help="Only list running projects.")
@click.option("stopped", "--stopped", is_flag=True, help="Only list stopped projects.")
def sessions_list(as_json: bool, running: bool, stopped: bool) -> None:
    """List remembered projects and whether each one is running."""
    if running and stopped:
        raise click.UsageError("give --running or --stopped, not both")
    _sessions_list(as_json, "running" if running else "stopped" if stopped else None)


def _target_session(target: str, *, running: bool = False) -> Session:
    session = find_session(target)
    if session is None:
        # Suggest only names worth acting on: stopping one that already
        # exited just clears its record.
        records = [item for item in sessions() if not running or session_alive(item)]
        _no_such_project(
            target,
            (item.name for item in records),
            "running" if running else "logged",
        )
    return session


@sessions_group.command("logs")
@click.argument("target", shell_complete=_complete_project_name)
@click.option(
    "follow",
    "--follow",
    "-f",
    is_flag=True,
    help="Keep printing new output until interrupted.",
)
def sessions_logs(target: str, follow: bool) -> None:
    """Print the captured log of a session, by project name or session ID."""
    session = _target_session(target)
    if session.mode == "compose" and session.project:
        command = [
            "docker",
            "compose",
            "--project-name",
            session.project,
            "logs",
        ]
        if follow:
            command.append("--follow")
        try:
            result = subprocess.run(command, cwd=session.cwd, check=False)
        except FileNotFoundError as exc:
            raise click.ClickException("docker is required") from exc
        if result.returncode:
            raise click.exceptions.Exit(result.returncode)
        return
    if not session.detached:
        click.echo(
            f"{session.name} is running in the foreground in another terminal; "
            "its output is there, not in a log."
        )
        return
    log = Path(session.log)
    if not log.exists():
        click.echo(f"{session.name} has no log yet.")
        return
    with log.open("r", encoding="utf-8", errors="replace") as handle:
        click.echo(handle.read(), nl=False)
        if not follow:
            return
        while True:
            line = handle.readline()
            if line:
                click.echo(line, nl=False)
                continue
            if not session_alive(session):
                return
            time.sleep(0.2)


@sessions_group.command("stop")
@click.argument("target", required=False, shell_complete=_complete_project_name)
@click.option("--all", "stop_all", is_flag=True, help="Stop every session.")
def sessions_stop(target: str | None, stop_all: bool) -> None:
    """Stop one session, by project name or session ID, or every one with --all.

    A host session is asked to exit with SIGTERM and force-quit with SIGKILL
    after a grace period; its bridge is removed either way.
    """
    if bool(target) == stop_all:
        raise click.UsageError("give a project name, a session ID, or --all")
    targets = sessions() if stop_all else [_target_session(target, running=True)]
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


@sessions_group.command("forget")
@click.argument("name", required=False, shell_complete=_complete_project_name)
@click.option(
    "--all",
    "forget_everything",
    is_flag=True,
    help="Forget every project that isn't running.",
)
@click.option(
    "missing",
    "--missing",
    is_flag=True,
    help="Forget every stopped project whose directory no longer exists.",
)
def sessions_forget(name: str | None, forget_everything: bool, missing: bool) -> None:
    """Drop a remembered project, its ghost page, and its stopped sessions.

    The project's own files are never touched.
    """
    if sum([name is not None, forget_everything, missing]) != 1:
        raise click.UsageError("give a project name, --missing, or --all")
    if missing:
        running = {item.name for item in sessions() if session_alive(item)}
        gone = sorted(
            entry.name
            for entry in registry.entries()
            if entry.name not in running and not Path(entry.directory).is_dir()
        )
        for gone_name in gone:
            registry.forget(gone_name)
            clean_sessions(gone_name)
        if gone:
            success(f"Forgot {len(gone)} project(s): {', '.join(gone)}")
        else:
            info("No stopped project has a missing directory.")
        return
    if forget_everything:
        running = sorted({item.name for item in sessions() if session_alive(item)})
        removed = sum(
            registry.forget(entry.name)
            for entry in registry.entries()
            if entry.name not in running
        )
        clean_sessions()
        success(f"Forgot {removed} project(s).")
        if running:
            info(f"Kept running project(s): {', '.join(running)}")
        return
    assert name is not None
    if live_session(name) is not None:
        raise click.ClickException(
            f"{name} is running; stop it first with: {COMMAND} sessions stop {name}"
        )
    cleaned = clean_sessions(name)
    if not registry.forget(name) and not cleaned:
        _no_such_project(name, (entry.name for entry in registry.entries()))
    success(f"Forgot {name}.")


cli.add_command(
    click.Command(
        "forget",
        callback=sessions_forget.callback,
        params=sessions_forget.params,
        help=sessions_forget.help,
        hidden=True,
        deprecated=f"Use '{COMMAND} sessions forget' instead.",
    )
)


# Kept for scripts: reaping and `run` already clear what this removed, so all
# it adds is discarding the last stopped run's log early.
@sessions_group.command(
    "clean", hidden=True, deprecated="Stopped sessions are cleared automatically."
)
def sessions_clean() -> None:
    """Remove records and bridges left behind by sessions that already exited."""
    success(f"Removed {clean_sessions()} stale session(s).")


@cli.group(invoke_without_command=True)
@click.pass_context
def trust(ctx: click.Context) -> None:
    """Inspect, install, or remove the hub's public development root."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(trust_status)


@trust.command("install")
def trust_install() -> None:
    """Install the managed public root and enable HTTPS."""
    title()
    was_configured = _https_configured()
    was_running = proxy_is_running()
    info("Preparing HTTPS trust…")
    _enable_https()
    tailnet = load_tailscale_state()
    if tailnet is not None and tailnet.https:
        # An unreachable gateway must not strand the localhost half of trust
        # or skip the hub reconcile below.
        try:
            _install_tailnet_trust(tailnet.suffix, show_details=False)
        except click.ClickException as exc:
            warning("Tailnet trust was not installed", [str(exc)])
            action("Retry when the gateway is up", f"{COMMAND} tailscale trust")
    if was_running and not was_configured:
        _run_proxy("up", already_running=True, https_enabled=True)
        success("Trusted HTTPS is enabled for the running hub.")
    elif was_running:
        success("The hub was already configured for HTTPS.")
    else:
        success("Trusted HTTPS is configured.")
        action("Start the hub", f"{COMMAND} hub up")


@trust.command("remove")
def trust_remove() -> None:
    """Remove the managed root and disable HTTPS."""
    title()
    _remove_trust()


@trust.command("status")
def trust_status() -> None:
    """Show the managed public-root state without changing it."""
    title()
    _trust_status()


def _remove_trust() -> None:
    """Disable HTTPS before removing the managed development roots."""
    was_configured = _https_configured()
    was_running = proxy_is_running()
    tailnet_active = _tailnet_forces_https()
    if was_running and was_configured and not tailnet_active:
        # With tailnet hosting on, the hub must keep serving HTTPS, so a
        # downgrade recreate would be silently overridden anyway.
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
            ZenNssInstaller(certificate_path, scope="localhost").uninstall()
            MkcertInstaller(certificate_path).uninstall()
        except TrustError as exc:
            raise click.ClickException(str(exc)) from exc
    for tailnet_path in sorted(_state_directory().glob("tailscale-*-rootCA.pem")):
        try:
            ZenNssInstaller(
                tailnet_path, scope=_tailnet_root_scope(tailnet_path)
            ).uninstall()
            MkcertInstaller(tailnet_path).uninstall()
        except TrustError as exc:
            raise click.ClickException(str(exc)) from exc
        tailnet_path.unlink(missing_ok=True)
    if was_running and was_configured and not tailnet_active:
        success("HTTPS is disabled and the local root was removed from managed stores.")
    else:
        success("The local root was removed from managed stores.")
    if tailnet_active:
        info(
            "Tailnet hosting keeps the hub serving HTTPS; run "
            f"`{COMMAND} tailscale unshare` to turn the mirror off."
        )


def _trust_status() -> None:
    """Show the local HTTPS state without changing trust stores."""
    certificate_path = _public_root_path()
    if not certificate_path.exists():
        details(
            [("HTTPS", "disabled (no local public root has been bootstrapped)")],
        )
        return
    try:
        certificate = PublicCertificate.parse(certificate_path.read_bytes())
    except TrustError as exc:
        raise click.ClickException(f"invalid local public root: {exc}") from exc
    state = "enabled" if _https_configured() else "disabled"
    rows = [
            ("HTTPS", state),
            ("Public root", str(certificate_path)),
            ("Fingerprint", certificate.fingerprint),
            ("Managed stores", "system,nss; Zen profiles when present"),
        ]
    tailnet = load_tailscale_state()
    if tailnet is not None and not tailnet.https:
        rows.append((f"Tailnet .{tailnet.suffix}", "HTTP only (public DNS name)"))
    elif tailnet is not None:
        tailnet_path = _tailnet_root_path(tailnet.suffix)
        tailnet_state = "not installed"
        if tailnet_path.exists():
            try:
                tailnet_certificate = PublicCertificate.parse(tailnet_path.read_bytes())
            except TrustError as exc:
                raise click.ClickException(
                    f"invalid .{tailnet.suffix} public root: {exc}"
                ) from exc
            tailnet_state = tailnet_certificate.fingerprint
        rows.append((f"Tailnet .{tailnet.suffix}", tailnet_state))
    details(rows)


@cli.group(invoke_without_command=True)
@click.pass_context
def tailscale(ctx: click.Context) -> None:
    """Expose the same localghost routes inside a Tailscale tailnet."""
    if ctx.invoked_subcommand is None:
        _tailscale_status()


@tailscale.command("status")
def tailscale_status() -> None:
    """Show saved tailnet hosting state without changing it."""
    _tailscale_status()


def _explicit_tailnet_row(tailnet: str) -> list[tuple[str, str]]:
    """Name the tailnet only when one was chosen; '-' is the credential's own."""
    return [] if tailnet == "-" else [("Tailnet", tailnet)]


def _tailscale_status() -> None:
    title()
    try:
        state = load_tailscale_state()
    except TailscaleError as exc:
        raise click.ClickException(str(exc)) from exc
    if state is None:
        details([("Tailnet sharing", "off")])
        return
    details(
        [("Tailnet sharing", "on")]
        + _explicit_tailnet_row(state.tailnet)
        + [
            ("Route suffix", f".{state.suffix}"),
            ("Hub page", f"http://localghost.{state.suffix}"),
            ("Gateway", ", ".join(state.gateway_ips)),
            ("Gateway health", _tailscale_gateway_health()),
            ("Device tag", state.tag),
        ]
        + (
            [("Localhost-only routers", ", ".join(unmirrored))]
            if (unmirrored := _unmirrored_router_names())
            else []
        ),
    )
    if not state.https:
        _public_suffix_notice(state.suffix)
        return
    root_path = _tailnet_root_path(state.suffix)
    if root_path.is_file():
        with suppress(TrustError):
            certificate = PublicCertificate.parse(root_path.read_bytes())
            _share_trust(state.suffix, certificate)


def _tailscale_gateway_health() -> str:
    """The gateway container's observed state, from its Docker healthcheck."""
    return _service_health("tailscale-gateway")


def _only_the_gateway_failed() -> bool:
    """After a failed up: the local hub is healthy, only the mirror is not."""
    return (
        _service_health("traefik") == "healthy"
        and _service_health("tailscale-gateway") != "healthy"
    )


def _service_health(service: str) -> str:
    """A hub service's observed state, from its Docker healthcheck."""
    command = [
        "docker",
        "ps",
        "--filter",
        "label=com.docker.compose.project=localghost",
        "--filter",
        f"label=com.docker.compose.service={service}",
        "--format",
        "{{.Status}}",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return "unknown"
    if result.returncode:
        return "unknown"
    status = result.stdout.strip()
    if not status:
        return "not running"
    if "(health: starting)" in status:
        return "starting"
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(healthy)" in status:
        return "healthy"
    return "running"


_UNMIRRORED = re.compile(r"router (\S+) cannot be mirrored")


def _unmirrored_router_names() -> list[str]:
    """Router names the tailnet CA provider reported it could not mirror.

    The provider can only log the routers it skips; reading Traefik's recent
    logs is the one place that turns "works on localhost, 404 on the tailnet"
    into a visible status row.
    """
    identify = [
        "docker",
        "ps",
        "--filter",
        "label=com.docker.compose.project=localghost",
        "--filter",
        "label=com.docker.compose.service=traefik",
        "--format",
        "{{.ID}}",
    ]
    try:
        result = subprocess.run(identify, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return []
    container = result.stdout.strip().splitlines()[0:1] if not result.returncode else []
    if not container:
        return []
    logs = subprocess.run(
        ["docker", "logs", "--tail", "500", container[0]],
        check=False,
        capture_output=True,
        text=True,
    )
    if logs.returncode:
        return []
    names: list[str] = []
    for match in _UNMIRRORED.finditer(logs.stderr + logs.stdout):
        if match.group(1) not in names:
            names.append(match.group(1))
    return names


@tailscale.command("share")
@click.option(
    "tailnet",
    "--tailnet",
    default="-",
    show_default=True,
    help="Tailnet name; '-' selects the credential's own tailnet.",
)
@click.option("suffix", "--suffix", help="One-label route suffix, such as tail1234.")
@click.option("tag", "--tag", default="tag:localghost", show_default=True)
@click.option(
    "takeover",
    "--takeover",
    is_flag=True,
    help="Replace an existing split-DNS mapping for the suffix.",
)
@click.option("client_id", "--client-id", envvar="TAILSCALE_CLIENT_ID")
@click.option(
    "client_secret",
    "--client-secret",
    envvar="TAILSCALE_CLIENT_SECRET",
)
def tailscale_share(
    tailnet: str,
    suffix: str | None,
    tag: str,
    takeover: bool,
    client_id: str | None,
    client_secret: str | None,
) -> None:
    """Share this machine's hub on the tailnet under a route suffix.

    Enrolls a tagged gateway device and points split DNS for the suffix at
    it. When the hub is already shared, a different suffix, given or
    detected, moves the routes to it, keeping the same gateway device.
    """
    title()
    current = load_tailscale_state()
    if current is not None:
        _switch_tailnet_suffix(current, suffix, takeover, client_id, client_secret)
        return
    localhost_trusted = _https_configured()
    if not tag.startswith("tag:"):
        raise click.UsageError("--tag must start with 'tag:'")
    client_id, client_secret = _resolve_tailscale_credential(
        client_id, client_secret, tag=tag
    )
    try:
        chosen_suffix = validate_tailscale_suffix(
            suffix or detect_tailscale_suffix()
        )
        https = not tailscale_suffix_is_public(chosen_suffix)
        node = default_tailscale_node()
        details(
            _explicit_tailnet_row(tailnet)
            + [
                ("Route suffix", f".{chosen_suffix}"),
                ("Gateway", f"localghost-{node}"),
                ("Device tag", tag),
            ],
            title="Tailnet hosting",
        )
        if not https:
            _public_suffix_notice(chosen_suffix)
        info("Authenticating with the Tailscale API…")
        api = TailscaleAPI.authenticate(client_id, client_secret)
        info("Reading the existing split-DNS configuration…")
        previous = api.split_dns(tailnet)
        _refuse_active_mapping(chosen_suffix, previous, takeover)
        certificate = _prepare_tailnet_authority(chosen_suffix) if https else None
        # The auth key lives ten minutes; the first gateway image build can
        # take longer, so build before minting the key rather than after.
        info("Preparing the gateway image…")
        _ensure_gateway_image(chosen_suffix)
        info("Creating a single-use gateway auth key…")
        auth_key = api.create_auth_key(tailnet, tag)
        info(f"Enrolling localghost-{node} in the tailnet…")
        gateway_ips = _bootstrap_tailscale_gateway(chosen_suffix, node, auth_key)
        state = TailscaleState(
            tailnet=tailnet,
            suffix=chosen_suffix,
            gateway_ips=gateway_ips,
            previous_split_dns=previous,
            tag=tag,
            node=node,
        )
        save_tailscale_state(state)
        try:
            info(f"Routing *.{chosen_suffix} DNS to the gateway…")
            api.update_split_dns(tailnet, {chosen_suffix: list(gateway_ips)})
            info("Starting the mirrored localhost and tailnet routes…")
            _run_proxy(
                "up", https_enabled=https or localhost_trusted, force_recreate=True
            )
        except Exception:
            _rollback_enable(api, tailnet, chosen_suffix, previous)
            raise
    except (TailscaleError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    _remember_tailscale_credential(client_id, client_secret)
    success(f"Shared on the tailnet at {_tailnet_url(state, '<project>')}.")
    _offer_tailnet_trust(chosen_suffix, certificate, localhost_trusted)


def _switch_tailnet_suffix(
    current: TailscaleState,
    suffix: str | None,
    takeover: bool,
    client_id: str | None,
    client_secret: str | None,
) -> None:
    """Move the tailnet routes to another suffix on the same gateway device.

    The gateway keeps its enrollment, addresses, tailnet and tag: only the
    split-DNS entries move, the suffix's HTTPS root is prepared, and the hub is
    recreated to answer for the new names.
    """
    try:
        # Without --suffix, detection runs again, so renaming the machine in
        # the admin console carries its routes to the new name.
        chosen_suffix = validate_tailscale_suffix(
            suffix or detect_tailscale_suffix()
        )
    except (TailscaleError, ValueError) as exc:
        if suffix:
            raise click.ClickException(str(exc)) from exc
        chosen_suffix = current.suffix
    if chosen_suffix == current.suffix:
        raise click.ClickException(
            f"the hub is already shared at .{current.suffix}; pass "
            "--suffix to move it to another suffix"
        )
    old_suffix = current.suffix
    localhost_trusted = _https_configured()
    client_id, client_secret = _resolve_tailscale_credential(
        client_id, client_secret, tag=current.tag
    )
    https = not tailscale_suffix_is_public(chosen_suffix)
    details(
        [
            ("Route suffix", f".{old_suffix} → .{chosen_suffix}"),
            ("Gateway", f"localghost-{current.node_label} (unchanged)"),
        ],
        title="Switching tailnet suffix",
    )
    if not https:
        _public_suffix_notice(chosen_suffix)
    try:
        info("Authenticating with the Tailscale API…")
        api = TailscaleAPI.authenticate(client_id, client_secret)
        info("Reading the existing split-DNS configuration…")
        previous = api.split_dns(current.tailnet)
        _refuse_active_mapping(chosen_suffix, previous, takeover)
        certificate = _prepare_tailnet_authority(chosen_suffix) if https else None
        state = replace(
            current,
            suffix=chosen_suffix,
            previous_split_dns=previous,
            node=current.node_label,
        )
        save_tailscale_state(state)
        try:
            info(f"Routing *.{chosen_suffix} DNS to the gateway…")
            api.update_split_dns(
                current.tailnet,
                {
                    chosen_suffix: list(current.gateway_ips),
                    old_suffix: current.previous_split_dns.get(old_suffix),
                },
            )
            info("Starting the mirrored localhost and tailnet routes…")
            _run_proxy(
                "up", https_enabled=https or localhost_trusted, force_recreate=True
            )
        except Exception:
            _rollback_switch(api, current, chosen_suffix, previous)
            raise
    except (TailscaleError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    _remember_tailscale_credential(client_id, client_secret)
    success(f"Tailnet routes moved to {_tailnet_url(state, '<project>')}.")
    if current.https:
        info(
            f"Machines that trusted the .{old_suffix} root still trust it; run "
            f"`{COMMAND} trust remove` on each to revoke."
        )
    _offer_tailnet_trust(chosen_suffix, certificate, localhost_trusted)


def _refuse_active_mapping(
    suffix: str, split_dns: dict[str, list[str]], takeover: bool
) -> None:
    active = split_dns.get(suffix)
    if active and not takeover:
        raise click.ClickException(
            f"split DNS for .{suffix} already points at "
            f"{', '.join(active)} — another machine may be hosting this "
            "suffix. Choose a different --suffix, or pass --takeover to "
            f"replace the mapping. Split DNS is listed at {DNS_CONSOLE_URL}."
        )


def _prepare_tailnet_authority(suffix: str) -> PublicCertificate:
    # Tailnet TLS terminates on Traefik's websecure entrypoint. Bootstrap its
    # localhost signer as well, but do not install localhost trust as an
    # implicit side effect of enabling a remote route.
    info("Preparing the localhost and tailnet HTTPS authorities…")
    _bootstrap_public_root()
    certificate = _bootstrap_tailnet_root(suffix)
    # Keep the tailnet root beside the state so `tailscale status` can repeat
    # the share command without a running gateway.
    _write_public_root(_tailnet_root_path(suffix), certificate.pem)
    return certificate


def _remember_tailscale_credential(client_id: str, client_secret: str) -> None:
    if not store_tailscale_credential(client_id, client_secret):
        warning(
            "The OAuth credential was not stored",
            ["no usable system keyring; unshare will ask for the credential again"],
        )


def _offer_tailnet_trust(
    suffix: str, certificate: PublicCertificate | None, localhost_trusted: bool
) -> None:
    if certificate is None:
        return
    _share_trust(suffix, certificate)
    if localhost_trusted:
        info("Installing the tailnet HTTPS root (sudo may be requested)…")
        try:
            _install_tailnet_trust(suffix, show_details=False)
        except click.ClickException as exc:
            warning("Tailnet trust was not installed", [str(exc)])
            action("Trust this client", f"{COMMAND} trust")
        else:
            success("Tailnet HTTPS is trusted on this client.")
    else:
        action("Trust localhost and tailnet HTTPS", f"{COMMAND} trust")


def _resolve_tailscale_credential(
    client_id: str | None, client_secret: str | None, *, tag: str | None = None
) -> tuple[str, str]:
    """Find the OAuth credential: options, then keyring, then a guided prompt.

    With a tag, the prompt follows share's full admin-console setup; without
    one it only names the scope unshare uses.
    """
    if not client_id and not client_secret:
        stored = load_tailscale_credential()
        if stored is not None:
            info("Using the OAuth credential stored in the system keyring.")
            return stored.client_id, stored.client_secret
        if tag is not None:
            client = "Add OAuth credential"
            if tag_in_use(tag):
                # A device already carries the tag, so only the client is left.
                success(f"The {tag} device tag is already allowed on the tailnet.")
                steps = [(client, OAUTH_CONSOLE_URL)]
                indent = ""
            else:
                steps = [
                    ("1. Allow the device tag", POLICY_CONSOLE_URL),
                    (
                        "   Add to the policy file",
                        f'"tagOwners": {{"{tag}": ["autogroup:admin"]}}',
                    ),
                    (f"2. {client}", OAUTH_CONSOLE_URL),
                ]
                indent = "   "
            details(steps)
            styled(
                [
                    (f"{indent}with scopes ", "default"),
                    ("DNS: Write", MINT),
                    (", ", "default"),
                    ("Auth Keys: Write", MINT),
                    (" (", "default"),
                    (tag, MINT),
                    (")", "default"),
                ]
            )
        else:
            details(
                [("OAuth credential with DNS: Write", OAUTH_CONSOLE_URL)],
                title="No OAuth credential is stored in the system keyring",
            )
    if not client_id:
        client_id = click.prompt("Client ID")
    if not client_secret:
        client_secret = click.prompt("Client secret", hide_input=True)
    return client_id, client_secret


@tailscale.command("unshare")
@click.option("client_id", "--client-id", envvar="TAILSCALE_CLIENT_ID")
@click.option(
    "client_secret",
    "--client-secret",
    envvar="TAILSCALE_CLIENT_SECRET",
)
@click.option(
    "forget_credential",
    "--forget-credential",
    is_flag=True,
    help="Also remove the stored OAuth credential; works once unshared too.",
)
def tailscale_unshare(
    client_id: str | None, client_secret: str | None, forget_credential: bool
) -> None:
    """Stop sharing the hub: restore prior tailnet DNS and stop the gateway.

    The OAuth credential stays in the system keyring, so sharing again needs
    no setup; --forget-credential removes it.
    """
    title()
    try:
        state = load_tailscale_state()
    except TailscaleError as exc:
        raise click.ClickException(str(exc)) from exc
    if state is None:
        if forget_credential:
            _forget_tailscale_credential()
            return
        raise click.ClickException("the hub is not shared on the tailnet")
    client_id, client_secret = _resolve_tailscale_credential(client_id, client_secret)
    try:
        api = TailscaleAPI.authenticate(client_id, client_secret)
        previous = state.previous_split_dns.get(state.suffix)
        api.update_split_dns(state.tailnet, {state.suffix: previous})
        remove_tailscale_state()
        if proxy_is_running():
            _run_proxy(
                "up",
                already_running=True,
                https_enabled=_https_configured(),
                force_recreate=True,
            )
    except TailscaleError as exc:
        raise click.ClickException(str(exc)) from exc
    success("Tailnet DNS was restored and the local gateway was removed.")
    if forget_credential:
        _forget_tailscale_credential()
    elif load_tailscale_credential() is not None:
        info(
            "The OAuth credential stays in the system keyring for the next "
            f"share; run `{COMMAND} tailscale unshare --forget-credential` to "
            "remove it."
        )
    info(
        f"Machines that trusted the .{state.suffix} root still trust it; run "
        f"`{COMMAND} trust remove` on each to revoke."
    )
    info(
        f"Remove the offline tagged localghost-{state.node_label} device at "
        f"{MACHINES_CONSOLE_URL}."
    )


def _forget_tailscale_credential() -> None:
    if delete_tailscale_credential():
        info("The OAuth credential was removed from the system keyring.")
        info(
            "The OAuth credential still exists in Tailscale; revoke it on the Trust "
            f"credentials page ({OAUTH_CONSOLE_URL}) if no other machine uses it."
        )
    else:
        info("No OAuth credential was stored in the system keyring.")


tailscale.add_command(
    click.Command(
        "enable",
        callback=tailscale_share.callback,
        params=tailscale_share.params,
        help=tailscale_share.help,
        hidden=True,
        deprecated=f"Use '{COMMAND} tailscale share' instead.",
    )
)
tailscale.add_command(
    click.Command(
        "disable",
        callback=tailscale_unshare.callback,
        params=tailscale_unshare.params,
        help=tailscale_unshare.help,
        hidden=True,
        deprecated=f"Use '{COMMAND} tailscale unshare' instead.",
    )
)


@tailscale.command("trust")
@click.argument("target", required=False)
@click.option(
    "fingerprint",
    "--fingerprint",
    help=(
        "Install the root only when it has this SHA-256 fingerprint, as shown "
        "by `localghost tailscale status` on the hosting machine. TARGET is "
        "then the suffix, and no tailscale CLI is needed."
    ),
)
def tailscale_trust(target: str | None, fingerprint: str | None) -> None:
    """Trust a tailnet hub's HTTPS root on this client.

    TARGET names the hub by suffix, machine, or gateway device. Without
    --fingerprint the hub is found in this client's Tailscale network map, so
    the tailnet itself vouches for the root. With no TARGET, the one hub on the
    tailnet is trusted, or on a hosting machine its own.
    """
    title()
    state = load_tailscale_state()
    if state is not None and target in (None, state.suffix):
        if not state.https:
            raise click.ClickException(
                f"tailnet routes for .{state.suffix} are HTTP only, so there "
                "is no root to trust"
            )
        _install_tailnet_trust(state.suffix, expected_fingerprint=fingerprint)
    elif fingerprint is not None:
        if target is None:
            raise click.UsageError("name the suffix to trust with --fingerprint")
        _install_tailnet_trust(target, expected_fingerprint=fingerprint)
    else:
        _trust_tailnet_hub(target)


def _trust_tailnet_hub(target: str | None) -> None:
    """Find a hub in the network map and trust the root it serves."""
    hubs = tailnet_hubs()
    if hubs is None:
        if target is None:
            raise click.ClickException(
                "the tailscale CLI is unavailable to find a hub; name the suffix "
                "and pass --fingerprint"
            )
        warning(
            f"The .{target} root is not verified by the tailnet",
            [
                "the tailscale CLI is unavailable, so the download trusts DNS",
                "pass --fingerprint to pin it",
            ],
        )
        _install_tailnet_trust(target)
        return
    described: list[TailnetHub] = []
    unreachable: list[str] = []
    for hub in hubs:
        try:
            described.append(describe_tailnet_hub(hub))
        except TailscaleError as exc:
            unreachable.append(str(exc))
    matches = [hub for hub in described if target is None or hub.answers_to(target)]
    if len(matches) == 1:
        hub = matches[0]
        try:
            certificate = PublicCertificate.parse(hub.root)
        except TrustError as exc:
            raise click.ClickException(str(exc)) from exc
        _install_tailnet_trust(hub.suffix, certificate=certificate, verified_by=hub)
        return
    if matches:
        message = "more than one hub matches; name one as TARGET"
    elif target:
        message = f"no online localghost hub answers to {target}"
    else:
        message = "no online localghost hub was found in this tailnet"
    found = [f"{hub.label} (.{hub.suffix})" for hub in described] + unreachable
    raise click.ClickException("\n  ".join([message, *found]))


def _share_trust(suffix: str, certificate: PublicCertificate) -> None:
    """Show another tailnet machine how to trust this hub's root."""
    action(
        "Trust on other machines",
        f"uvx localghost tailscale trust {suffix}",
    )
    # The fingerprint is for clients that cannot ask Tailscale for the hub;
    # the plain command above is the way forward for everyone else.
    info(
        [
            ("Without the tailscale CLI, add ", "default"),
            (f"--fingerprint {certificate.fingerprint}", LIME),
            (" to pin the root.", "default"),
        ]
    )


def _rollback_enable(
    api: TailscaleAPI, tailnet: str, suffix: str, previous: dict[str, list[str]]
) -> None:
    """Return to the pre-enable state after a late enable failure.

    The suffix's split-DNS entry goes back to its previous value and the
    saved state is removed so `share` can simply be run again. The enrolled
    gateway device stays: retrying reuses its persisted node state, and
    giving up only needs the admin console.
    """
    try:
        api.update_split_dns(tailnet, {suffix: previous.get(suffix)})
    except TailscaleError as exc:
        warning("Tailnet DNS was not restored", [str(exc)])
    remove_tailscale_state()
    warning(
        "Sharing failed and was rolled back",
        [f"run `{COMMAND} tailscale share` again to retry .{suffix}"],
    )


def _rollback_switch(
    api: TailscaleAPI,
    current: TailscaleState,
    suffix: str,
    previous: dict[str, list[str]],
) -> None:
    """Return the routes to the old suffix after a late switch failure."""
    try:
        api.update_split_dns(
            current.tailnet,
            {
                suffix: previous.get(suffix),
                current.suffix: previous.get(current.suffix),
            },
        )
    except TailscaleError as exc:
        warning("Tailnet DNS was not restored", [str(exc)])
    save_tailscale_state(current)
    warning(
        "The switch failed and was rolled back",
        [
            f"tailnet routes stay on .{current.suffix}",
            f"run `{COMMAND} hub up` to restore them on the running hub",
        ],
    )


def _tailnet_root_path(suffix: str) -> Path:
    return _state_directory() / f"tailscale-{suffix}-rootCA.pem"


def _tailnet_root_scope(path: Path) -> str:
    """Recover the suffix a stored tailnet root belongs to."""
    return path.name.removeprefix("tailscale-").removesuffix("-rootCA.pem")


def _install_tailnet_trust(
    suffix: str,
    *,
    show_details: bool = True,
    expected_fingerprint: str | None = None,
    certificate: PublicCertificate | None = None,
    verified_by: TailnetHub | None = None,
) -> None:
    """Install a tailnet root: `certificate` when a caller already has it,
    otherwise downloaded by suffix."""
    expected = _normalize_fingerprint(expected_fingerprint)
    try:
        require_mkcert()
        suffix = validate_tailscale_suffix(suffix)
        if certificate is None:
            state = load_tailscale_state()
            gateway_ips = (
                state.gateway_ips if state and state.suffix == suffix else ()
            )
            certificate = PublicCertificate.parse(
                fetch_tailscale_root(suffix, gateway_ips)
            )
    except (TailscaleError, TrustError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if expected is not None and expected != certificate.fingerprint:
        raise click.ClickException(
            f"the downloaded .{suffix} root does not match the expected "
            f"fingerprint; it was not installed.\n"
            f"  expected: {expected}\n"
            f"  received: {certificate.fingerprint}"
        )
    path = _tailnet_root_path(suffix)
    known = path.exists()
    replaced = _remove_superseded_root(path, certificate, scope=suffix)
    _write_public_root(path, certificate.pem)
    if show_details:
        details(
            [
                ("Authorization", "sudo password may be requested for system trust"),
                ("Scope", f"development names under .{suffix}"),
                ("Public-root fingerprint", certificate.fingerprint),
                *(
                    [("Verified by", f"tailnet node {verified_by.name}")]
                    if verified_by
                    else []
                ),
                ("Private keys", "remain on the hosting machine"),
            ],
            title="Tailnet HTTPS setup",
        )
    try:
        MkcertInstaller(path).install()
        ZenNssInstaller(path, scope=suffix).install()
    except TrustError as exc:
        raise click.ClickException(str(exc)) from exc
    if replaced:
        success(
            f"Replaced the previous .{suffix} root; this client now trusts the "
            "current one."
        )
    elif known:
        success(f"Already trusted .{suffix}; refreshed this client's trust stores.")
    else:
        success(f"Trusted localghost HTTPS for *.{suffix} on this client.")


def _normalize_fingerprint(value: str | None) -> str | None:
    """Accept the spellings a fingerprint survives being pasted in."""
    if value is None:
        return None
    digest = value.strip().upper().removeprefix("SHA256:").replace(":", "")
    if not re.fullmatch(r"[0-9A-F]{64}", digest):
        raise click.UsageError(
            "--fingerprint must be 64 hexadecimal characters, optionally "
            "prefixed with SHA256: or separated by colons"
        )
    return f"SHA256:{digest}"


def _remove_superseded_root(
    path: Path, certificate: PublicCertificate, *, scope: str
) -> bool:
    """Take a replaced root out of the trust stores before it is overwritten.

    mkcert and certutil both identify an installed authority by the
    certificate they are given, so a rotated root can only be removed while
    its own bytes are still on disk. Failing to remove it must not block
    trusting the current one, or the client keeps only the dead root.
    """
    if not path.exists():
        return False
    try:
        previous = PublicCertificate.parse(path.read_bytes())
    except TrustError:
        previous = None
    if previous is not None and previous.fingerprint == certificate.fingerprint:
        return False
    try:
        ZenNssInstaller(path, scope=scope).uninstall()
        MkcertInstaller(path).uninstall()
    except TrustError as exc:
        warning(
            f"The superseded .{scope} root was left in the trust stores",
            [str(exc), "Remove it with this client's trust-store tooling."],
        )
    return True


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
    "root",
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    help="Treat this directory as the project root instead of searching.",
)
@click.option(
    "port", "--port", "-p", type=click.IntRange(1, 65535), help="Host HTTP port."
)
@click.option(
    "detach",
    "--detach",
    is_flag=True,
    help="Run in the background and manage it later.",
)
@click.option(
    "config",
    "--config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Run configuration TOML path.",
)
@click.option(
    "isolate",
    "--isolate",
    is_flag=True,
    help="Run in its own network namespace so it keeps its port (Linux, pasta).",
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
    root: Path | None,
    port: int | None,
    detach: bool,
    config: Path | None,
    isolate: bool,
    dry_run: bool,
    no_status_bar: bool,
    command: tuple[str, ...],
) -> None:
    """Run a configured host or Compose application behind the hub."""
    flags = registry.RunFlags(
        type=selected_type,
        root=str(root.resolve()) if root else None,
        port=port,
        config=str(config.resolve()) if config else None,
        isolate=isolate,
        command=command,
    )
    resolved = _resolve_application(
        working_directory=working_directory,
        name=name,
        selected_type=selected_type,
        root=root,
        port=port,
        config=config,
        command=command,
        isolate=isolate,
    )
    if resolved.selected_type == "compose":
        # Compose owns the application's configuration; --project-root is
        # orthogonal and stays allowed.
        if resolved.command or resolved.port is not None or isolate:
            raise click.ClickException(
                "compose does not accept a host command, --port or --isolate; "
                "Compose owns them"
            )
        project = resolved.name or _local_project_name(resolved.root)
        _check_compose_routing(resolved.root, project)
        if dry_run:
            compose_dry_run(
                project=project,
                url=_proxy_origin(project),
                tailnet_url=_tailnet_origin(project, warn=True),
            )
            return
        # Same guard as the host branch below: a second run of a stack that
        # is already running would add a second session record to the same
        # containers, and stopping either would take the whole stack down.
        if _already_serving(
            project, resolved.root, refuse=resolved.explicit_run_settings
        ):
            return
        reap_sessions()
        clean_sessions(project)
        _record_registry(resolved, detached=detach, flags=flags)
        _run_compose(
            resolved.root,
            resolved.name,
            detach,
            status_bar=not no_status_bar,
        )
        return
    plan = resolved.plan
    assert plan is not None
    if _already_serving(
        plan.name,
        plan.project_root or resolved.cwd,
        refuse=resolved.explicit_run_settings,
    ):
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
    reap_sessions()
    clean_sessions(plan.name)
    _record_registry(resolved, detached=detach, flags=flags)
    if detach:
        _detach_host(plan, resolved.cwd)
        return
    # The record lets other terminals see this run, and stop or take it over.
    session = create_session(
        mode="host",
        name=plan.name,
        port=plan.port,
        cwd=plan.project_root or resolved.cwd,
        command=plan.command,
        log=None,
        pid=os.getpid(),
        bridge_project=plan.project,
        bridge_yaml=plan.bridge_yaml,
        detached=False,
        isolated=plan.app_port is not None,
    )
    try:
        status = execute(
            plan,
            lambda: _run_proxy("up", https_enabled=_https_configured()),
            cwd=plan.working_directory or resolved.cwd,
            public_origin=_proxy_origin(plan.name),
            secondary_origin=_tailnet_origin(plan.name),
            status_bar=not no_status_bar,
        )
    finally:
        discard_session(session)
    _report_application_exit(plan.name, status)
    if status:
        raise click.exceptions.Exit(status)


def _already_serving(name: str, directory: Path, *, refuse: bool) -> bool:
    """Report a live session already serving `name`; True if one is.

    The same name from another directory (a second worktree, say) would
    fight the first for NAME.localhost, so that is always refused.
    """
    session = live_session(name)
    if session is None:
        return False
    if Path(session.cwd).resolve() != directory.resolve():
        raise click.ClickException(
            f"{name}.localhost is already served from {session.cwd}; run this "
            "copy under another name with --name, or stop that one with: "
            f"{COMMAND} sessions stop {name}"
        )
    _report_live_session(session, refuse=refuse)
    return True


def _report_live_session(session: Session, *, refuse: bool) -> None:
    """Point at the session already serving this project.

    Explicit run settings mean the user wanted something other than what
    is running, so that is an error; a plain `run` is just informed.
    """
    name = session.name
    message = (
        f"{name} is already running; restart it with: {COMMAND} restart "
        f"{name}, view logs with: {COMMAND} sessions logs {name}, or stop "
        f"it with: {COMMAND} sessions stop {name}"
    )
    if refuse:
        raise click.ClickException(message)
    click.echo(message)


def _resolve_application(
    *,
    working_directory: Path | None,
    name: str | None,
    selected_type: str | None,
    root: Path | None,
    port: int | None,
    config: Path | None,
    command: tuple[str, ...],
    isolate: bool = False,
    local_config: bool = True,
) -> ResolvedApplication:
    """Resolve the effective application once for running and persistence."""
    cwd = working_directory or Path.cwd()
    requested_type = selected_type
    root_from_flag = root is not None
    flagged_root = (
        resolve_root(start=cwd, flag=root, configured=None, config_dir=None)
        if root_from_flag
        else None
    )
    config_file = config or discover_config(flagged_root or cwd)
    settings = (
        load_config(config_file, local=local_config) if config_file else RunConfig()
    )
    config_dir = config_file.parent if config_file else None
    explicit_run_settings = (
        bool(command)
        or isolate
        or any(value is not None for value in (name, selected_type, port, config))
    )
    command = command or settings.command
    name = name or settings.name
    selected_type = selected_type or settings.type
    port = port or settings.port
    if command and selected_type in {"compose", "dockerfile"}:
        raise click.ClickException(
            f"a command cannot be combined with --type {selected_type}"
        )
    pinned = flagged_root or resolve_root(
        start=cwd, flag=None, configured=settings.root, config_dir=config_dir
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
        isolate=isolate or settings.isolate,
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
        # Where the hub's bridge cannot reach loopback but a relay can, a
        # leader stays behind to relay a loopback-bound application, which a
        # foreground run would otherwise do itself. Elsewhere the command
        # runs on its own, as nothing would be left for a leader to do.
        command = list(plan.launch)
        if forwarder.available():
            mode = "shared" if plan.app_port is None else "isolated"
            command = [
                sys.executable, "-I", "-m", "localghost.detached",
                str(plan.port), mode, *command,
            ]  # fmt: skip
        with log.open("ab") as handle:
            # `start_new_session` makes the child a process-group leader, which
            # is what lets `sessions.stop` signal the group by its recorded pid.
            child = subprocess.Popen(
                command,
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
                isolated=plan.app_port is not None,
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
    than the process's own working directory, for `-C`/`--project-root` runs.
    """
    compose_file = _compose_file(compose_root)
    problem = routing_problem(resolve_compose((), cwd=compose_root))
    if problem is None:
        return
    label = compose_file.name if compose_file else "the Compose project"
    raise click.ClickException(
        f"found {label} but {problem}, so nothing would be reachable at "
        f"{_proxy_origin(project)}; run {COMMAND} save --run to save the "
        "routing setup and start it"
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
    detachable = not detach and detach_key_available()
    # A foreground stack is recorded for as long as `up` holds the terminal.
    foreground = (
        None
        if detach
        else create_session(
            mode="compose",
            name=project,
            port=0,
            cwd=cwd,
            command=(),
            log=None,
            pid=os.getpid(),
            project=project,
            detached=False,
        )
    )
    with statusbar.pinned(
        public_origin,
        secondary_url=_tailnet_origin(project),
        # A detached `up` returns immediately, so there is no foreground
        # lifetime for a bar to span.
        enabled=status_bar and not detach,
        # Compose owns its own ports, so readiness has to be asked of the
        # route itself rather than of a port we chose.
        probe=statusbar.http_probe(public_origin),
        message="starting hub",
        detachable=detachable,
    ) as bar:
        # The hub has to be reconciled before the application comes up,
        # otherwise a foreground `compose up` blocks before anything can
        # route to it.
        _run_proxy("up", https_enabled=_https_configured())
        info(f"Public URL: {public_origin}")
        _report_tailnet_origin(project)
        if detachable:
            info("Press Ctrl+Z to detach and leave it running.")
        bar.status("starting")
        status: int | None
        try:
            status = (
                subprocess.run(command, cwd=cwd, check=False).returncode
                if detach
                else attached_compose_up(command, cwd=cwd)
            )
        finally:
            if foreground is not None:
                discard_session(foreground)
        if status is None:
            # Done here, not after the bar: if the terminal is what went away,
            # releasing the bar can fail, and this must already have happened.
            with suppress(OSError):
                bar.status("detaching")
            problem = _settle_detached_compose(cwd, project, command)
    if status is None:
        _report_detached_compose(project, public_origin, problem)
        return
    if status:
        _report_application_exit(project, status)
        raise click.exceptions.Exit(status)
    if detach:
        _record_detached_compose(cwd, project)
        success(f"Started detached Compose session for {project}.")


def _record_detached_compose(cwd: Path, project: str) -> None:
    create_session(
        mode="compose",
        name=project,
        port=0,
        cwd=cwd,
        command=(),
        log=_session_log_path(project),
        pid=None,
        project=project,
    )


def _settle_detached_compose(
    cwd: Path, project: str, command: list[str]
) -> str | None:
    """Hand a stack whose client has gone over to the background.

    `up --detach` finishes whatever the client was interrupted in the middle
    of. The record comes first so the stack stays manageable if that fails.
    """
    _record_detached_compose(cwd, project)
    registry.update(project, detached=True)
    settled = subprocess.run(
        [*command, "--detach"], cwd=cwd, check=False, capture_output=True, text=True
    )
    return settled.stderr.strip() if settled.returncode else None


def _report_detached_compose(
    project: str, public_origin: str, problem: str | None
) -> None:
    # A closed terminal is one way to get here, and has nowhere to report to.
    with suppress(OSError):
        sign_off()
        success(f"Detached from {project}; it keeps running at {public_origin}.")
        info(f"Follow its output: {COMMAND} sessions logs {project} -f")
        info(f"Stop it: {COMMAND} sessions stop {project}")
        if problem:
            warning("Compose could not finish starting the stack", [problem])


def _report_application_exit(name: str, status: int) -> None:
    """Leave a stable diagnosis after the transient status display is gone."""
    sign_off()
    if status in (0, 128 + signal.SIGINT):
        # Ctrl+C is how a foreground run is meant to end, not a failure.
        success(f"Stopped {name}.")
        return
    failure(f"{name} exited with status {status}.")


def _print_run_plan(plan: RunPlan, dry_run: bool, detach: bool = False) -> None:
    public_origin = _proxy_origin(plan.name)
    run_plan(
        name=plan.name,
        type=plan.type,
        command=plan.command,
        port=plan.port,
        app_port=plan.app_port,
        url=public_origin,
        dry_run=dry_run,
        project_root=plan.project_root,
        working_directory=plan.working_directory,
        tailnet_url=_tailnet_origin(plan.name, warn=True),
    )
    if dry_run:
        # stdout carries only the bridge YAML.
        click.echo(plan.bridge_yaml, nl=False)
    elif detach:
        info(
            "Starting the application in the background; inspect and stop it "
            f"with {COMMAND} sessions."
        )
    else:
        info("Starting the application. Press Ctrl+C to stop it.")


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


def _tailnet_forces_https() -> bool:
    """The hub serves HTTPS whenever a private-suffix tailnet is enabled, even
    after local trust is removed, because tailnet TLS terminates on Traefik.
    A public suffix serves HTTP only and leaves the localhost side alone."""
    with suppress(TailscaleError):
        state = load_tailscale_state()
        return state is not None and state.https
    return False


def _proxy_origin(hostname: str) -> str:
    https_enabled = _https_configured() or _tailnet_forces_https()
    port = _proxy_https_port() if https_enabled else _proxy_http_port()
    default_port = 443 if https_enabled else 80
    suffix = "" if port == default_port else f":{port}"
    scheme = "https" if https_enabled else "http"
    return f"{scheme}://{hostname}.localhost{suffix}"


def _report_tailnet_origin(hostname: str, *, err: bool = False) -> None:
    try:
        state = load_tailscale_state()
    except TailscaleError as exc:
        warning("Tailnet URL unavailable", [str(exc)])
        return
    if state is not None:
        info(f"Tailnet URL: {_tailnet_url(state, hostname)}", err=err)


def _tailnet_origin(hostname: str, *, warn: bool = False) -> str | None:
    """The mirrored tailnet URL, or None when hosting is off or unreadable.

    A run plan warns about unreadable state; the status bar stays quiet.
    """
    try:
        state = load_tailscale_state()
    except TailscaleError as exc:
        if warn:
            warning("Tailnet URL unavailable", [str(exc)])
        return None
    return _tailnet_url(state, hostname) if state is not None else None


def _tailnet_url(state: TailscaleState, hostname: str) -> str:
    scheme = "https" if state.https else "http"
    return f"{scheme}://{hostname}.{state.suffix}"


def _public_suffix_notice(suffix: str) -> None:
    """Explain once per command why a public suffix routes without TLS."""
    warning(
        f"Tailnet routes for .{suffix} are HTTP only",
        [
            f".{suffix} is a public or reserved DNS name, so no root is minted "
            "for it; the tailnet still encrypts traffic, and a private label "
            "such as tail1234 would get trusted HTTPS"
        ],
    )


def _run_proxy(
    action: str,
    *,
    already_running: bool = False,
    https_enabled: bool = False,
    force_recreate: bool = False,
    rebuild: bool = False,
    repair: bool = True,
) -> None:
    try:
        tailscale_state = load_tailscale_state()
    except TailscaleError as exc:
        # Corrupt state must never block a teardown: the overlays are only
        # needed to name services, and `down` removes orphans anyway.
        if action != "down":
            raise click.ClickException(str(exc)) from exc
        warning("Ignoring unreadable tailnet state for teardown", [str(exc)])
        tailscale_state = None
    if tailscale_state is not None and tailscale_state.https:
        https_enabled = True
    with _proxy_resource_directory() as resource_root:
        compose_file = resource_root / "proxy_compose.yaml"
        command = [
            "docker",
            "compose",
            "--project-name",
            PROJECT_NAME,
            "--file",
            str(compose_file),
            action,
        ]
        if https_enabled:
            command[6:6] = ["--file", str(resource_root / "proxy_compose_https.yaml")]
        if tailscale_state is not None:
            insertion = [
                "--file",
                str(resource_root / "proxy_compose_tailscale.yaml"),
            ]
            if tailscale_state.https:
                insertion += [
                    "--file",
                    str(resource_root / "proxy_compose_tailscale_https.yaml"),
                ]
            command[8:8] = insertion
        if action == "down":
            # `bootstrap` sits behind a profile, so a plain `down` leaves its
            # exited container behind and Docker still reports the project as
            # existing. Orphans need naming too: the overlays are only passed
            # when they apply, so a service that left the file set — the
            # gateway, when saved tailnet state is gone — would survive the
            # teardown and hold the project network open.
            command[-1:-1] = ["--profile", "bootstrap"]
            command.append("--remove-orphans")
        if action == "up":
            command.extend(
                ["--detach", "--wait", "--wait-timeout", "60", "--remove-orphans"]
            )
            if force_recreate:
                command.append("--force-recreate")
            # Hub images are tagged with the release version, so re-checking
            # their build every time costs about a second and can change
            # nothing. `--rebuild` covers editing the bundled sources.
            required = [TRAEFIK_IMAGE]
            if tailscale_state is not None:
                required.append(GATEWAY_IMAGE)
            if not rebuild and _images_are_built(*required):
                command.append("--no-build")

        verb = "Reconciling" if already_running else "Starting"
        if action == "down":
            verb = "Stopping"
        info(f"{verb} hub…")
        try:
            environment = os.environ.copy()
            environment["LOCALGHOST_IMAGE_TAG"] = f"v{LOCALGHOST_VERSION}"
            if tailscale_state is not None:
                environment["LOCALGHOST_TAILSCALE_SUFFIX"] = tailscale_state.suffix
                environment["LOCALGHOST_TAILSCALE_NODE"] = tailscale_state.node_label
                # The tailnet overlay owns Traefik's command, so it also has
                # to be told which CA providers have a signer to run with.
                environment["LOCALGHOST_LOCALHOST_CA_MODE"] = (
                    "tls" if https_enabled else "http"
                )
                environment["LOCALGHOST_TAILNET_CA_MODE"] = (
                    "tls" if tailscale_state.https else "http"
                )
            try:
                registry.registry_dir().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # Registry failures must never break a run: leave
                # LOCALGHOST_REGISTRY_DIR unset so compose falls back to its
                # default mount (/tmp/localghost-registry), which stays valid.
                warning("Could not prepare ghost page registry", [str(exc)])
            else:
                environment["LOCALGHOST_REGISTRY_DIR"] = str(registry.registry_dir())
            result = subprocess.run(
                command, check=False, capture_output=True, text=True, env=environment
            )
        except FileNotFoundError as exc:
            raise click.ClickException("docker is required") from exc

    if result.returncode:
        if (
            action == "up"
            and tailscale_state is not None
            and _only_the_gateway_failed()
        ):
            # An unreachable tailnet must not block local development: the
            # gateway container keeps retrying in the background.
            warning(
                "The tailnet gateway is not ready",
                [
                    "local .localhost routes are up; the gateway keeps "
                    "retrying in the background",
                    f"check progress with `{COMMAND} tailscale status`",
                ],
            )
            return
        if (
            action == "up"
            and repair
            and https_enabled
            and _repair_authorities(tailscale_state)
        ):
            _run_proxy(
                action,
                already_running=already_running,
                https_enabled=https_enabled,
                force_recreate=force_recreate,
                rebuild=rebuild,
                repair=False,
            )
            return
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        if detail:
            warning("Hub command failed", [detail])
        raise click.exceptions.Exit(result.returncode)


def _repair_authorities(tailscale_state: TailscaleState | None) -> bool:
    """Re-run the idempotent CA bootstraps after a failed HTTPS start.

    A pruned or half-created signer volume aborts Traefik's CA plugins, and
    nothing else recreates the material outside `trust` and `share`.
    Bootstrap validates a healthy authority untouched, so retrying is safe.
    Returns False when repair itself fails so the original failure is the
    one reported.
    """
    info("Hub start failed; repairing the certificate authorities…")
    try:
        _bootstrap_public_root()
        if tailscale_state is not None and tailscale_state.https:
            _bootstrap_tailnet_root(tailscale_state.suffix)
    except (TailscaleError, click.ClickException):
        return False
    return True


def _bootstrap_tailnet_root(suffix: str) -> PublicCertificate:
    with _proxy_resource_directory() as resource_root:
        files = [
            resource_root / "proxy_compose.yaml",
            resource_root / "proxy_compose_https.yaml",
            resource_root / "proxy_compose_tailscale.yaml",
            resource_root / "proxy_compose_tailscale_https.yaml",
        ]
        # Build progress goes to stdout, which must carry only the public root.
        command = [
            "docker",
            "compose",
            "--progress",
            "quiet",
            "--project-name",
            PROJECT_NAME,
        ]
        for compose_file in files:
            command.extend(["--file", str(compose_file)])
        command.extend(
            [
                "run",
                "--rm",
                "bootstrap",
                "--root-path=/var/lib/localghost-tailnet-root",
                "--signer-path=/var/lib/localghost-tailnet-ca",
                f"--suffix={suffix}",
                "--print-root",
            ]
        )
        environment = os.environ.copy()
        environment["LOCALGHOST_IMAGE_TAG"] = f"v{LOCALGHOST_VERSION}"
        environment["LOCALGHOST_TAILSCALE_SUFFIX"] = suffix
        try:
            result = subprocess.run(
                command, check=False, capture_output=True, env=environment
            )
        except FileNotFoundError as exc:
            raise TailscaleError("docker is required") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise TailscaleError(detail or "could not bootstrap the tailnet CA")
    try:
        return PublicCertificate.parse(result.stdout)
    except TrustError as exc:
        raise TailscaleError(
            f"tailnet CA bootstrap returned invalid data: {exc}"
        ) from exc


def _ensure_gateway_image(suffix: str) -> None:
    """Build the gateway image ahead of enrollment when it is missing."""
    if _images_are_built(GATEWAY_IMAGE):
        return
    with _proxy_resource_directory() as resource_root:
        command = [
            "docker",
            "compose",
            "--project-name",
            PROJECT_NAME,
            "--file",
            str(resource_root / "proxy_compose.yaml"),
            "--file",
            str(resource_root / "proxy_compose_https.yaml"),
            "--file",
            str(resource_root / "proxy_compose_tailscale.yaml"),
            "build",
            "tailscale-gateway",
        ]
        environment = os.environ.copy()
        environment["LOCALGHOST_IMAGE_TAG"] = f"v{LOCALGHOST_VERSION}"
        environment["LOCALGHOST_TAILSCALE_SUFFIX"] = suffix
        try:
            result = subprocess.run(
                command, check=False, capture_output=True, env=environment
            )
        except FileNotFoundError as exc:
            raise TailscaleError("docker is required") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise TailscaleError(detail or "could not build the gateway image")


def _bootstrap_tailscale_gateway(
    suffix: str, node: str, auth_key: str
) -> tuple[str, ...]:
    with _proxy_resource_directory() as resource_root:
        command = [
            "docker",
            "compose",
            "--project-name",
            PROJECT_NAME,
            "--file",
            str(resource_root / "proxy_compose.yaml"),
            "--file",
            str(resource_root / "proxy_compose_https.yaml"),
            "--file",
            str(resource_root / "proxy_compose_tailscale.yaml"),
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "tailscale-gateway",
            f"--suffix={suffix}",
            f"--hostname=localghost-{node}",
            "--state-dir=/var/lib/localghost-tailscale",
            "--bootstrap",
        ]
        environment = os.environ.copy()
        environment["LOCALGHOST_IMAGE_TAG"] = f"v{LOCALGHOST_VERSION}"
        environment["LOCALGHOST_TAILSCALE_SUFFIX"] = suffix
        environment["LOCALGHOST_TAILSCALE_NODE"] = node
        try:
            result = subprocess.run(
                command,
                input=auth_key.encode(),
                check=False,
                capture_output=True,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise TailscaleError("docker is required") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise TailscaleError(detail or "could not enroll the Tailscale gateway")
    addresses = []
    # tsnet logging can mention arbitrary addresses, so only the bootstrap
    # binary's own IPv4=/IPv6= lines count.
    for line in result.stdout.decode(errors="replace").splitlines():
        label, separator, value = line.strip().partition("=")
        if not separator or label not in {"IPv4", "IPv6"}:
            continue
        try:
            addresses.append(str(ipaddress.ip_address(value)))
        except ValueError:
            continue
    if not addresses:
        raise TailscaleError("gateway enrollment returned no Tailscale address")
    return tuple(addresses)


def _state_directory() -> Path:
    return state_directory()


def _record_registry(
    resolved: ResolvedApplication,
    *,
    detached: bool | None = None,
    flags: registry.RunFlags | None = None,
) -> None:
    """Remember this project so the hub can serve its ghost page later.

    `detached` is how it is being started and `flags` what `run` was told; a
    save passes neither.
    """
    if resolved.selected_type == "compose":
        registry.record(
            resolved.name or _local_project_name(resolved.root),
            resolved.root,
            "compose",
            detached=detached,
            isolated=None if detached is None else False,
            flags=flags,
        )
    elif resolved.plan is not None:
        registry.record(
            resolved.plan.name,
            resolved.cwd,
            resolved.plan.type,
            detached=detached,
            isolated=None if detached is None else resolved.plan.app_port is not None,
            flags=flags,
        )


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
    return _images_are_built(TRAEFIK_IMAGE)


def _images_are_built(*names: str) -> bool:
    """Report whether every named image already exists locally."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", *names],
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
    try:
        require_mkcert()
    except TrustError as exc:
        raise click.ClickException(str(exc)) from exc
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
    zen_installer = ZenNssInstaller(certificate_path, scope="localhost")
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
            # Build progress goes to stdout, which must carry only the root.
            "--progress",
            "quiet",
            "--project-name",
            PROJECT_NAME,
            "--file",
            str(resource_root / "proxy_compose.yaml"),
            "--file",
            str(resource_root / "proxy_compose_https.yaml"),
            "run",
            "--rm",
            "bootstrap",
            "--print-root",
        ]
        environment = os.environ.copy()
        environment["LOCALGHOST_IMAGE_TAG"] = f"v{LOCALGHOST_VERSION}"
        try:
            result = subprocess.run(
                command, check=False, capture_output=True, env=environment
            )
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


# Which `save` options each kind of project accepts. `--port`,
# `--project-root`, and the type-neutral flags (-C, --dry-run, --no-input,
# --run) apply everywhere and are not listed. A flag given for the wrong kind
# is a usage error naming the resolved type, so `--help` stays one list.
_SAVE_FLAG_KINDS: dict[str, tuple[str, ...]] = {
    "--file": ("compose",),
    "--service": ("compose", "dockerfile"),
    "--output": ("compose", "dockerfile"),
    "--extend": ("compose", "host"),
    "--local": ("host",),
    "--name": ("host",),
    "--config": ("host",),
    "a trailing command": ("host",),
    "--isolate": ("host",),
}
_SAVE_KIND_LABELS = {
    "compose": "Compose",
    "dockerfile": "Dockerfile",
    "host": "host",
}


def _reject_foreign_save_flags(
    kind: str, label: str, given: dict[str, bool]
) -> None:
    """Refuse options that belong to another kind of project.

    `kind` is the branch `save` resolved to; `label` is what the user
    should hear it called (the detected framework, or "compose").
    """
    for flag, kinds in _SAVE_FLAG_KINDS.items():
        if given.get(flag) and kind not in kinds:
            accepted = " and ".join(_SAVE_KIND_LABELS[item] for item in kinds)
            raise click.UsageError(
                f"{flag} applies to {accepted} projects; this is a {label} project"
            )


def _save_kind(selected_type: str) -> str:
    if selected_type in ("compose", "dockerfile"):
        return selected_type
    return "host"


@cli.command()
@click.option(
    "working_directory",
    "--directory",
    "-C",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Application directory to save (defaults to the current directory).",
)
@click.option(
    "selected_type",
    "--type",
    type=click.Choice(SAVE_TYPES),
    help="Project type; detected from the directory when omitted.",
)
@click.option("name", "--name", help="Public project name used for NAME.localhost.")
@click.option(
    "root",
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    help="Treat this directory as the project root instead of searching.",
)
@click.option(
    "port",
    "--port",
    "-p",
    type=click.IntRange(1, 65535),
    help="HTTP port: on the host for a host run, inside the container for "
    "Compose and Dockerfile projects.",
)
@click.option(
    "config",
    "--config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Run configuration TOML path.",
)
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
    "output",
    "--output",
    "-o",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Compose output path.",
)
@click.option(
    "--extend",
    is_flag=True,
    help="Safely update an existing saved setup without prompting.",
)
@click.option(
    "--local",
    is_flag=True,
    help="Save to .localghost.local.toml, only where it differs from the shared "
    "file.",
)
@click.option(
    "--dry-run", is_flag=True, help="Print the configuration without writing it."
)
@click.option(
    "--no-input", is_flag=True, help="Use detected defaults and never prompt."
)
@click.option(
    "run_after", "--run", is_flag=True, help="Start the application after saving."
)
@click.option(
    "isolate",
    "--isolate",
    is_flag=True,
    help="Run in its own network namespace so it keeps its port (Linux, pasta).",
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def save(
    ctx: click.Context,
    working_directory: Path | None,
    selected_type: str | None,
    name: str | None,
    root: Path | None,
    port: int | None,
    config: Path | None,
    files: tuple[Path, ...],
    service_name: str | None,
    output: Path | None,
    extend: bool,
    local: bool,
    dry_run: bool,
    no_input: bool,
    run_after: bool,
    isolate: bool,
    command: tuple[str, ...],
) -> None:
    """Save Localghost setup for the current application.

    Host applications and custom commands are saved to .localghost.toml
    (or, with --local, the uncommitted .localghost.local.toml),
    Compose projects to compose.override.yaml, and a Dockerfile without
    Compose to a new compose.yaml. An explicit --type is remembered so
    later runs need no flag.
    """
    if selected_type is None and len(command) == 1 and command[0] in SAVE_TYPES:
        # The draft CLI spelled these as subcommands; a bare type name is
        # never a host command worth saving.
        raise click.UsageError(
            f"'{command[0]}' is not a command to save; use --type {command[0]}"
        )
    if command and selected_type in ("compose", "dockerfile"):
        raise click.UsageError(
            f"a command cannot be combined with --type {selected_type}"
        )
    start = working_directory or Path.cwd()
    interactive = _is_interactive(no_input)
    if selected_type is not None:
        resolved_type = selected_type
    elif command:
        resolved_type = "custom"
    elif os.environ.get("COMPOSE_FILE"):
        # Matches `_resolve_application`: COMPOSE_FILE alone selects Compose
        # even with no compose.yaml on disk for `discover_type` to find.
        resolved_type = "compose"
    else:
        resolved_type = _detect_save_type(start, root=root, config=config, local=local)
    kind = _save_kind(resolved_type)
    _reject_foreign_save_flags(
        kind,
        "compose" if kind == "compose" else resolved_type,
        {
            "--file": bool(files),
            "--service": service_name is not None,
            "--output": output is not None,
            "--extend": extend,
            "--local": local,
            "--name": name is not None,
            "--config": config is not None,
            "a trailing command": bool(command),
            "--isolate": isolate,
        },
    )
    if not dry_run:
        title()
    if kind == "compose":
        # A pinned root is the Compose directory, exactly as `run
        # --project-root` treats it; otherwise search for it.
        cwd = (
            resolve_root(start=start, flag=root, configured=None, config_dir=None)
            if root is not None
            else _compose_save_root(start, files)
        )
        if (
            run_after
            and output is not None
            and not _is_auto_loaded_override(cwd, output)
        ):
            # An output Compose does not merge on its own is never part of
            # the project `run` would start: the pair can only ever save
            # successfully and then fail the routing check. Rejecting it
            # here beats that confusing sequence. A different *auto-loaded*
            # override name is fine, though -- Compose merges
            # `docker-compose.override.yml` as readily as
            # `compose.override.yaml`.
            raise click.UsageError(
                "--output cannot be combined with --run unless it names an "
                "override Docker Compose merges by itself ("
                + ", ".join(COMPOSE_OVERRIDE_NAMES)
                + " in the project directory); this output would not be part "
                "of the project this would start"
            )
        _save_compose_project(
            cwd=cwd,
            files=files,
            service_name=service_name,
            port=port,
            output=output,
            extend=extend,
            dry_run=dry_run,
            interactive=interactive,
        )
        if selected_type == "compose" and not files and output is None:
            try:
                _pin_compose_type(
                    cwd, extend=extend, dry_run=dry_run, interactive=interactive
                )
            except click.ClickException as exc:
                warning(
                    "Compose type was not pinned",
                    [
                        "The override was saved without a Compose type pin: "
                        f"{exc.message}"
                    ],
                )
        if not dry_run:
            registry.record(_local_project_name(cwd), cwd, "compose")
        if run_after and not dry_run:
            _run_after_save(ctx, working_directory=cwd, selected_type="compose")
        return
    if kind == "dockerfile":
        _save_dockerfile_project(
            working_directory=working_directory,
            root=root,
            service_name=service_name,
            port=port,
            output=output,
            dry_run=dry_run,
            interactive=interactive,
        )
        if run_after and not dry_run:
            # A compose.yaml was just written, so what `run` starts is a
            # Compose project -- name the type rather than leaving `run` to
            # re-detect it and trip over a coexisting framework.
            _run_after_save(
                ctx,
                working_directory=working_directory,
                root=root,
                selected_type="compose",
            )
        return
    resolved = _resolve_application(
        working_directory=working_directory,
        name=name,
        selected_type=selected_type,
        root=root,
        port=port,
        config=config,
        command=command,
        isolate=isolate,
        # The shared file never inherits personal settings; a local save
        # keeps the ones already there.
        local_config=local,
    )
    assert resolved.selected_type != "compose"
    _persist_resolved_application(
        resolved,
        extend=extend,
        local=local,
        dry_run=dry_run,
        interactive=interactive,
    )
    if not dry_run:
        _record_registry(resolved)
    if run_after and not dry_run:
        _run_after_save(
            ctx,
            working_directory=working_directory,
            root=root,
            config=config,
            name=name,
            selected_type=selected_type,
        )


def _detect_save_type(
    start: Path, *, root: Path | None, config: Path | None, local: bool = False
) -> str:
    """Detect the type a `save` without `--type` should persist.

    Follows `_resolve_application`: a saved type or command wins, then a
    pinned root (from `--project-root` or configuration) resolves in place,
    and only an unpinned save walks upward. That walk matches `run`'s
    priority: prefer a host or Compose type over a coexisting Dockerfile
    (a Django app that also ships one), and only consider "dockerfile"
    when nothing else resolves. A single `discover_type(...,
    allowed=SAVE_TYPES)` call would instead raise an ambiguity error
    whenever both are present.
    """
    flagged_root = (
        resolve_root(start=start, flag=root, configured=None, config_dir=None)
        if root is not None
        else None
    )
    config_file = config or discover_config(flagged_root or start)
    settings = load_config(config_file, local=local) if config_file else RunConfig()
    if settings.type:
        return settings.type
    if settings.command:
        return "custom"
    pinned = flagged_root or resolve_root(
        start=start,
        flag=None,
        configured=settings.root,
        config_dir=config_file.parent if config_file else None,
    )
    if pinned is not None:
        detected, _ = resolve_pinned_type(
            pinned, None, allowed=SAVE_TYPES, from_flag=root is not None
        )
        return detected
    try:
        detected, _ = discover_type(start, None, allowed=RUN_TYPES)
    except click.ClickException as error:
        if "could not detect a project type" not in str(error):
            raise
        detected, _ = discover_type(start, None, allowed=SAVE_TYPES)
    return detected


def _run_after_save(ctx: click.Context, **overrides: object) -> None:
    """Start what `save` just wrote, on the terms it was written with.

    `run` re-runs the whole resolution chain from its own flags, so a save
    that used `--project-root`, `--config`, or an implied type has to
    forward them; otherwise `run` searches from the invocation directory,
    never sees the file just written, and fails on the very setup that
    succeeded a line earlier. `--port` and the trailing command are
    deliberately not forwarded: `save` has just persisted them, and `run`
    reads them back out of the saved configuration.
    """
    ctx.invoke(run, **overrides)


def _compose_save_root(start: Path, files: tuple[Path, ...]) -> Path:
    """The directory `save compose` writes `compose.override.yaml` into.

    An explicit `-f/--file` stack (or `COMPOSE_FILE`, its environment
    equivalent -- the same pairing `_resolve_application` makes) names the
    model to inspect, so the invocation directory stays the output
    directory: its existing, documented behaviour. Otherwise the Compose
    root has to be searched for exactly as bare `save` searches, or an
    override saved from `project/services/api` lands where Compose will
    never merge it.

    Detection failing is not fatal here -- `resolve_compose` reports a
    missing model far better than `discover_type` would -- so an
    undetectable project falls back to the invocation directory.
    """
    if files or os.environ.get("COMPOSE_FILE"):
        return start
    try:
        _, root = discover_type(start, "compose", allowed=RUN_TYPES)
    except click.ClickException:
        return start
    return root


def _auto_loaded_override(directory: Path) -> Path | None:
    """The override Docker Compose would merge in `directory`, if any."""
    for name in COMPOSE_OVERRIDE_NAMES:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _is_auto_loaded_override(cwd: Path, output: Path) -> bool:
    """Would Compose merge `output` on its own, without `-f`?"""
    return (
        output.name in COMPOSE_OVERRIDE_NAMES
        and output.resolve().parent == cwd.resolve()
    )


def _check_override_shadowing(output: Path) -> None:
    """Refuse a write Compose would ignore, or one that would mute another.

    Compose merges only the first override name it finds, so a project
    holding `docker-compose.override.yml` loses it outright the moment a
    `compose.override.yaml` appears beside it: build targets, volumes and
    environment all stop applying, with no message from anything. The
    reverse is just as quiet -- an override written below an existing one
    never loads, and `run` then fails its routing check on a file that was
    written successfully a line earlier.
    """
    if output.name not in COMPOSE_OVERRIDE_NAMES:
        return
    existing = _auto_loaded_override(output.parent)
    if existing is None or existing.name == output.name:
        return
    rank = COMPOSE_OVERRIDE_NAMES.index
    if rank(existing.name) < rank(output.name):
        problem = (
            f"Docker Compose loads '{existing.name}', not '{output.name}'"
        )
    else:
        problem = (
            f"writing '{output.name}' would stop Docker Compose loading "
            f"'{existing.name}', which it merges today"
        )
    raise click.ClickException(
        f"{problem}: Compose merges only the first override it finds. "
        f"Save into that file instead with --output {existing} --extend"
    )


def _pin_compose_type(
    root: Path, *, extend: bool, dry_run: bool, interactive: bool
) -> None:
    """Remember `type = "compose"` so later runs need no `--type`.

    Detection cannot resolve a `compose.yaml` that shares a directory with a
    framework: `discover_type` refuses and demands a type. `save --type
    django` pins the host side of that fork forever; without this the
    Compose side could not be pinned at all, and every later `run` would
    need `--type compose` typed by hand.

    Only an explicit `--type compose` writes it, and only without
    `-f/--file` or `--output`: an explicit file stack is inspected from the
    invocation directory, which is not necessarily a project root.
    """
    config = RunConfig(type="compose")
    if dry_run:
        _preview(root / CONFIG_NAME, render_run_config(config))
        return
    _save_run_config(root, config, extend=extend, interactive=interactive)


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
) -> None:
    """Persist the Compose setup for a Compose project."""
    output = output or cwd / "compose.override.yaml"
    _check_override_shadowing(output)
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
            _preview(output, render_override(document))
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
            _preview(output, render_override(document))
        else:
            write_new(output, document)
            success(
                f"Created {output} for service '{candidate.name}' on container "
                f"port {selected_port}."
            )

    if not dry_run:
        info(
            f"Run it with {COMMAND} run, or start the hub and keep using "
            "docker compose up."
        )


def _persist_resolved_application(
    resolved: ResolvedApplication,
    *,
    extend: bool,
    local: bool = False,
    dry_run: bool,
    interactive: bool,
) -> None:
    """Persist a host resolution to .localghost.toml.

    A local save writes .localghost.local.toml instead, keeping only what
    differs from the shared file so later shared changes still apply.

    Compose projects never reach here: `save` routes them to
    `_save_compose_project` before resolving a host plan.
    """
    plan = resolved.plan
    assert plan is not None
    # A generic folder's name came from a parent, and the file written here
    # stops that climb, so the name is pinned in it; moving the folder later
    # can't change it either.
    generic = resolved.root.name.lower() in GENERIC_NAMES
    saved_config = _run_config_from_plan(
        plan,
        explicit_name=resolved.name or (plan.name if generic else None),
        source_command=resolved.command,
    )
    target = resolved.config_file or resolved.root / CONFIG_NAME
    if local:
        shared = load_config(target, local=False)
        blank = RunConfig()
        saved_config = RunConfig(
            **{
                key: getattr(blank, key)
                if getattr(saved_config, key) == getattr(shared, key)
                else getattr(saved_config, key)
                for key in RunConfig.__dataclass_fields__
            }
        )
        target = local_config_path(target)
    if dry_run:
        _preview(target, render_run_config(saved_config))
        return
    _save_run_config(
        resolved.root,
        saved_config,
        # The local file is personal: updating it needs no confirmation, and
        # a backup beside it would be one more uncommitted file to ignore.
        extend=extend or local,
        interactive=interactive,
        target=target,
        keep_backup=not local,
    )
    if local:
        _ensure_ignored(target)


def _ensure_ignored(path: Path) -> None:
    """Add `path` to its folder's .gitignore when git would commit it."""
    try:
        status = subprocess.run(
            ["git", "-C", str(path.parent), "check-ignore", "-q", path.name],
            capture_output=True,
        ).returncode
    except OSError:
        return
    if status != 1:  # 0: already ignored; 128: not a git checkout
        return
    gitignore = path.parent / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    separator = "\n" if existing and not existing.endswith("\n") else ""
    gitignore.write_text(f"{existing}{separator}{path.name}\n", encoding="utf-8")
    shown = (
        Path(gitignore.name)
        if gitignore.parent.resolve() == Path.cwd().resolve()
        else gitignore
    )
    info(f"Added {path.name} to {shown}.")


def _save_dockerfile_project(
    *,
    working_directory: Path | None,
    root: Path | None,
    service_name: str | None,
    port: int | None,
    output: Path | None,
    dry_run: bool,
    interactive: bool,
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
    if port is None and interactive:
        port = click.prompt(
            "Container HTTP port",
            type=click.IntRange(1, 65535),
        )
    if port is None:
        raise click.ClickException("saving a Dockerfile project requires --port")
    service_name = service_name or "app"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service_name):
        raise click.ClickException(f"'{service_name}' is not a valid service name")
    validate_project_name_value(_local_project_name(project_root))
    output = output or project_root / "compose.yaml"
    if output.exists() and not dry_run:
        raise click.ClickException(f"refusing to overwrite existing '{output}'")
    document = create_dockerfile_compose(service_name, port)
    if dry_run:
        _preview(output, render_override(document))
        return
    write_new(output, document)
    success(f"Created {output} for the Dockerfile application.")
    info(f"Run it with {COMMAND} run.")
    registry.record(_local_project_name(project_root), project_root, "dockerfile")


def _run_config_from_plan(
    plan: RunPlan,
    *,
    explicit_name: str | None = None,
    source_command: tuple[str, ...] = (),
) -> RunConfig:
    return RunConfig(
        type=None if plan.type == "custom" else plan.type,
        name=explicit_name,
        port=plan.wanted_port or plan.port,
        command=(source_command or plan.command) if plan.type == "custom" else (),
        isolate=plan.isolate,
    )


def _preview(target: Path, content: str) -> None:
    """Show what a dry run would write, naming the file on stderr.

    stdout carries only file contents so `--dry-run > file` stays usable;
    the label goes to stderr, where it separates one previewed file from
    the next when a single save writes several.
    """
    shown = (
        Path(target.name)
        if target.parent.resolve() == Path.cwd().resolve()
        else target
    )
    info(f"Would write {shown}:", err=True)
    click.echo(content, nl=False)


def _save_run_config(
    root: Path,
    config: RunConfig,
    *,
    extend: bool,
    interactive: bool,
    target: Path | None = None,
    keep_backup: bool = True,
) -> None:
    target = target or root / CONFIG_NAME
    existed = target.exists()
    backup = write_run_config(
        target, config, extend=extend, interactive=interactive, keep_backup=keep_backup
    )
    shown = (
        Path(target.name)
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
            f"service '{candidate.name}' has {detail}; rerun with "
            f"'{COMMAND} save --port PORT'"
        )

    default = candidate.ports[0] if candidate.ports else None
    return click.prompt(
        "Container HTTP port",
        default=default,
        type=click.IntRange(1, 65535),
        show_default=default is not None,
    )
