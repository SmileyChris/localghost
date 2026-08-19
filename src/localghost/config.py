"""Project run configuration and conservative mode detection."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

import click

from .runner import SUPPORTED_FRAMEWORKS, framework_choices, host_project_root

CONFIG_NAME = ".localghost.toml"


@dataclass(frozen=True)
class RunConfig:
    mode: str | None = None
    name: str | None = None
    framework: str | None = None
    port: int | None = None
    command: tuple[str, ...] = ()


def load_config(path: Path) -> RunConfig:
    if not path.exists():
        return RunConfig()
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise click.ClickException(f"could not read {path}: {exc}") from exc
    values = document.get("run", {})
    if not isinstance(values, dict):
        raise click.ClickException(f"'{path}' [run] must be a table")
    unknown = sorted(set(values) - {"mode", "name", "framework", "port", "command"})
    if unknown:
        raise click.ClickException(f"unknown [run] setting '{unknown[0]}'")
    mode = values.get("mode")
    if mode is not None and mode not in {"host", "compose"}:
        raise click.ClickException("[run].mode must be 'host' or 'compose'")
    name = values.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        raise click.ClickException("[run].name must be a non-empty string")
    framework = values.get("framework")
    if framework is not None and framework not in SUPPORTED_FRAMEWORKS:
        raise click.ClickException(framework_choices("[run].framework"))
    command = values.get("command", ())
    if not isinstance(command, (list, tuple)) or not all(
        isinstance(item, str) for item in command
    ):
        raise click.ClickException("[run].command must be an argv array of strings")
    port = values.get("port")
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise click.ClickException("[run].port must be between 1 and 65535")
    return RunConfig(
        mode,
        name,
        framework,
        port,
        tuple(command),
    )


def config_path(directory: Path, configured: Path | None = None) -> Path:
    return (configured or directory / CONFIG_NAME).resolve()


def detect_mode(
    directory: Path, *, command: tuple[str, ...] = (), framework: str | None = None
) -> str:
    if command or framework:
        return "host"
    compose = any(
        (directory / filename).is_file()
        for filename in (
            "compose.yaml",
            "compose.yml",
            "docker-compose.yaml",
            "docker-compose.yml",
        )
    )
    host_root = host_project_root(directory)
    if compose and host_root == directory.resolve():
        raise click.ClickException(
            "both Compose and a host framework were detected; provide "
            "--mode host or --mode compose"
        )
    if compose:
        # A Compose file beside the invocation wins over an application root
        # found further up the tree; the nearer marker is the specific one.
        return "compose"
    if host_root is not None:
        return "host"
    raise click.ClickException(
        "could not detect a run mode; provide --mode, a framework, or a "
        "command after --"
    )


def render_run_config(config: RunConfig, existing: str = "") -> str:
    lines = existing.rstrip()
    header = re.search(r"(?m)^\[run\]\s*$", lines)
    if header:
        following = re.search(r"(?m)^\[[^\n]+\]\s*$", lines[header.end() :])
        end = header.end() + (
            following.start() if following else len(lines[header.end() :])
        )
        lines = lines[: header.start()].rstrip() + (
            "\n\n" + lines[end:].lstrip() if lines[end:].strip() else ""
        )
    lines += ("\n\n" if lines.strip() else "") + "[run]\n"
    for key, value in (
        ("mode", config.mode),
        ("name", config.name),
        ("framework", config.framework),
    ):
        if value is not None:
            lines += f"{key} = {json.dumps(value)}\n"
    if config.port is not None:
        lines += f"port = {config.port}\n"
    if config.command:
        escaped = ", ".join(json.dumps(item) for item in config.command)
        lines += f"command = [{escaped}]\n"
    return lines


def write_run_config(
    path: Path, config: RunConfig, *, extend: bool = False, interactive: bool = False
) -> Path | None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if (
        path.exists()
        and not extend
        and (
            not interactive
            or not click.confirm(
                f"{path} already exists. Update it safely?", default=False
            )
        )
    ):
        raise click.ClickException(
            f"refusing to overwrite existing '{path}'; use --extend"
        )
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        counter = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak.{counter}")
            counter += 1
        shutil.copy2(path, backup)
    path.write_text(
        render_run_config(config, existing if extend else ""), encoding="utf-8"
    )
    return backup
