"""The terminal's side of a supervised run, on a real pseudo-terminal."""

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
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from localghost import attach, runner, sessions, statusbar, supervisor


class _Terminal:
    """Stands in for the user's terminal, with sys.stdin and stdout on it."""

    def __init__(self, monkeypatch) -> None:
        self.user, self.program = os.openpty()
        self.resize(30, 120)
        self.stdin = os.fdopen(os.dup(self.program), "r")
        self.stdout = os.fdopen(os.dup(self.program), "w")
        # Not on `sys` itself, whose streams pytest's capture swaps between a
        # fixture and its test.
        stand_in = SimpleNamespace(
            stdin=self.stdin,
            stdout=self.stdout,
            stderr=self.stdout,
            executable=sys.executable,
        )
        monkeypatch.setattr(attach, "sys", stand_in)
        monkeypatch.setattr(runner, "sys", stand_in)

    def resize(self, rows: int, cols: int) -> None:
        fcntl.ioctl(
            self.user, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )

    def press(self, data: bytes) -> None:
        os.write(self.user, data)

    def shown(self, seconds: float = 0.2) -> bytes:
        output = b""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.user], [], [], 0.02)[0]:
                output += os.read(self.user, 65536)
        return output

    def modes(self) -> list:
        return termios.tcgetattr(self.program)

    def close(self) -> None:
        self.stdin.close()
        self.stdout.close()
        os.close(self.user)
        os.close(self.program)


@pytest.fixture
def terminal(monkeypatch):
    opened = _Terminal(monkeypatch)
    yield opened
    opened.close()


class _FakeSupervisor(threading.Thread):
    """Records frames from the client; `script` says what to do on each."""

    def __init__(self, connection: socket.socket, script) -> None:
        super().__init__(daemon=True)
        self.connection = connection
        self.script = script
        self.frames: list[tuple[bytes, bytes]] = []

    def run(self) -> None:
        reader = supervisor.FrameReader()
        while True:
            try:
                data = self.connection.recv(65536)
            except OSError:
                return
            if not data:
                return
            for kind, payload in reader.feed(data):
                self.frames.append((kind, payload))
                self.script(self.connection, kind, payload)

    def kinds(self, kind: bytes) -> list[bytes]:
        return [payload for seen, payload in self.frames if seen == kind]


def _pair(script=lambda *a: None) -> tuple[socket.socket, _FakeSupervisor]:
    client, server = socket.socketpair()
    fake = _FakeSupervisor(server, script)
    fake.start()
    return client, fake


def _relay_in_background(client, bar=False) -> dict:
    """Run the relay on a thread; handlers are stubbed as only main can set them."""
    result: dict = {}

    def run() -> None:
        result["outcome"] = attach._relay(client, bar=bar)

    thread = threading.Thread(target=run, daemon=True)
    result["thread"] = thread
    thread.start()
    return result


@pytest.fixture
def no_signal_handlers(monkeypatch):
    monkeypatch.setattr(attach.signal, "signal", lambda signum, handler: None)


def _eventually(check, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    assert check()


def _finished(result: dict) -> attach.Outcome:
    result["thread"].join(5)
    assert not result["thread"].is_alive()
    return result["outcome"]


def test_ctrl_z_leaves_and_restores_the_terminal(
    terminal, no_signal_handlers
) -> None:
    before = terminal.modes()
    client, fake = _pair()
    result = _relay_in_background(client)
    _eventually(lambda: fake.kinds(supervisor.HELLO))

    terminal.press(b"\x1a")

    assert _finished(result).status is None
    assert terminal.modes() == before


def test_hello_says_the_version_and_a_row_short_of_the_terminal_under_a_bar(
    terminal, no_signal_handlers
) -> None:
    client, fake = _pair()
    result = _relay_in_background(client, bar=True)
    _eventually(lambda: fake.kinds(supervisor.HELLO))

    hello = json.loads(fake.kinds(supervisor.HELLO)[0])

    assert hello == {
        "version": supervisor.PROTOCOL_VERSION, "rows": 29, "cols": 120,
    }  # fmt: skip
    terminal.press(b"\x1a")
    _finished(result)


def test_typing_reaches_the_application_except_the_two_keys(
    terminal, no_signal_handlers
) -> None:
    client, fake = _pair()
    result = _relay_in_background(client)
    _eventually(lambda: fake.kinds(supervisor.HELLO))

    terminal.press(b"a\x03b")

    _eventually(lambda: fake.kinds(supervisor.SIGNAL))
    assert b"".join(fake.kinds(supervisor.INPUT)) == b"ab"
    assert json.loads(fake.kinds(supervisor.SIGNAL)[0]) == {"signal": "SIGINT"}
    terminal.press(b"\x1a")
    _finished(result)


def test_a_resize_is_passed_on(terminal, no_signal_handlers) -> None:
    client, fake = _pair()
    result = _relay_in_background(client)
    _eventually(lambda: fake.kinds(supervisor.HELLO))

    terminal.resize(20, 90)

    _eventually(lambda: fake.kinds(supervisor.RESIZE))
    assert json.loads(fake.kinds(supervisor.RESIZE)[0]) == {"rows": 20, "cols": 90}
    terminal.press(b"\x1a")
    _finished(result)


def test_output_is_shown_and_an_exit_ends_with_its_status(
    terminal, no_signal_handlers
) -> None:
    def script(connection, kind, payload) -> None:
        if kind == supervisor.HELLO:
            connection.sendall(supervisor.frame(supervisor.OUTPUT, b"from the app"))
            connection.sendall(
                supervisor.control(supervisor.EXIT, status=3, message="why")
            )

    client, _ = _pair(script)

    outcome = _finished(_relay_in_background(client))

    assert outcome == attach.Outcome(3, "why")
    assert b"from the app" in terminal.shown()


def test_a_refusal_is_reported(terminal, no_signal_handlers) -> None:
    def script(connection, kind, payload) -> None:
        connection.sendall(supervisor.control(supervisor.REFUSED, message="too old"))

    client, _ = _pair(script)

    assert _finished(_relay_in_background(client)) == attach.Outcome(1, "too old")


def test_losing_the_supervisor_counts_as_leaving(
    terminal, no_signal_handlers
) -> None:
    client, fake = _pair()
    result = _relay_in_background(client)
    _eventually(lambda: fake.kinds(supervisor.HELLO))

    fake.connection.shutdown(socket.SHUT_RDWR)

    assert _finished(result).status is None


def test_a_signal_to_the_client_is_passed_on(terminal) -> None:
    def script(connection, kind, payload) -> None:
        if kind == supervisor.HELLO:
            os.kill(os.getpid(), signal.SIGTERM)
        elif kind == supervisor.SIGNAL:
            connection.sendall(
                supervisor.control(supervisor.EXIT, status=143, message=None)
            )

    client, fake = _pair(script)

    # On the main thread, where a signal handler can be installed.
    outcome = attach._relay(client, bar=False)

    assert outcome.status == 143
    assert json.loads(fake.kinds(supervisor.SIGNAL)[0]) == {"signal": "SIGTERM"}


def test_connecting_reports_a_supervisor_that_failed_to_start(tmp_path) -> None:
    log = tmp_path / "demo.log"
    log.write_text("ImportError: nope")
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()

    with pytest.raises(click.ClickException, match="ImportError: nope"):
        attach._connect(str(tmp_path / "missing.sock"), process, log)


def test_connecting_waits_for_the_socket(tmp_path) -> None:
    path = tmp_path / "s.sock"
    listener = socket.socket(socket.AF_UNIX)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])

    def bind_later() -> None:
        time.sleep(0.1)
        listener.bind(str(path))
        listener.listen()

    threading.Thread(target=bind_later, daemon=True).start()
    try:
        attach._connect(str(path), process, tmp_path / "log").close()
    finally:
        process.kill()
        process.wait()
        listener.close()


def _plan(tmp_path: Path) -> runner.RunPlan:
    return runner.RunPlan("demo", "custom", ("serve",), 1, "p", "", tmp_path)


def test_running_supervised_starts_records_and_relays(
    terminal, tmp_path, monkeypatch
) -> None:
    started: dict = {}

    class Process:
        pid = os.getpid()
        stdin = None

        def __init__(self, command, **kwargs) -> None:
            started["command"] = command
            read, write = os.pipe()
            self.stdin = os.fdopen(write, "wb")
            started["read"] = read

        def poll(self):
            return None

    monkeypatch.setattr(attach.subprocess, "Popen", Process)
    monkeypatch.setattr(attach, "_connect", lambda path, process, log: "connection")

    def relay(connection, *, bar):
        started["spec"] = json.loads(os.read(started["read"], 65536))
        started["relayed"] = connection
        return attach.Outcome(None)

    monkeypatch.setattr(attach, "_relay", relay)
    proxy = []

    outcome = attach.run_supervised(
        _plan(tmp_path),
        lambda: proxy.append(True),
        cwd=tmp_path,
        log=tmp_path / "demo.log",
        public_origin="http://demo.localhost",
        secondary_origin=None,
        status_bar=False,
    )

    assert outcome.status is None and proxy == [True]
    assert started["command"][-1] == "localghost.supervisor"
    assert started["relayed"] == "connection"
    record = sessions.sessions()[0]
    assert (record.pid, record.detached) == (os.getpid(), False)
    assert record.socket and started["spec"]["session"] == record.id
    assert started["spec"]["socket"] == record.socket
    assert (started["spec"]["rows"], started["spec"]["cols"]) == (30, 120)


def test_interrupting_the_hub_starts_nothing(
    terminal, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        attach.subprocess, "Popen", lambda *a, **k: pytest.fail("started")
    )

    def interrupted() -> None:
        raise runner._TerminationSignal(signal.SIGINT)

    outcome = attach.run_supervised(
        _plan(tmp_path),
        interrupted,
        cwd=tmp_path,
        log=tmp_path / "demo.log",
        public_origin="http://demo.localhost",
        secondary_origin=None,
        status_bar=False,
    )

    assert outcome.status == 130
    assert sessions.sessions() == []


def test_probes_ask_an_isolated_run_for_an_http_answer(monkeypatch) -> None:
    monkeypatch.setattr(statusbar, "http_probe", lambda url: ("http", url))
    monkeypatch.setattr(statusbar, "tcp_probe", lambda port: ("tcp", port))

    assert attach._readiness_probe(80, True) == ("http", "http://127.0.0.1:80/")
    assert attach._readiness_probe(80, False) == ("tcp", 80)


def _session(tmp_path: Path, **overrides) -> sessions.Session:
    values = {
        "mode": "host", "name": "demo", "port": 1, "cwd": tmp_path,
        "command": (), "log": tmp_path / "demo.log", "pid": os.getpid(),
    }  # fmt: skip
    values.update(overrides)
    return sessions.create(**values)


def test_attaching_to_a_supervised_run_relays(
    terminal, tmp_path, monkeypatch
) -> None:
    path = tmp_path / "s.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen()
    monkeypatch.setattr(
        attach, "_relay", lambda connection, *, bar: attach.Outcome(7)
    )

    outcome = attach.attach(
        _session(tmp_path, socket=str(path)),
        public_origin="http://demo.localhost",
        secondary_origin=None,
    )

    listener.close()
    assert outcome.status == 7


def test_attaching_to_a_dead_socket_says_so(terminal, tmp_path) -> None:
    with pytest.raises(click.ClickException, match="could not reach demo"):
        attach.attach(
            _session(tmp_path, socket=str(tmp_path / "gone.sock")),
            public_origin="http://demo.localhost",
            secondary_origin=None,
        )


def test_following_a_detached_log_shows_it_and_ends_with_the_application(
    terminal, tmp_path, monkeypatch
) -> None:
    (tmp_path / "demo.log").write_bytes(b"old line\n")
    monkeypatch.setattr(attach, "alive", lambda session: False)

    outcome = attach.attach(
        _session(tmp_path),
        public_origin="http://demo.localhost",
        secondary_origin=None,
    )

    assert outcome.status == 0
    assert b"old line" in terminal.shown()


def test_ctrl_z_leaves_a_detached_log_with_the_application_running(
    terminal, tmp_path, monkeypatch
) -> None:
    (tmp_path / "demo.log").write_bytes(b"")
    # This is not the test's controlling terminal, so it would not be offered.
    monkeypatch.setattr(runner, "detach_key_available", lambda: True)
    # Once the terminal is claimed: before that the key would be a signal.
    threading.Timer(0.3, terminal.press, [b"\x1a"]).start()

    outcome = attach._follow_log(_session(tmp_path))

    assert outcome.status is None


def test_ctrl_c_stops_a_detached_application(
    terminal, tmp_path, monkeypatch
) -> None:
    (tmp_path / "demo.log").write_bytes(b"")
    stopped = []
    monkeypatch.setattr(attach, "stop_session", stopped.append)

    def interrupted(key):
        raise runner._TerminationSignal(signal.SIGINT)

    monkeypatch.setattr(attach, "_detach_key_pressed", interrupted)

    outcome = attach._follow_log(_session(tmp_path))

    assert outcome.status == 130
    assert [item.name for item in stopped] == ["demo"]


def test_a_terminal_that_reports_no_size_falls_back(terminal, monkeypatch) -> None:
    terminal.resize(0, 0)
    monkeypatch.setattr(statusbar, "_terminal_size", lambda: (100, 40))

    assert attach._terminal_rows_cols(bar=True) == (39, 100)


def test_a_closed_terminal_counts_as_leaving(terminal, no_signal_handlers) -> None:
    client, fake = _pair()
    reads = []
    real_read = os.read

    def read(descriptor, size):
        if descriptor == terminal.stdin.fileno():
            reads.append(descriptor)
            return b""
        return real_read(descriptor, size)

    terminal.press(b"x")
    result = None
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(attach.os, "read", read)
        result = attach._relay(client, bar=False)

    assert result.status is None and reads


def test_a_failed_write_counts_as_leaving(terminal, no_signal_handlers) -> None:
    client, fake = _pair()
    fake.connection.close()
    client.close()

    assert attach._relay(client, bar=False).status is None
