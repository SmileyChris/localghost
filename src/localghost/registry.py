"""Filesystem registry of hostnames the hub has routed.

Ghost pages read this: the hub's fallback middleware serves a "project is
offline" page for any hostname remembered here. Entries persist until
`localghost sessions forget` removes them; stale entries only make the page report
an old date.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .feedback import warning
from .paths import state_directory


@dataclass(frozen=True)
class RunFlags:
    """What `run` was explicitly told, so `restart` can tell it again.

    Only flags: what detection or a config file chose is worked out afresh,
    so editing the config still takes effect on the next restart.
    """

    type: str | None = None
    root: str | None = None
    port: int | None = None
    config: str | None = None
    isolate: bool = False
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegistryEntry:
    hostname: str
    name: str
    directory: str
    type: str
    last_started: str
    # How the project last ran, so `restart` can bring it back the same way.
    detached: bool = False
    # Whether it last ran in its own network namespace, for the hub's page.
    isolated: bool = False
    flags: RunFlags = RunFlags()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def registry_dir() -> Path:
    return state_directory() / "registry"


def record(
    name: str,
    directory: Path,
    run_type: str,
    *,
    detached: bool | None = None,
    isolated: bool | None = None,
    flags: RunFlags | None = None,
) -> None:
    """Remember that `name.localhost` routes to a project in `directory`.

    `detached` and `isolated` are how it was started; `None` (a save, not a
    run) keeps what was last recorded. `flags` are always replaced: a save
    leaves none, as the config it wrote now holds them. Best-effort: a run
    must never fail because its ghost could not be written.
    """
    previous = next((item for item in entries() if item.name == name), None)
    if detached is None:
        detached = previous.detached if previous else False
    if isolated is None:
        isolated = previous.isolated if previous else False
    entry = RegistryEntry(
        hostname=f"{name}.localhost",
        name=name,
        directory=str(directory),
        type=run_type,
        last_started=datetime.now(UTC)
        .astimezone()
        .isoformat(timespec="seconds"),
        detached=detached,
        isolated=isolated,
        flags=flags or RunFlags(),
    )
    try:
        _write(entry)
    except OSError as exc:
        warning("Could not record project for ghost pages", [str(exc)])


def restore(entry: RegistryEntry) -> None:
    """Re-write a previously read entry verbatim (undo for `forget`)."""
    try:
        _write(entry)
    except OSError as exc:
        warning("Could not restore project for ghost pages", [str(exc)])


def _write(entry: RegistryEntry) -> None:
    registry_dir().mkdir(parents=True, exist_ok=True)
    path = registry_dir() / f"{entry.name}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(asdict(entry), indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _read_flags(payload: dict[str, object]) -> RunFlags:
    return RunFlags(
        type=payload.get("type"),
        root=payload.get("root"),
        port=payload.get("port"),
        config=payload.get("config"),
        isolate=payload.get("isolate") is True,
        command=tuple(payload.get("command") or ()),
    )


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
                    detached=payload.get("detached") is True,
                    isolated=payload.get("isolated") is True,
                    flags=_read_flags(payload.get("flags") or {}),
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def forget(name: str) -> bool:
    if (
        not name
        or "/" in name
        or "\\" in name
        or Path(name).name != name
        or name in {".", ".."}
    ):
        return False
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
