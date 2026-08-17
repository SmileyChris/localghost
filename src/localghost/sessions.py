"""Filesystem-backed metadata for detached Localghost runs."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


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

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


def state_dir() -> Path:
    return (
        Path(
            os.environ.get(
                "LOCALGHOST_STATE_DIR", Path.home() / ".local" / "state" / "localghost"
            )
        )
        / "sessions"
    )


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
    for path in sorted(state_dir().glob("*.json")):
        try:
            result.append(Session(**json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    return result


def create(
    *,
    mode: str,
    name: str,
    port: int,
    cwd: Path,
    command: tuple[str, ...],
    log: Path,
    pid: int | None,
    project: str | None = None,
    bridge_project: str | None = None,
) -> Session:
    session = Session(
        uuid.uuid4().hex[:8],
        mode,
        name,
        port,
        str(cwd),
        pid,
        str(log),
        project,
        bridge_project,
        command,
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


def find_matching(*, name: str, cwd: Path) -> Session | None:
    return next(
        (
            item
            for item in sessions()
            if item.name == name
            and Path(item.cwd).resolve() == cwd.resolve()
            and alive(item)
        ),
        None,
    )


def stop(session: Session) -> None:
    if session.mode == "host" and session.pid and alive(session):
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(session.pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while alive(session) and time.monotonic() < deadline:
            time.sleep(0.05)
        if alive(session):
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(session.pid, signal.SIGKILL)
    elif session.mode == "compose" and session.project:
        subprocess.run(
            ["docker", "compose", "--project-name", session.project, "down"],
            cwd=session.cwd,
            check=False,
        )
    if session.bridge_project:
        subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                session.bridge_project,
                "down",
                "--remove-orphans",
            ],
            check=False,
            capture_output=True,
        )
    _path(session.id).unlink(missing_ok=True)


def clean() -> int:
    removed = 0
    for session in sessions():
        if not alive(session):
            if session.bridge_project:
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "--project-name",
                        session.bridge_project,
                        "down",
                        "--remove-orphans",
                    ],
                    check=False,
                    capture_output=True,
                )
            _path(session.id).unlink(missing_ok=True)
            removed += 1
    return removed
