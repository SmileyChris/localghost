"""Foreground host-process runner and its ephemeral Caddy bridge."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .generator import (
    DNS_SAFE_PROJECT,
    HOST_BRIDGE_IMAGE,
    PROXY_NETWORK,
    render_override,
)


@dataclass(frozen=True)
class RunPlan:
    name: str
    framework: str
    command: tuple[str, ...]
    port: int
    project: str
    bridge_yaml: str


class _TerminationSignal(Exception):
    """Internal control flow used to make termination run normal cleanup."""

    def __init__(self, signum: int) -> None:
        self.signum = signum


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


def build_plan(
    cwd: Path,
    name: str | None,
    framework: str | None,
    port: int | None,
    command: tuple[str, ...],
) -> RunPlan:
    public_name = name or resolve_name(cwd)
    validate_name(public_name)
    if command:
        if port is None:
            raise click.ClickException("a custom command requires --port")
        selected_framework = "custom"
        selected_command = command
        selected_port = select_port(port, strict=True)
    else:
        selected_framework = framework or detect_framework(cwd)
        if selected_framework == "django":
            default_port, selected_command = django_command(cwd, port)
        elif selected_framework == "vite":
            default_port, selected_command = vite_command(cwd, port)
        else:  # Click validates the public option; retain this for direct callers.
            raise click.ClickException("--framework must be django or vite")
        selected_port = select_port(port or default_port, strict=port is not None)
        selected_command = tuple(
            part.format(port=selected_port) for part in selected_command
        )
    project = _session_project(cwd)
    return RunPlan(
        public_name,
        selected_framework,
        selected_command,
        selected_port,
        project,
        render_override(
            create_run_bridge_compose(public_name, selected_port, cwd.resolve())
        ),
    )


def detect_framework(cwd: Path) -> str:
    django = (cwd / "manage.py").is_file()
    vite = _vite_manifest(cwd) is not None
    if django and vite:
        raise click.ClickException(
            "both Django and Vite were detected; rerun with --framework django "
            "or --framework vite"
        )
    if django:
        return "django"
    if vite:
        return "vite"
    raise click.ClickException(
        "could not detect Django or Vite; provide a command after -- together "
        "with --port"
    )


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
        "pnpm": ("pnpm", "run", "dev", "--"),
        "yarn": ("yarn", "run", "dev", "--"),
        "bun": ("bun", "run", "dev", "--"),
    }
    return requested_port or 5173, (
        *commands[manager],
        "--host",
        "0.0.0.0",
        "--port",
        "{port}",
        "--strictPort",
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
        for manager, names in lockfiles.items()
        if any((cwd / item).is_file() for item in names)
    ]
    if len(found) > 1:
        raise click.ClickException(
            "multiple package-manager lockfiles found; set packageManager in "
            "package.json or provide a command after --"
        )
    return found[0] if found else "npm"


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
        f"traefik.http.services.{router}.loadbalancer.server.port=8080",
        "io.localhost.managed=true",
        "io.localhost.kind=host-run-bridge",
    ]
    if source_path is not None:
        labels.append(f"io.localhost.source-path={source_path}")
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
    plan: RunPlan, start_proxy: Callable[[], None], *, cwd: Path | None = None
) -> int:
    bridge_attempted = False
    child: subprocess.Popen[bytes] | None = None
    status = 1
    cleanup_error: Exception | None = None
    old_handlers = _install_termination_handlers()
    try:
        start_proxy()
        bridge_attempted = True
        start_bridge(plan)
        try:
            child = subprocess.Popen(
                list(plan.command), cwd=cwd or Path.cwd(), start_new_session=True
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
            child.wait()
    finally:
        _restore_termination_handlers(old_handlers)
        if bridge_attempted:
            try:
                stop_bridge(plan)
            except Exception as exc:  # preserve the child status below
                cleanup_error = exc
    if cleanup_error:
        click.echo(
            f"Warning: failed to remove bridge '{plan.project}': {cleanup_error}",
            err=True,
        )
        if status == 0:
            return 1
    return status


def django_settings_warnings(plan: RunPlan, cwd: Path) -> list[str]:
    """Return advisory warnings from Django's loaded settings, if available."""
    if plan.framework != "django":
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
    origin = f"http://{host}"
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


def _install_termination_handlers() -> dict[int, signal.Handlers]:
    def terminate(signum: int, frame: object) -> None:
        del frame
        raise _TerminationSignal(signum)

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


def _vite_manifest(cwd: Path) -> dict[str, object] | None:
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
    dependencies = value.get("dependencies")
    dev_dependencies = value.get("devDependencies")
    if not isinstance(scripts, dict) or not isinstance(scripts.get("dev"), str):
        return None
    if not any(
        isinstance(group, dict) and "vite" in group
        for group in (dependencies, dev_dependencies)
    ):
        return None
    return value


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
    return f"localhost-host-{digest}-{uuid.uuid4().hex[:8]}"
