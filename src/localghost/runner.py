"""Foreground host-process runner and its ephemeral Caddy bridge."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from . import statusbar
from .feedback import info, warning
from .generator import (
    DNS_SAFE_PROJECT,
    HOST_BRIDGE_IMAGE,
    PROXY_NETWORK,
    render_override,
)


@dataclass(frozen=True)
class RunPlan:
    name: str
    type: str
    command: tuple[str, ...]
    port: int
    project: str
    bridge_yaml: str
    project_root: Path | None = None
    working_directory: Path | None = None


SUPPORTED_TYPES = (
    "compose",
    "dockerfile",
    "django",
    "vite",
    "astro",
    "cakephp",
    "laravel",
    "php",
)
RUN_TYPES = tuple(item for item in SUPPORTED_TYPES if item != "dockerfile")
# `compose` is never a legitimate `generate --type` value: with no Compose
# file present it would ask generate to scaffold a *host* bridge for a type
# named "compose", and with one present, `--type` is already rejected
# outright regardless of its value. Excluding it here removes the dead,
# actively-misleading choice instead of special-casing it at every call site.
GENERATE_TYPES = tuple(item for item in SUPPORTED_TYPES if item != "compose")

DEFAULT_PORTS = {
    "django": 8000,
    "vite": 5173,
    "astro": 4321,
    "cakephp": 8765,
    "laravel": 8000,
    "php": 8080,
}

COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
PHP_DOCROOTS = ("public", "web", "htdocs", "www")


def type_choices(prefix: str, allowed: tuple[str, ...] = SUPPORTED_TYPES) -> str:
    """Render a type list for an error message."""
    *leading, last = allowed
    return f"{prefix} must be {', '.join(leading)}, or {last}"


class _TerminationSignal(Exception):
    """Internal control flow used to make termination run normal cleanup."""

    def __init__(self, signum: int) -> None:
        self.signum = signum


class _ForceQuit(_TerminationSignal):
    """A termination signal repeated after the grace period has elapsed."""


def resolve_name(cwd: Path) -> str:
    """Resolve the public name with Compose-compatible precedence."""
    value = os.environ.get("COMPOSE_PROJECT_NAME") or _dotenv_name(cwd / ".env")
    if not value:
        value = "".join(
            char for char in cwd.name.lower() if char.isalnum() or char in "-_"
        )
        value = value.lstrip("-_")
    validate_name(value)
    return value


def validate_name(name: str) -> None:
    if not DNS_SAFE_PROJECT.fullmatch(name):
        raise click.ClickException(
            f"'{name}' is not a DNS-safe project name; use --name with lowercase "
            "letters, numbers, and hyphens"
        )


def resolve_pinned_type(
    pinned: Path,
    requested: str | None,
    *,
    allowed: tuple[str, ...] = RUN_TYPES,
    from_flag: bool = True,
) -> tuple[str, Path]:
    """Resolve the project type at an exact, already-pinned root.

    A pinned root never walks: it resolves entirely against what is detected
    at that exact directory, including a weak php match (a bare index.php) —
    pinning means "this is the root", so there is nothing further out to
    defer to. Shared by `build_plan`'s own pinned handling and by `run`'s
    up-front compose check, so both resolve type and root from one piece of
    logic rather than two independently maintained copies.

    `from_flag` controls whether the mismatch error may suggest dropping
    `--root`: that advice is only true when the pin actually came from the
    flag, not from `[run].root` or a discovered `.localghost.toml`.
    """
    detected = [item for item in _types_at(pinned) if item in allowed]
    if requested is not None:
        if requested not in detected:
            available = ", ".join(detected) if detected else "nothing"
            hint = "drop --type to auto-detect"
            if from_flag:
                hint += ", or drop --root to search from the current directory"
            raise click.ClickException(
                f"no {requested} project at '{pinned}'; detected "
                f"{available} there; {hint}"
            )
        return requested, pinned
    if not detected:
        raise click.ClickException(
            f"could not detect a project type from '{pinned}'; provide "
            "--type, or a command after -- together with --port"
        )
    if len(detected) > 1:
        choices = " or ".join(f"--type {item}" for item in detected)
        raise click.ClickException(
            f"both {' and '.join(detected)} were detected; rerun with {choices}"
        )
    return detected[0], pinned


def build_plan(
    cwd: Path,
    name: str | None,
    framework: str | None,
    port: int | None,
    command: tuple[str, ...],
    *,
    pinned: Path | None = None,
    pinned_from_flag: bool = True,
) -> RunPlan:
    cwd = cwd.resolve()
    if command:
        project_root = cwd
        working_directory = cwd
        if port is None:
            raise click.ClickException("a custom command requires --port")
        selected_type = "custom"
        selected_port = select_port(port, strict=True)
        selected_command = tuple(
            part.replace("{port}", str(selected_port)) for part in command
        )
    else:
        if pinned is not None:
            selected_type, project_root = resolve_pinned_type(
                pinned, framework, from_flag=pinned_from_flag
            )
        else:
            selected_type, project_root = discover_type(cwd, framework)
        working_directory = project_root
        if selected_type == "django":
            default_port, selected_command = django_command(project_root, port)
        elif selected_type == "vite":
            default_port, selected_command = vite_command(project_root, port)
        elif selected_type == "astro":
            default_port, selected_command = astro_command(project_root, port)
        elif selected_type == "cakephp":
            default_port, selected_command, working_directory = cakephp_command(
                project_root, port
            )
        elif selected_type == "laravel":
            default_port, selected_command = laravel_command(project_root, port)
        elif selected_type == "php":
            default_port, selected_command, working_directory = php_command(
                project_root, port
            )
        else:  # Click validates the public option; retain this for direct callers.
            raise click.ClickException(type_choices("--type"))
        selected_port = select_port(port or default_port, strict=port is not None)
        selected_command = tuple(
            part.format(port=selected_port) for part in selected_command
        )
    public_name = name or resolve_name(project_root)
    validate_name(public_name)
    project = _session_project(project_root)
    return RunPlan(
        public_name,
        selected_type,
        selected_command,
        selected_port,
        project,
        render_override(
            create_run_bridge_compose(public_name, selected_port, project_root)
        ),
        project_root,
        working_directory,
    )


def discover_type(
    cwd: Path,
    requested: str | None = None,
    allowed: tuple[str, ...] = RUN_TYPES,
) -> tuple[str, Path]:
    """Return the nearest project type this command supports, and its root.

    Ambiguity is computed after filtering to `allowed`, so a type another
    command handles never blocks this one. Generic `php` defers to anything
    stronger found further up; see `_php_project_strength`.
    """
    if requested is not None and requested not in SUPPORTED_TYPES:
        raise click.ClickException(type_choices("--type"))
    if requested is not None and requested not in allowed:
        if requested == "dockerfile":
            raise click.ClickException(
                "'dockerfile' cannot be run directly; run `localghost generate "
                "--type dockerfile` to build a Compose file first"
            )
        raise click.ClickException(
            f"'{requested}' is not available here; {type_choices('--type', allowed)}"
        )
    start = cwd.resolve()
    fallback: tuple[str, Path] | None = None
    for candidate in _search_path(start):
        detected = [item for item in _types_at(candidate) if item in allowed]
        if requested is not None:
            if requested in detected:
                if requested == "php" and _php_project_strength(candidate) != "strong":
                    if fallback is None:
                        fallback = (requested, candidate)
                    continue
                return requested, candidate
            continue
        if detected == ["php"]:
            if _php_project_strength(candidate) == "strong":
                return "php", candidate
            if fallback is None:
                fallback = ("php", candidate)
            continue
        if len(detected) > 1:
            choices = " or ".join(f"--type {item}" for item in detected)
            raise click.ClickException(
                f"both {' and '.join(detected)} were detected; rerun with {choices}"
            )
        if detected:
            return detected[0], candidate
    if fallback is not None:
        return fallback
    if requested is not None:
        raise click.ClickException(
            f"could not find a {requested} project root from '{start}'"
        )
    raise click.ClickException(
        f"could not detect a project type from '{start}'; provide --type, or a "
        "command after -- together with --port"
    )


VCS_MARKERS = (".git", ".hg", ".svn")


def _search_path(start: Path) -> list[Path]:
    """Candidate project directories, nearest first.

    `$HOME` itself and everything above it, including the filesystem root,
    are never candidates, so a stray manifest there cannot be adopted — even
    when `$HOME` holds a VCS marker. Search then stops after the first
    remaining directory holding a VCS marker, which is itself a candidate.
    """
    candidates = [start, *start.parents]
    home = Path.home().resolve()
    excluded = {home, *home.parents}
    candidates = [path for path in candidates if path not in excluded]
    boundary = next(
        (
            path
            for path in candidates
            if any((path / marker).exists() for marker in VCS_MARKERS)
        ),
        None,
    )
    if boundary is not None:
        return candidates[: candidates.index(boundary) + 1]
    return candidates


def _compose_file(cwd: Path) -> Path | None:
    for filename in COMPOSE_FILENAMES:
        candidate = cwd / filename
        if candidate.is_file():
            return candidate
    return None


def _php_docroot(cwd: Path) -> Path | None:
    """The directory holding index.php, or None when there is no entrypoint."""
    for name in PHP_DOCROOTS:
        candidate = cwd / name
        if (candidate / "index.php").is_file():
            return candidate
    if (cwd / "index.php").is_file():
        return cwd
    return None


def _php_project_strength(cwd: Path) -> str | None:
    """How strongly this directory looks like a php project root.

    `composer.json` or a populated docroot means a project; a bare
    `index.php` means a directory that is probably itself a docroot.
    """
    if _composer_manifest(cwd) is not None:
        return "strong"
    for name in PHP_DOCROOTS:
        if (cwd / name / "index.php").is_file():
            return "strong"
    if (cwd / "index.php").is_file():
        return "weak"
    return None


def _php_type(cwd: Path, composer: dict[str, object] | None) -> str | None:
    """Resolve the PHP tier: the most specific framework wins."""
    cake_dependency = composer is not None and _has_composer_dependency(
        composer, "cakephp/cakephp"
    )
    if (cake_dependency and (cwd / "bin" / "cake").is_file()) or _legacy_cakephp_root(
        cwd
    ):
        return "cakephp"
    laravel_dependency = composer is not None and _has_composer_dependency(
        composer, "laravel/framework"
    )
    if laravel_dependency and (cwd / "artisan").is_file():
        return "laravel"
    if composer is not None or _php_docroot(cwd) is not None:
        return "php"
    return None


def _types_at(cwd: Path) -> list[str]:
    """Every project type present at this directory, in SUPPORTED_TYPES order."""
    detected = []
    if _compose_file(cwd) is not None:
        detected.append("compose")
    if (cwd / "Dockerfile").is_file():
        detected.append("dockerfile")
    if (cwd / "manage.py").is_file():
        detected.append("django")
    if _vite_manifest(cwd) is not None:
        detected.append("vite")
    if _astro_manifest(cwd) is not None:
        detected.append("astro")
    php = _php_type(cwd, _composer_manifest(cwd))
    if php is not None:
        detected.append(php)
    return detected


def django_command(
    cwd: Path, requested_port: int | None
) -> tuple[int, tuple[str, ...]]:
    if not (cwd / "manage.py").is_file():
        raise click.ClickException("Django requires manage.py in the current directory")
    if (cwd / "uv.lock").is_file():
        _require_executable("uv", "Django project runner")
        prefix = ("uv", "run", "python")
    elif (cwd / "poetry.lock").is_file():
        _require_executable("poetry", "Django project runner")
        prefix = ("poetry", "run", "python")
    elif (cwd / "Pipfile").is_file() or (cwd / "Pipfile.lock").is_file():
        _require_executable("pipenv", "Django project runner")
        prefix = ("pipenv", "run", "python")
    elif os.environ.get("VIRTUAL_ENV"):
        python = Path(os.environ["VIRTUAL_ENV"]) / "bin" / "python"
        _require_path(python, "active virtualenv Python")
        prefix = (str(python),)
    elif (cwd / ".venv").is_dir():
        python = cwd / ".venv" / "bin" / "python"
        _require_path(python, "local .venv Python")
        prefix = (str(python),)
    else:
        raise click.ClickException(
            "no supported Django runner found; install uv, poetry, or pipenv, "
            "activate a virtualenv, or provide a command after --"
        )
    return requested_port or 8000, (*prefix, "manage.py", "runserver", "0.0.0.0:{port}")


def cakephp_command(
    cwd: Path, requested_port: int | None
) -> tuple[int, tuple[str, ...], Path]:
    """Return the appropriate modern or legacy CakePHP development server."""
    if _legacy_cakephp_root(cwd):
        _require_executable("php", "CakePHP project runner")
        return requested_port or 8765, ("php", "-S", "0.0.0.0:{port}"), (
            cwd / "app" / "webroot"
        )

    manifest = _composer_manifest(cwd)
    cake = cwd / "bin" / "cake"
    if (
        manifest is None
        or not _has_composer_dependency(manifest, "cakephp/cakephp")
        or not cake.is_file()
    ):
        raise click.ClickException(
            "CakePHP requires bin/cake and a cakephp/cakephp Composer dependency"
        )
    if os.access(cake, os.X_OK):
        command = ("bin/cake", "server", "-H", "0.0.0.0", "-p", "{port}")
    else:
        cake_php = cwd / "bin" / "cake.php"
        if not cake_php.is_file():
            raise click.ClickException(
                "CakePHP bin/cake is not executable and bin/cake.php was not found"
            )
        _require_executable("php", "CakePHP project runner")
        command = (
            "php",
            "bin/cake.php",
            "server",
            "-H",
            "0.0.0.0",
            "-p",
            "{port}",
        )
    return requested_port or 8765, command, cwd


def laravel_command(
    cwd: Path, requested_port: int | None
) -> tuple[int, tuple[str, ...]]:
    manifest = _composer_manifest(cwd)
    if (
        manifest is None
        or not _has_composer_dependency(manifest, "laravel/framework")
        or not (cwd / "artisan").is_file()
    ):
        raise click.ClickException(
            "Laravel requires artisan and a laravel/framework Composer dependency"
        )
    _require_executable("php", "Laravel project runner")
    return requested_port or 8000, (
        "php",
        "artisan",
        "serve",
        "--host=0.0.0.0",
        "--port={port}",
    )


def php_command(
    cwd: Path, requested_port: int | None
) -> tuple[int, tuple[str, ...], Path]:
    """Serve a plain PHP project from its docroot with the built-in server."""
    _require_executable("php", "PHP project runner")
    docroot = _php_docroot(cwd) or cwd
    return (
        requested_port or DEFAULT_PORTS["php"],
        ("php", "-S", "0.0.0.0:{port}"),
        docroot,
    )


def astro_command(cwd: Path, requested_port: int | None) -> tuple[int, tuple[str, ...]]:
    manifest = _astro_manifest(cwd)
    if manifest is None:
        raise click.ClickException(
            "Astro requires a valid package.json with a dev script and astro dependency"
        )
    manager = package_manager(cwd, manifest)
    _require_executable(manager, "Astro package manager")
    commands = {
        "npm": ("npm", "run", "dev", "--"),
        # pnpm forwards the separator itself to the script. Yarn 1 forwards
        # arguments without it (and warns that future versions will not strip
        # it), so npm is the only manager here that needs the explicit
        # separator.
        "pnpm": ("pnpm", "run", "dev"),
        "yarn": ("yarn", "run", "dev"),
        "bun": ("bun", "run", "dev"),
    }
    return requested_port or 4321, (
        *commands[manager],
        "--port",
        "{port}",
        "--host",
        "0.0.0.0",
    )


def package_manager(cwd: Path, manifest: dict[str, object]) -> str:
    declared = manifest.get("packageManager")
    if isinstance(declared, str) and declared:
        manager = declared.split("@", 1)[0]
        if manager not in {"npm", "pnpm", "yarn", "bun"}:
            raise click.ClickException(f"unsupported packageManager '{manager}'")
        return manager
    lockfiles = {
        "npm": ("package-lock.json", "npm-shrinkwrap.json"),
        "pnpm": ("pnpm-lock.yaml",),
        "yarn": ("yarn.lock",),
        "bun": ("bun.lock", "bun.lockb"),
    }
    found = [
        manager
        for manager in _PACKAGE_MANAGER_PRIORITY
        if any((cwd / item).is_file() for item in lockfiles[manager])
    ]
    if not found:
        return "npm"
    for manager in found:
        if shutil.which(manager) is not None:
            return manager
    # Preserve the deterministic error for a project whose detected managers
    # are all unavailable, while preferring an installed fallback when there is
    # one.
    return found[0]


def select_port(port: int, strict: bool) -> int:
    if _port_available(port):
        return port
    if strict:
        raise click.ClickException(f"host port {port} is already in use")
    for candidate in range(port + 1, min(port + 100, 65536)):
        if _port_available(candidate):
            return candidate
    raise click.ClickException(
        f"no free host port found from {port} through {min(port + 99, 65535)}"
    )


def create_run_bridge_compose(
    name: str, host_port: int, source_path: Path | None = None
) -> CommentedMap:
    """Return the fileless, foreground-owned bridge Compose model."""
    router = f"{name}-app"
    service = CommentedMap()
    service["image"] = HOST_BRIDGE_IMAGE
    service["command"] = CommentedSeq(
        [
            "caddy",
            "reverse-proxy",
            "--from",
            ":8080",
            "--to",
            f"http://host.docker.internal:{host_port}",
        ]
    )
    service["extra_hosts"] = CommentedSeq(["host.docker.internal:host-gateway"])
    service["restart"] = "no"
    service["networks"] = CommentedSeq([PROXY_NETWORK])
    labels = [
        "traefik.enable=true",
        f"traefik.docker.network={PROXY_NETWORK}",
        f"traefik.http.routers.{router}.entrypoints=web",
        f"traefik.http.routers.{router}.rule=Host(`{name}.localhost`)",
        f"traefik.http.routers.{router}.service={router}",
        f"traefik.http.routers.{router}-secure.entrypoints=websecure",
        f"traefik.http.routers.{router}-secure.rule=Host(`{name}.localhost`)",
        f"traefik.http.routers.{router}-secure.service={router}",
        f"traefik.http.routers.{router}-secure.tls=true",
        f"traefik.http.services.{router}.loadbalancer.server.port=8080",
        "io.localghost.managed=true",
        "io.localghost.kind=host-run-bridge",
        f"io.localghost.tls-domains={name}.localhost", 
    ]
    if source_path is not None:
        labels.append(f"io.localghost.source-path={source_path}")
    service["labels"] = CommentedSeq(labels)
    document = CommentedMap({"services": CommentedMap({"bridge": service})})
    document["networks"] = CommentedMap(
        {
            PROXY_NETWORK: CommentedMap(
                {"external": True, "name": PROXY_NETWORK}
            )
        }
    )
    return document


def find_route_collision(name: str) -> str | None:
    try:
        listed = subprocess.run(
            ["docker", "ps", "--all", "--quiet"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise click.ClickException("docker is required") from exc
    if listed.returncode:
        raise click.ClickException(
            listed.stderr.strip() or "could not inspect Docker containers"
        )
    identifiers = listed.stdout.split()
    if not identifiers:
        return None
    inspected = subprocess.run(
        ["docker", "inspect", *identifiers], check=False, capture_output=True, text=True
    )
    if inspected.returncode:
        raise click.ClickException(
            inspected.stderr.strip() or "could not inspect Docker containers"
        )
    try:
        containers = json.loads(inspected.stdout)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            "Docker returned invalid container inspection data"
        ) from exc
    hostname = f"`{name}.localhost`"
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {})
        if isinstance(labels, dict) and any(
            _rule_claims_hostname(str(value), hostname)
            for key, value in labels.items()
            if key.startswith("traefik.http.routers.") and key.endswith(".rule")
        ):
            return str(container.get("Id") or container.get("Name", "unknown")).lstrip(
                "/"
            )
    return None


def start_bridge(plan: RunPlan) -> None:
    _compose(plan, ["up", "--detach"])


def stop_bridge(plan: RunPlan) -> None:
    _compose(plan, ["down", "--remove-orphans"])


def _compose(plan: RunPlan, action: list[str]) -> None:
    command = [
        "docker",
        "compose",
        "--project-name",
        plan.project,
        "--file",
        "-",
        *action,
    ]
    try:
        result = subprocess.run(command, input=plan.bridge_yaml, text=True, check=False)
    except FileNotFoundError as exc:
        raise click.ClickException("docker is required") from exc
    if result.returncode:
        raise click.exceptions.Exit(result.returncode)


def execute(
    plan: RunPlan,
    start_proxy: Callable[[], None],
    *,
    cwd: Path | None = None,
    public_origin: str | None = None,
    status_bar: bool = True,
) -> int:
    bridge_attempted = False
    child: subprocess.Popen[bytes] | None = None
    status = 1
    cleanup_error: Exception | None = None
    old_handlers = _install_termination_handlers()
    try:
        # Opened before the hub is reconciled: that is the slowest step of a
        # cold start, and the URL is most wanted while waiting on it. Hub and
        # bridge progress scroll above the bar; teardown runs after it is
        # released, so those messages are plain output either way.
        with statusbar.pinned(
            public_origin or f"http://{plan.name}.localhost",
            enabled=status_bar and public_origin is not None,
            probe=statusbar.tcp_probe(plan.port),
            message="starting hub",
        ) as bar:
            start_proxy()
            bridge_attempted = True
            start_bridge(plan)
            bar.status("starting")
            if statusbar.tcp_probe(plan.port)():
                # Sampled before the child exists, so this is somebody else.
                # The application is about to fail to bind, and until it does
                # the readiness probe cannot tell the squatter apart from a
                # fast start.
                warning(
                    "Port already in use",
                    [
                        f"Something is already serving on port {plan.port}; "
                        "the application may fail to start, and the status "
                        "bar cannot tell it apart from the application."
                    ],
                )
            try:
                child = subprocess.Popen(
                    list(plan.command),
                    cwd=plan.working_directory or cwd or Path.cwd(),
                    start_new_session=True,
                )
            except OSError as exc:
                raise click.ClickException(
                    f"could not start application command: {exc}"
                ) from exc
            status = child.wait()
    except (KeyboardInterrupt, _TerminationSignal) as interrupted:
        signum = (
            signal.SIGINT
            if isinstance(interrupted, KeyboardInterrupt)
            else interrupted.signum
        )
        status = 128 + signum
        if child is not None:
            _terminate_process_tree(child, signum)
            try:
                child.wait()
            except _ForceQuit:
                # The child is not exiting and the user asked again after
                # the grace period: force it down and stop waiting.
                with contextlib.suppress(AttributeError, ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                info("Force quitting.")
    finally:
        try:
            if bridge_attempted:
                try:
                    stop_bridge(plan)
                except _ForceQuit:
                    # A repeat Ctrl+C after the grace period landed during
                    # teardown; give up on removing the bridge here (it is
                    # reclaimed on the next run).
                    info("Bridge cleanup interrupted.")
                except _TerminationSignal:
                    # A termination signal landed mid-teardown. Retry once
                    # to leave no bridge behind; repeats within the grace
                    # period are ignored by the handler.
                    try:
                        stop_bridge(plan)
                    except Exception as exc:  # preserve the child status below
                        cleanup_error = exc
                except Exception as exc:  # preserve the child status below
                    cleanup_error = exc
            _restore_termination_handlers(old_handlers)
        except _ForceQuit:
            # A force-quit landed during the final bookkeeping; exit with
            # the interrupted status rather than crashing.
            pass
    if cleanup_error:
        warning(
            "Bridge cleanup failed",
            [f"Could not remove bridge '{plan.project}': {cleanup_error}"],
        )
        if status == 0:
            return 1
    return status


def django_settings_warnings(
    plan: RunPlan, cwd: Path, *, public_origin: str | None = None
) -> list[str]:
    """Return advisory warnings from Django's loaded settings, if available."""
    if plan.type != "django":
        return []
    command = [
        *plan.command[:-2],
        "shell",
        "-c",
        (
            "import json; from django.conf import settings; "
            "print(json.dumps({'allowed_hosts': settings.ALLOWED_HOSTS, "
            "'csrf_trusted_origins': settings.CSRF_TRUSTED_ORIGINS}))"
        ),
    ]
    try:
        result = subprocess.run(
            command, cwd=cwd, check=False, capture_output=True, text=True
        )
    except OSError:
        return []
    if result.returncode:
        return []
    try:
        settings = json.loads(result.stdout.splitlines()[-1])
        allowed_hosts = settings["allowed_hosts"]
        csrf_origins = settings["csrf_trusted_origins"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return []
    host = f"{plan.name}.localhost"
    warnings = []
    if not isinstance(allowed_hosts, list) or not _host_is_allowed(host, allowed_hosts):
        warnings.append(
            f"Django ALLOWED_HOSTS does not include '{host}'; add it before "
            "opening the public URL."
        )
    origin = public_origin or f"http://{host}"
    if not isinstance(csrf_origins, list) or not _origin_is_trusted(
        origin, csrf_origins
    ):
        warnings.append(
            f"Django CSRF_TRUSTED_ORIGINS does not include '{origin}'; add it "
            "if CSRF-protected requests use this origin."
        )
    return warnings


def _rule_claims_hostname(rule: str, hostname: str) -> bool:
    """Conservatively detect Host/HostRegexp rules that include the hostname."""
    return hostname in rule and ("Host(" in rule or "HostRegexp(" in rule)


def _host_is_allowed(host: str, allowed_hosts: list[object]) -> bool:
    normalized_host = host.lower().rstrip(".")
    for item in allowed_hosts:
        if not isinstance(item, str):
            continue
        pattern = item.lower().rstrip(".")
        if pattern == "*" or pattern == normalized_host:
            return True
        if pattern.startswith(".") and normalized_host.endswith(pattern):
            return True
    return False


def _origin_is_trusted(origin: str, origins: list[object]) -> bool:
    if origin in origins:
        return True
    scheme, host = origin.split("://", 1)
    return f"{scheme}://*.{host.partition('.')[2]}" in origins


_FORCE_QUIT_DELAY = 2.0  # seconds of grace before a repeat Ctrl+C force-quits


def _install_termination_handlers() -> dict[int, signal.Handlers]:
    first_at: float | None = None

    def terminate(signum: int, frame: object) -> None:
        nonlocal first_at
        del frame
        now = time.monotonic()
        if first_at is None:
            first_at = now
            raise _TerminationSignal(signum)
        if now - first_at < _FORCE_QUIT_DELAY:
            # A repeat within the grace period is almost always an
            # accidental double-press; keep the graceful shutdown going.
            return
        # The graceful shutdown is stuck and the user asked again after
        # the grace period: force quit cleanly.
        raise _ForceQuit(signum)

    return {
        signum: signal.signal(signum, terminate)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }


def _restore_termination_handlers(old_handlers: dict[int, signal.Handlers]) -> None:
    for signum, handler in old_handlers.items():
        signal.signal(signum, handler)


def _terminate_process_tree(child: subprocess.Popen[bytes], signum: int) -> None:
    try:
        os.killpg(child.pid, signum)
        return
    except (AttributeError, ProcessLookupError):
        pass
    send_signal = getattr(child, "send_signal", None)
    if send_signal is not None:
        send_signal(signum)
    else:
        child.terminate()


_JSON_DEPENDENCY_KEYS = ("dependencies", "devDependencies")
_PACKAGE_MANAGER_PRIORITY = ("bun", "pnpm", "yarn", "npm")


def _has_dependency(manifest: dict[str, object], name: str) -> bool:
    return any(
        isinstance(group, dict) and name in group
        for group in (
            manifest.get(key)
            for key in _JSON_DEPENDENCY_KEYS
        )
    )


def _has_composer_dependency(manifest: dict[str, object], name: str) -> bool:
    return any(
        isinstance(manifest.get(group), dict) and name in manifest[group]
        for group in ("require", "require-dev")
    )


def _composer_manifest(cwd: Path) -> dict[str, object] | None:
    path = cwd / "composer.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            f"could not read valid composer.json: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise click.ClickException("composer.json must contain an object")
    return value


def _legacy_cakephp_root(cwd: Path) -> bool:
    return (cwd / "app" / "Config" / "core.php").is_file() and (
        cwd / "app" / "webroot" / "index.php"
    ).is_file()


def _vite_manifest(cwd: Path) -> dict[str, object] | None:
    return _package_json_with_dev_script_and_dep(cwd, "vite")


def _astro_manifest(cwd: Path) -> dict[str, object] | None:
    return _package_json_with_dev_script_and_dep(cwd, "astro")


def _package_json_with_dev_script_and_dep(
    cwd: Path, dep: str
) -> dict[str, object] | None:
    path = cwd / "package.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"could not read valid package.json: {exc}") from exc
    if not isinstance(value, dict):
        raise click.ClickException("package.json must contain an object")
    scripts = value.get("scripts")
    if not isinstance(scripts, dict) or not isinstance(scripts.get("dev"), str):
        return None
    if not _has_dependency(value, dep):
        return None
    return value


def vite_command(cwd: Path, requested_port: int | None) -> tuple[int, tuple[str, ...]]:
    manifest = _vite_manifest(cwd)
    if manifest is None:
        raise click.ClickException(
            "Vite requires a valid package.json with a dev script and vite dependency"
        )
    manager = package_manager(cwd, manifest)
    _require_executable(manager, "Vite package manager")
    commands = {
        "npm": ("npm", "run", "dev", "--"),
        "pnpm": ("pnpm", "run", "dev"),
        "yarn": ("yarn", "run", "dev"),
        "bun": ("bun", "run", "dev"),
    }
    return requested_port or 5173, (
        *commands[manager],
        "--host",
        "0.0.0.0",
        "--port",
        "{port}",
        "--strictPort",
    )


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _require_executable(executable: str, description: str) -> None:
    if shutil.which(executable) is None:
        raise click.ClickException(
            f"{description} '{executable}' was not found; provide a command after --"
        )


def _require_path(path: Path, description: str) -> None:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise click.ClickException(f"{description} was not found at '{path}'")


def _dotenv_name(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() == "COMPOSE_PROJECT_NAME":
            return value.strip().strip("'\"")
    return None


def _session_project(cwd: Path) -> str:
    digest = hashlib.sha256(str(cwd.resolve()).encode()).hexdigest()[:10]
    return f"localghost-host-{digest}-{uuid.uuid4().hex[:8]}"
