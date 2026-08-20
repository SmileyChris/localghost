"""Project root and configuration discovery."""

from __future__ import annotations

from pathlib import Path

import click

from .config import CONFIG_NAME
from .runner import _search_path


def discover_config(start: Path) -> Path | None:
    """The nearest .localghost.toml within the bounded search, or None."""
    for candidate in _search_path(start.resolve()):
        path = candidate / CONFIG_NAME
        if path.is_file():
            return path
    return None


def resolve_root(
    *,
    start: Path,
    flag: Path | None,
    configured: str | None,
    config_dir: Path | None,
) -> Path | None:
    """The pinned project root, or None when detection should walk.

    Precedence: the --root flag, then [run].root, then the directory holding
    the discovered configuration file.
    """
    if flag is not None:
        return _validated(flag, Path.cwd(), "--root")
    if configured is not None:
        base = config_dir or start
        return _validated(Path(configured), base, f"[run].root in {base}")
    if config_dir is not None:
        return config_dir.resolve()
    return None


def _validated(value: Path, base: Path, source: str) -> Path:
    resolved = (base / value).resolve() if not value.is_absolute() else value.resolve()
    if not resolved.exists():
        raise click.ClickException(
            f"{source}: '{value}' does not exist (resolved against '{base}')"
        )
    if not resolved.is_dir():
        raise click.ClickException(f"{source}: '{value}' is not a directory")
    return resolved
