"""Filesystem-backed metadata for running Localghost applications.

Detached runs record the application's process group; foreground runs record
the localghost process itself, so other terminals can see and stop them.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import click

from .feedback import info, warning
from .paths import state_directory


@dataclass
class Session:
    id: str
    mode: str
    name: str
    port: int
    cwd: str
    pid: int | None
    log: str
    project: str | None = None
    bridge_project: str | None = None
    command: tuple[str, ...] = ()
    bridge_yaml: str = ""
    detached: bool = True

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def state_dir() -> Path:
    return state_directory() / "sessions"


def _path(session_id: str) -> Path:
    return state_dir() / f"{session_id}.json"


def save(session: Session) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    temporary = _path(session.id).with_suffix(".tmp")
    temporary.write_text(
        json.dumps(session.as_dict(), indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(_path(session.id))


def sessions() -> list[Session]:
    result = []
    unreadable = []
    for path in sorted(state_dir().glob("*.json")):
        try:
            result.append(Session(**json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError) as exc:
            # Dropping the record silently would hide a still-running
            # application from `sessions list`, `stop`, and `clean`.
            unreadable.append(f"{path.name}: {exc}")
    if unreadable:
        warning("Unreadable session records", unreadable)
    return result


def create(
    *,
    mode: str,
    name: str,
    port: int,
    cwd: Path,
    command: tuple[str, ...],
    log: Path | None,
    pid: int | None,
    project: str | None = None,
    bridge_project: str | None = None,
    bridge_yaml: str = "",
    detached: bool = True,
) -> Session:
    session = Session(
        uuid.uuid4().hex[:8],
        mode,
        name,
        port,
        str(cwd),
        pid,
        str(log) if log else "",
        project,
        bridge_project,
        command,
        bridge_yaml,
        detached,
    )
    save(session)
    return session


def alive(session: Session) -> bool:
    if session.mode == "compose" and session.project:
        result = subprocess.run(
            ["docker", "compose", "--project-name", session.project, "ps", "-q"],
            cwd=session.cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    if not session.pid:
        return False
    try:
        os.kill(session.pid, 0)
    except OSError:
        return False
    return True


def live(name: str) -> Session | None:
    """The running session serving `name`; a hostname is only served once."""
    return next(
        (item for item in sessions() if item.name == name and alive(item)), None
    )


def find(target: str) -> Session | None:
    """Resolve an exact session ID, else a project name.

    A name resolves to its running session, else its most recent record.
    """
    records = sessions()
    exact = next((item for item in records if item.id == target), None)
    if exact is not None:
        return exact
    named = [item for item in records if item.name == target]
    running = next((item for item in named if alive(item)), None)
    if running is not None or not named:
        return running
    return max(named, key=_recorded_at)


def _recorded_at(session: Session) -> float:
    try:
        return _path(session.id).stat().st_mtime
    except OSError:
        return 0.0


def discard(session: Session) -> None:
    """Drop a record whose application has already shut itself down."""
    _path(session.id).unlink(missing_ok=True)


def stop(session: Session) -> None:
    if session.mode == "host" and session.pid and alive(session):
        # A detached record holds the application's process group. A
        # foreground one holds the localghost process, which tears down its
        # application and bridge on SIGTERM, so it gets longer to do that.
        kill = os.killpg if session.detached else os.kill
        with suppress(ProcessLookupError, PermissionError):
            kill(session.pid, signal.SIGTERM)
        deadline = time.monotonic() + (2 if session.detached else 10)
        while alive(session) and time.monotonic() < deadline:
            time.sleep(0.05)
        if alive(session):
            with suppress(ProcessLookupError, PermissionError):
                kill(session.pid, signal.SIGKILL)
        if alive(session):
            # Removing the record here would orphan the process: nothing else
            # remembers its pid, bridge, or log.
            raise click.ClickException(
                f"session {session.id} did not stop; process {session.pid} "
                "is still running"
            )
    elif session.mode == "compose" and session.project:
        subprocess.run(
            ["docker", "compose", "--project-name", session.project, "down"],
            cwd=session.cwd,
            check=False,
        )
    if session.bridge_project:
        _stop_bridge(session)
    _path(session.id).unlink(missing_ok=True)


def clean(name: str | None = None) -> int:
    """Remove stopped records, every name's or just `name`'s."""
    removed = 0
    for session in sessions():
        if (name is None or session.name == name) and not alive(session):
            if session.bridge_project:
                _stop_bridge(session)
            _path(session.id).unlink(missing_ok=True)
            removed += 1
    return removed


def reap() -> int:
    """Take down bridges left behind by sessions whose application exited.

    A detached application that crashes, or every one after a reboot, leaves
    its bridge running, and the hub keeps routing to it. The record stays so
    its log can still be read; only the route goes.
    """
    # ponytail: runs only when the CLI is used, so a crashed app shows as
    # running on the hub until then; supervise the detached process to close
    # that gap if it matters.
    stale = [item for item in sessions() if item.bridge_project and not alive(item)]
    if not stale:
        return 0
    # `sessions stop` and `restart` remove their own bridges, so whatever is
    # left ended some other way: a crash, an exit, a kill, or a reboot.
    names = sorted({item.name for item in stale})
    listed = names[0] if len(names) == 1 else (
        f"{', '.join(names[:-1])} and {names[-1]}"
    )
    routes = "route" if len(names) == 1 else "routes"
    info(
        f"Removing {routes} for {listed}, which stopped outside localghost…",
        err=True,
    )
    # Each bridge is its own Compose project, so they come down together.
    with ThreadPoolExecutor() as pool:
        list(pool.map(_stop_bridge, stale))
    for session in stale:
        session.bridge_project = None
        session.bridge_yaml = ""
        save(session)
    if len(names) == 1:
        info(f"Its log is kept: localghost sessions logs {names[0]}", err=True)
    else:
        info("Their logs are kept: localghost sessions logs NAME", err=True)
    return len(stale)


def _stop_bridge(session: Session) -> None:
    command = [
        "docker",
        "compose",
        "--project-name",
        session.bridge_project,
        "--file",
        "-",
        "down",
        "--remove-orphans",
    ]
    subprocess.run(
        command,
        input=session.bridge_yaml,
        text=True,
        check=False,
        capture_output=True,
    )
