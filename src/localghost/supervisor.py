"""The background process that keeps a terminal's host run alive without it.

An application whose output goes straight to a terminal cannot be moved off it
later. So a run started from a terminal lives here instead: this process runs
`runner.execute`, with the application on a pseudo-terminal it owns, and the
terminal is only a client connected over a Unix socket. Detaching, or the
terminal closing, just drops that connection; the application never notices.

Runs without a terminal stay in-process, where losing the caller should end
them, and `run --detach` writes to a file as it always has.

Usage: python -I -m localghost.supervisor, with a JSON spec on stdin.
"""

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
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path

import click

from . import registry, sessions
from .runner import RunPlan, execute

PROTOCOL_VERSION = 1

# Frame types, client to supervisor.
HELLO = b"h"  # {"version", "rows", "cols"}
INPUT = b"i"  # raw bytes for the application
RESIZE = b"w"  # {"rows", "cols"}
SIGNAL = b"s"  # {"signal": "SIGINT" or "SIGTERM"}, as if sent to the run
# Frame types, supervisor to client.
OUTPUT = b"o"  # raw bytes from the application
EXIT = b"x"  # {"status", "message"}
REFUSED = b"e"  # {"message"}

_HEADER = struct.Struct(">cI")
_MAXIMUM_FRAME = 1 << 24
_REPLAY_BYTES = 1 << 16
_READ_BYTES = 1 << 16
# A client this far behind is dropped rather than left to stall the application.
_CLIENT_BACKLOG = 1 << 20
_LOG_LIMIT = 16 << 20
_DRAIN_SECONDS = 0.05
_FLUSH_SECONDS = 2.0
_FIRST_CLIENT_SECONDS = 15.0


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return _HEADER.pack(kind, len(payload)) + payload


def control(kind: bytes, **fields: object) -> bytes:
    return frame(kind, json.dumps(fields).encode())


class FrameReader:
    """Reassembles frames from whatever chunks a stream socket delivers."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[bytes, bytes]]:
        self._buffer += data
        frames = []
        while len(self._buffer) >= _HEADER.size:
            kind, length = _HEADER.unpack_from(self._buffer)
            if length > _MAXIMUM_FRAME:
                raise ValueError(f"oversized frame of {length} bytes")
            end = _HEADER.size + length
            if len(self._buffer) < end:
                break
            frames.append((kind, bytes(self._buffer[_HEADER.size : end])))
            del self._buffer[:end]
        return frames


def rotate_log(log: Path) -> None:
    """Keep the previous log, which may hold the crash being investigated."""
    if log.exists() and log.stat().st_size:
        log.replace(log.with_name(log.name + ".1"))


def spec_for(
    plan: RunPlan, *, session_id: str, cwd: Path, socket_path: str, log: Path,
    rows: int, cols: int,
) -> dict[str, object]:  # fmt: skip
    return {
        "plan": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in asdict(plan).items()
        },
        "session": session_id,
        "cwd": str(cwd),
        "socket": socket_path,
        "log": str(log),
        "rows": rows,
        "cols": cols,
    }


def plan_from(spec: dict[str, object]) -> RunPlan:
    fields = dict(spec)
    fields["command"] = tuple(fields["command"])
    for key in ("project_root", "working_directory"):
        if fields[key] is not None:
            fields[key] = Path(fields[key])
    return RunPlan(**fields)


def _set_size(descriptor: int, rows: int, cols: int) -> None:
    # On the master, this also delivers SIGWINCH to the application.
    fcntl.ioctl(
        descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), cols, 0, 0)
    )


class _Client:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.reader = FrameReader()
        self.backlog = bytearray()


class _Server:
    """Moves bytes between the pseudo-terminal, the log, and any clients."""

    def __init__(
        self,
        master: int,
        listener: socket.socket,
        log: Path,
        session_id: str,
        name: str,
    ) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._master = master
        self._listener = listener
        self._log_path = log
        self._log = log.open("ab")
        self._session_id = session_id
        self._name = name
        self._clients: dict[socket.socket, _Client] = {}
        self._replay = bytearray()
        self._result: dict[str, object] | None = None
        self._wake_read, self._wake_write = os.pipe()
        self.attached_at_exit = False
        self.greeted = threading.Event()

    def finish(self, status: int, message: str | None) -> None:
        self._result = {"status": status, "message": message}
        os.write(self._wake_write, b".")

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join()

    def _run(self) -> None:
        try:
            self._loop()
        except Exception:
            # Nothing else can read the application's output or a client's keys,
            # so a run that lost this thread would hang with nobody told. Its
            # stderr is the pseudo-terminal this thread was reading.
            self._log.write(traceback.format_exc().encode())
            self._log.flush()
            os.kill(os.getpid(), signal.SIGTERM)

    def _loop(self) -> None:
        while True:
            readers = [self._master, self._listener, self._wake_read, *self._clients]
            writers = [c for c, item in self._clients.items() if item.backlog]
            timeout = _DRAIN_SECONDS if self._result else None
            readable, writable, _ = select.select(readers, writers, [], timeout)
            for source in readable:
                if source == self._master:
                    self._read_application()
                elif source == self._listener:
                    self._accept()
                elif source == self._wake_read:
                    os.read(self._wake_read, 64)
                else:
                    self._read_client(source)
            for connection in writable:
                if connection in self._clients:
                    self._flush(self._clients[connection])
            if self._result and self._master not in readable:
                self._say_goodbye()
                return

    def _read_application(self) -> None:
        try:
            data = os.read(self._master, _READ_BYTES)
        except OSError:
            return
        self._write_log(data)
        self._replay += data
        del self._replay[: -_REPLAY_BYTES]
        for client in list(self._clients.values()):
            self._send(client, frame(OUTPUT, data))

    def _write_log(self, data: bytes) -> None:
        self._log.write(data)
        self._log.flush()
        if self._log.tell() > _LOG_LIMIT:
            self._log.close()
            rotate_log(self._log_path)
            self._log = self._log_path.open("ab")

    def _accept(self) -> None:
        connection, _ = self._listener.accept()
        connection.setblocking(False)
        self._clients[connection] = _Client(connection)

    def _read_client(self, connection: socket.socket) -> None:
        client = self._clients[connection]
        try:
            data = connection.recv(_READ_BYTES)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._drop(client)
            return
        try:
            frames = client.reader.feed(data)
        except ValueError:
            self._drop(client)
            return
        for kind, payload in frames:
            self._handle(client, kind, payload)

    def _handle(self, client: _Client, kind: bytes, payload: bytes) -> None:
        if kind == INPUT:
            self._type(payload)
            return
        fields = json.loads(payload)
        if kind == HELLO:
            if fields.get("version") != PROTOCOL_VERSION:
                self._send(
                    client,
                    control(
                        REFUSED,
                        message=(
                            f"{self._name} was started by a different version of "
                            "localghost, so it cannot be attached to; restart it."
                        ),
                    ),
                )
                return
            _set_size(self._master, fields["rows"], fields["cols"])
            self._send(client, frame(OUTPUT, self._replayable()))
            self._mark_detached(False)
            self.greeted.set()
        elif kind == RESIZE:
            _set_size(self._master, fields["rows"], fields["cols"])
        elif kind == SIGNAL and fields.get("signal") in ("SIGINT", "SIGTERM"):
            # To this process, not the application: `execute` owns stopping it,
            # including the grace period before a repeat force-quits.
            os.kill(os.getpid(), getattr(signal, fields["signal"]))

    def _type(self, data: bytes) -> None:
        with contextlib.suppress(OSError):
            os.write(self._master, data)

    def _replayable(self) -> bytes:
        replay = bytes(self._replay)
        if len(replay) < _REPLAY_BYTES:
            return replay
        # Cut off mid-line, and perhaps mid escape sequence; start at a clean one.
        return replay[replay.find(b"\n") + 1 :]

    def _send(self, client: _Client, data: bytes) -> None:
        client.backlog += data
        if len(client.backlog) > _CLIENT_BACKLOG:
            self._drop(client)
            return
        self._flush(client)

    def _flush(self, client: _Client) -> None:
        try:
            sent = client.connection.send(client.backlog)
        except BlockingIOError:
            return
        except OSError:
            self._drop(client)
            return
        del client.backlog[:sent]

    def _drop(self, client: _Client) -> None:
        self._clients.pop(client.connection, None)
        client.connection.close()
        if not self._clients and self._result is None:
            self._mark_detached(True)

    def _mark_detached(self, detached: bool) -> None:
        sessions.mark_detached(self._session_id, detached)
        registry.update(self._name, detached=detached)

    def _say_goodbye(self) -> None:
        self.attached_at_exit = bool(self._clients)
        assert self._result is not None
        for client in list(self._clients.values()):
            self._send(client, control(EXIT, **self._result))
        deadline = time.monotonic() + _FLUSH_SECONDS
        while time.monotonic() < deadline:
            waiting = [c for c, item in self._clients.items() if item.backlog]
            if not waiting:
                return
            _, writable, _ = select.select([], waiting, [], _DRAIN_SECONDS)
            for connection in writable:
                if connection in self._clients:
                    self._flush(self._clients[connection])


def _take_terminal() -> None:
    """Runs in the child: make its pseudo-terminal its controlling terminal.

    Without one it has no /dev/tty, so a password prompt has nowhere to go.
    """
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def serve(spec: dict[str, object]) -> int:
    plan = plan_from(spec["plan"])
    # Replaced before any application could be started, and only ever a no-op
    # function, not SIG_IGN, which the application would inherit.
    signal.signal(signal.SIGHUP, lambda *_: None)
    os.environ.pop("LINES", None)
    os.environ.pop("COLUMNS", None)
    master, terminal = os.openpty()
    _set_size(master, spec["rows"], spec["cols"])
    listener = socket.socket(socket.AF_UNIX)
    socket_path = Path(spec["socket"])
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    listener.listen()
    server = _Server(
        master, listener, Path(spec["log"]), spec["session"], plan.name
    )
    # Everything this process says goes where the application's output goes, so
    # hub and bridge progress reach the terminal and the log the same way.
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(terminal, 1)
    os.dup2(terminal, 2)
    server.start()
    # A failure to start has only the first client to tell; don't outrun it.
    server.greeted.wait(_FIRST_CLIENT_SECONDS)

    def spawn(launch: list[str], *, cwd: Path) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            launch,
            cwd=cwd,
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
            start_new_session=True,
            preexec_fn=_take_terminal,
        )

    message = None
    try:
        status = execute(
            plan,
            lambda: None,
            cwd=Path(spec["cwd"]),
            status_bar=False,
            spawn=spawn,
        )
    except click.ClickException as exc:
        status, message = exc.exit_code, exc.format_message()
    except click.exceptions.Exit as exc:
        status = exc.exit_code
    except Exception:
        traceback.print_exc()
        status = 1
    # `execute` has put the handlers back; a late signal must not lose the rest.
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: None)
    sys.stdout.flush()
    sys.stderr.flush()
    server.finish(status, message)
    server.join()
    listener.close()
    socket_path.unlink(missing_ok=True)
    if server.attached_at_exit:
        sessions.discard_by_id(spec["session"])
    else:
        # Nobody saw how it ended, so the record stays to find its log by.
        sessions.mark_ended(spec["session"])
    return status


def main() -> int:
    return serve(json.load(sys.stdin))


if __name__ == "__main__":
    sys.exit(main())
