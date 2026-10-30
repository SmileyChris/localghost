"""The terminal's side of a supervised run: the bar, the keys, the output."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click

from . import statusbar, supervisor
from .runner import (
    RunPlan,
    _claim_detach_key,
    _detach_key_pressed,
    _install_termination_handlers,
    _restore_termination_handlers,
    _TerminationSignal,
    detach_key_available,
)
from .sessions import Session, alive, create, socket_path_for, state_dir
from .sessions import stop as stop_session

_CTRL_C = 0x03
_CTRL_Z = 0x1a
_POLL_SECONDS = 0.1
_CONNECT_SECONDS = 15.0
_FOLLOW_SECONDS = 0.1
_TAIL_LINES = 40


@dataclass(frozen=True)
class Outcome:
    """How an attached run ended. No status means it is still running."""

    status: int | None
    message: str | None = None


def supervised_run_available() -> bool:
    return detach_key_available() and not os.environ.get("LOCALGHOST_LEGACY_RUN")


def _terminal_rows_cols(bar: bool) -> tuple[int, int]:
    """The size to give the application: a row short of the terminal under a bar.

    A full-screen application then draws above the bar rather than over it.
    """
    packed = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
    rows, cols, _, _ = struct.unpack("HHHH", packed)
    if not rows or not cols:
        cols, rows = statusbar._terminal_size()
    return rows - 1 if bar else rows, cols


def _readiness_probe(port: int, isolated: bool) -> Callable[[], bool]:
    # pasta accepts on the host port before the application is up, so only an
    # HTTP answer shows an isolated run is ready.
    if isolated:
        return statusbar.http_probe(f"http://127.0.0.1:{port}/")
    return statusbar.tcp_probe(port)


def _relay(connection: socket.socket, *, bar: bool) -> Outcome:
    """Join the terminal to the supervisor until the run ends or the user leaves.

    The terminal is raw, so the application's own terminal decides what keys
    mean. Ctrl+C and Ctrl+Z are the exceptions and never reach it, as Ctrl+C
    never did: it stops the run, whose whole process group gets the signal.
    """
    stdin = sys.stdin.fileno()
    stdout = sys.stdout.fileno()
    size = _terminal_rows_cols(bar)
    sys.stdout.flush()
    sys.stderr.flush()
    # Signals from elsewhere mean what they did: `kill` on the run ends the run.
    requested: list[str] = []

    def request(signum: int, frame: object) -> None:
        del frame
        requested.append(signal.Signals(signum).name)

    previous = {
        signum: signal.signal(signum, request)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    reader = supervisor.FrameReader()
    saved = termios.tcgetattr(stdin)
    tty.setraw(stdin, termios.TCSADRAIN)
    try:
        connection.sendall(
            supervisor.control(
                supervisor.HELLO,
                version=supervisor.PROTOCOL_VERSION,
                rows=size[0],
                cols=size[1],
            )
        )
        while True:
            while requested:
                connection.sendall(
                    supervisor.control(supervisor.SIGNAL, signal=requested.pop(0))
                )
            current = _terminal_rows_cols(bar)
            if current != size:
                size = current
                connection.sendall(
                    supervisor.control(supervisor.RESIZE, rows=size[0], cols=size[1])
                )
            readable, _, _ = select.select([stdin, connection], [], [], _POLL_SECONDS)
            if stdin in readable:
                typed = os.read(stdin, 4096)
                if not typed:
                    return Outcome(None)
                forward = bytearray()
                for byte in typed:
                    if byte == _CTRL_Z:
                        return Outcome(None)
                    if byte == _CTRL_C:
                        requested.append("SIGINT")
                    else:
                        forward.append(byte)
                if forward:
                    connection.sendall(
                        supervisor.frame(supervisor.INPUT, bytes(forward))
                    )
            if connection in readable:
                data = connection.recv(1 << 16)
                if not data:
                    return Outcome(None)
                for kind, payload in reader.feed(data):
                    if kind == supervisor.OUTPUT:
                        _write_all(stdout, payload)
                    elif kind == supervisor.EXIT:
                        ended = json.loads(payload)
                        return Outcome(ended["status"], ended["message"])
                    elif kind == supervisor.REFUSED:
                        return Outcome(1, json.loads(payload)["message"])
    except OSError:
        # The terminal or the supervisor went away; either way this client is done.
        return Outcome(None)
    finally:
        with contextlib.suppress(termios.error, OSError):
            termios.tcsetattr(stdin, termios.TCSADRAIN, saved)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        connection.close()


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


def _connect(path: str, process: subprocess.Popen[bytes], log: Path) -> socket.socket:
    deadline = time.monotonic() + _CONNECT_SECONDS
    while time.monotonic() < deadline:
        connection = socket.socket(socket.AF_UNIX)
        try:
            connection.connect(path)
            return connection
        except OSError:
            connection.close()
        if process.poll() is not None:
            break
        time.sleep(0.02)
    tail = (
        log.read_text(encoding="utf-8", errors="replace")[-2000:]
        if log.exists()
        else ""
    )
    raise click.ClickException(
        "could not start the background process for this run"
        + (f":\n{tail.strip()}" if tail.strip() else "")
    )


def run_supervised(
    plan: RunPlan,
    start_proxy: Callable[[], None],
    *,
    cwd: Path,
    log: Path,
    public_origin: str,
    secondary_origin: str | None,
    status_bar: bool,
) -> Outcome:
    """Start `plan` under a supervisor and attach this terminal to it."""
    bar = statusbar.supported(sys.stdout, enabled=status_bar)
    with statusbar.pinned(
        public_origin,
        secondary_url=secondary_origin,
        enabled=status_bar,
        probe=_readiness_probe(plan.port, plan.app_port is not None),
        message="starting hub",
        detachable=True,
    ) as pinned:
        old_handlers = _install_termination_handlers()
        try:
            start_proxy()
        except _TerminationSignal as interrupted:
            return Outcome(128 + interrupted.signum)
        finally:
            _restore_termination_handlers(old_handlers)
        pinned.status("starting")
        state_dir().mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        supervisor.rotate_log(log)
        path = socket_path_for(plan.name)
        rows, cols = _terminal_rows_cols(bar)
        with log.open("ab") as handle:
            # Its output moves to the pseudo-terminal once it is up; until then
            # a failure to start at all lands here, for `_connect` to report.
            process = subprocess.Popen(
                [sys.executable, "-I", "-m", "localghost.supervisor"],
                stdin=subprocess.PIPE,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        # The record holds the supervisor: signalling it ends the run the way
        # signalling an in-process foreground run does.
        session = create(
            mode="host",
            name=plan.name,
            port=plan.port,
            cwd=plan.project_root or cwd,
            command=plan.command,
            log=log,
            pid=process.pid,
            bridge_project=plan.project,
            bridge_yaml=plan.bridge_yaml,
            detached=False,
            isolated=plan.app_port is not None,
            socket=path,
        )
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                supervisor.spec_for(
                    plan,
                    session_id=session.id,
                    cwd=cwd,
                    socket_path=path,
                    log=log,
                    rows=rows,
                    cols=cols,
                )
            ).encode()
        )
        process.stdin.close()
        return _relay(_connect(path, process, log), bar=bar)


def attach(
    session: Session, *, public_origin: str, secondary_origin: str | None
) -> Outcome:
    """Bring a running session into this terminal."""
    if session.socket:
        connection = socket.socket(socket.AF_UNIX)
        try:
            connection.connect(session.socket)
        except OSError as exc:
            connection.close()
            raise click.ClickException(
                f"could not reach {session.name}'s background process: {exc}"
            ) from exc
    else:
        connection = None
    bar = statusbar.supported(sys.stdout)
    with statusbar.pinned(
        public_origin,
        secondary_url=secondary_origin,
        probe=_readiness_probe(session.port, session.isolated),
        detachable=True,
    ):
        if connection is not None:
            return _relay(connection, bar=bar)
        return _follow_log(session)


def _follow_log(session: Session) -> Outcome:
    """Attach to a `run --detach` application, which only has a log to offer.

    Nothing can be typed into it. Ctrl+C stops it, Ctrl+Z leaves it running.
    """
    old_handlers = _install_termination_handlers()
    try:
        with _claim_detach_key() as key, Path(session.log).open("rb") as log:
            lines = log.read().splitlines(keepends=True)[-_TAIL_LINES:]
            _write_all(sys.stdout.fileno(), b"".join(lines))
            while True:
                data = log.read(1 << 16)
                if data:
                    _write_all(sys.stdout.fileno(), data)
                    continue
                if not alive(session):
                    return Outcome(0)
                pressed, key = _detach_key_pressed(key)
                if pressed:
                    return Outcome(None)
                if key is None:
                    time.sleep(_FOLLOW_SECONDS)
    except _TerminationSignal as interrupted:
        stop_session(session)
        return Outcome(128 + interrupted.signum)
    finally:
        _restore_termination_handlers(old_handlers)
