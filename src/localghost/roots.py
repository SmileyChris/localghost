"""Project root and configuration discovery."""

from __future__ import annotations

from pathlib import Path

import click

from .config import CONFIG_NAME
from .runner import VCS_MARKERS, _search_path


def discover_config(start: Path) -> Path | None:
    """The nearest .localghost.toml within the bounded search, or None.

    Unlike type detection, this executes whatever argv a discovered file
    names, so it must never wander into a directory the invoker doesn't
    control. `_search_path` alone isn't a tight enough bound for that: absent
    a VCS marker, it still returns every ancestor up to (but excluding) $HOME,
    which includes world-writable shared directories such as /tmp. Without a
    VCS marker anywhere in that search, only the invocation directory itself
    is therefore a candidate; type detection keeps walking regardless, since
    it only inspects files rather than executing them.
    """
    resolved = start.resolve()
    candidates = _search_path(resolved)
    has_vcs_marker = any(
        (candidate / marker).exists()
        for candidate in candidates
        for marker in VCS_MARKERS
    )
    if not has_vcs_marker:
        candidates = candidates[:1]
    for candidate in candidates:
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
        return _validated(Path(configured), base, "[run].root")
    if config_dir is not None:
        return config_dir.resolve()
    return None


def _validated(value: Path, base: Path, source: str) -> Path:
    resolved = (base / value).resolve() if not value.is_absolute() else value.resolve()
    if not resolved.exists():
        raise click.ClickException(
            f"{source} '{value}' (resolved against '{base}') does not exist; "
            "provide a path to an existing directory"
        )
    if not resolved.is_dir():
        raise click.ClickException(
            f"{source} '{value}' (resolved against '{base}') is not a directory; "
            "provide a directory path"
        )
    return resolved
