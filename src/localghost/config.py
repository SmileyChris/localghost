"""Project run configuration."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

import click

from .feedback import warning
from .runner import SUPPORTED_TYPES, type_choices

CONFIG_NAME = ".localghost.toml"


@dataclass(frozen=True)
class RunConfig:
    type: str | None = None
    name: str | None = None
    root: str | None = None
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
    # `mode` is checked before the unknown-key scan would otherwise reject
    # it, so the migration message wins; keep it out of the known-key set.
    if "mode" in values:
        migration = (
            'type = "compose"'
            if values["mode"] == "compose"
            else "remove the key and let detection choose, or name the type"
        )
        raise click.ClickException(f"[run].mode was removed in 2.0; use {migration}")
    unknown = sorted(
        set(values) - {"type", "framework", "name", "root", "port", "command"}
    )
    if unknown:
        raise click.ClickException(f"unknown [run] setting '{unknown[0]}'")
    selected_type = values.get("type")
    if "framework" in values:
        if selected_type is not None:
            raise click.ClickException(
                "[run] sets both type and framework; keep type"
            )
        warning(
            "Deprecated configuration",
            ["[run].framework is deprecated; rename it to [run].type"],
        )
        selected_type = values["framework"]
    if selected_type is not None and selected_type not in SUPPORTED_TYPES:
        raise click.ClickException(type_choices("[run].type"))
    name = values.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        raise click.ClickException("[run].name must be a non-empty string")
    root = values.get("root")
    if root is not None and (not isinstance(root, str) or not root):
        raise click.ClickException("[run].root must be a non-empty string")
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
        selected_type,
        name,
        root,
        port,
        tuple(command),
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
        ("type", config.type),
        ("name", config.name),
        ("root", config.root),
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
