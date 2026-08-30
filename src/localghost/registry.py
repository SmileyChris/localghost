"""Filesystem registry of hostnames the hub has routed.

Ghost pages read this: the hub's fallback middleware serves a "project is
offline" page for any hostname remembered here. Entries persist until
`localghost forget` removes them; stale entries only make the page report
an old date.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .feedback import warning
from .paths import state_directory


@dataclass(frozen=True)
class RegistryEntry:
    hostname: str
    name: str
    directory: str
    type: str
    last_started: str


def registry_dir() -> Path:
    return state_directory() / "registry"


def record(name: str, directory: Path, run_type: str) -> None:
    """Remember that `name.localhost` routes to a project in `directory`.

    Best-effort: a run must never fail because its ghost could not be
    written.
    """
    entry = RegistryEntry(
        hostname=f"{name}.localhost",
        name=name,
        directory=str(directory),
        type=run_type,
        last_started=datetime.now(UTC)
        .astimezone()
        .isoformat(timespec="seconds"),
    )
    try:
        registry_dir().mkdir(parents=True, exist_ok=True)
        path = registry_dir() / f"{name}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(entry.__dict__, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except OSError as exc:
        warning("Could not record project for ghost pages", [str(exc)])


def entries() -> list[RegistryEntry]:
    result = []
    try:
        paths = sorted(registry_dir().glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result.append(
                RegistryEntry(
                    hostname=payload["hostname"],
                    name=payload["name"],
                    directory=payload["directory"],
                    type=payload["type"],
                    last_started=payload["last_started"],
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def forget(name: str) -> bool:
    path = registry_dir() / f"{name}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def forget_all() -> int:
    removed = 0
    for entry in entries():
        if forget(entry.name):
            removed += 1
    return removed
