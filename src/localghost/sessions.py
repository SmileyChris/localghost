"""Filesystem-backed metadata for running Localghost applications.

`run --detach` records the application's process group. A foreground run
records the process that owns it, localghost or its supervisor, so other
terminals can see and stop it; `socket` marks a supervised one, which `detached`
then says whether a terminal is attached to.
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

from .feedback import COMMAND, info, warning
from .paths import state_directory

_STOP_GRACE_SECONDS = 10


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
    # Ran in its own network namespace, so `port` is only the host side.
    isolated: bool = False
    # Where a supervised run's terminal connects; empty for any other.
    socket: str = ""

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
    isolated: bool = False,
    socket: str = "",
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
        isolated,
        socket,
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


def _read(session_id: str) -> Session | None:
    try:
        payload = json.loads(_path(session_id).read_text(encoding="utf-8"))
        return Session(**payload)
    except (OSError, ValueError, TypeError):
        return None


def mark_detached(session_id: str, detached: bool) -> None:
    """Note whether a terminal is attached; a stopped record stays gone."""
    session = _read(session_id)
    if session is not None:
        session.detached = detached
        save(session)


def mark_ended(session_id: str) -> None:
    """Keep a finished run's record, with its log, once its route has gone."""
    session = _read(session_id)
    if session is not None:
        session.pid = None
        session.bridge_project = None
        session.bridge_yaml = ""
        session.socket = ""
        session.detached = True
        save(session)


def _remove(session: Session) -> None:
    # A supervisor that was killed outright left its socket behind.
    if session.socket:
        Path(session.socket).unlink(missing_ok=True)
    _path(session.id).unlink(missing_ok=True)


def discard_by_id(session_id: str) -> None:
    _path(session_id).unlink(missing_ok=True)


def socket_path_for(name: str) -> str:
    """A path for a supervisor's socket, kept short for the 104-byte limit.

    An explicit state directory wins, so an isolated test never shares a
    directory with the real sessions.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and not os.environ.get("LOCALGHOST_STATE_DIR"):
        directory = Path(runtime) / "localghost"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    else:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{name[:32]}-{uuid.uuid4().hex[:8]}.sock")


def stop(session: Session) -> None:
    if session.mode == "host" and session.pid and alive(session):
        # A `run --detach` record holds the application's process group. Any
        # other holds the process that owns the application, which tears it
        # and its bridge down on SIGTERM. Only an application that ignores the
        # signal waits out the grace period; the rest return when it exits.
        kill = os.killpg if session.detached and not session.socket else os.kill
        with suppress(ProcessLookupError, PermissionError):
            kill(session.pid, signal.SIGTERM)
        deadline = time.monotonic() + _STOP_GRACE_SECONDS
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
    _remove(session)


def clean(name: str | None = None) -> int:
    """Remove stopped records, every name's or just `name`'s."""
    removed = 0
    for session in sessions():
        if (name is None or session.name == name) and not alive(session):
            if session.bridge_project:
                _stop_bridge(session)
            _remove(session)
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
        info(f"Its log is kept: {COMMAND} sessions logs {names[0]}", err=True)
    else:
        info(f"Their logs are kept: {COMMAND} sessions logs NAME", err=True)
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
