"""The supervisor: a real pseudo-terminal, socket, and application, no Docker."""

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from localghost import attach, registry, runner, sessions, supervisor
from localghost.cli import cli

# Reports what it can see of its terminal, echoes a line, and reports signals.
_APP = r"""
import os, signal, sys, time, fcntl, termios, struct
def report(signum, frame):
    print("SIGNAL", signal.Signals(signum).name, flush=True)
    if signum != signal.SIGWINCH:
        sys.exit(128 + signum)
for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH):
    signal.signal(signum, report)
size = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
os.close(os.open("/dev/tty", os.O_RDWR))
print("TTY", sys.stdout.isatty(), "SIZE", size[0], size[1], "CTTY ok", flush=True)
print("GOT", sys.stdin.readline().strip(), flush=True)
time.sleep(30)
"""


def _plan(tmp_path: Path, command=None) -> runner.RunPlan:
    return runner.RunPlan(
        "demo", "custom", command or (sys.executable, "-c", _APP), 1, "p", "",
        tmp_path,
    )  # fmt: skip


@pytest.fixture
def supervised(tmp_path, monkeypatch):
    """Fork a supervisor for a plan, with Docker and the port check stubbed."""
    children = []

    def start(plan=None, *, bridge_fails: bool = False) -> tuple[str, sessions.Session]:
        plan = plan or _plan(tmp_path)
        path = str(tmp_path / f"{len(children)}.sock")
        log = tmp_path / "demo.log"
        session = sessions.create(
            mode="host", name="demo", port=1, cwd=tmp_path, command=plan.command,
            log=log, pid=None, detached=False, socket=path,
        )  # fmt: skip
        spec = supervisor.spec_for(
            plan, session_id=session.id, cwd=tmp_path, socket_path=path, log=log,
            rows=24, cols=80,
        )  # fmt: skip
        spec = json.loads(json.dumps(spec))
        pid = os.fork()
        if pid == 0:
            status = 1
            try:
                os.setsid()
                monkeypatch.setattr(runner, "start_bridge", lambda plan: None)
                monkeypatch.setattr(runner, "stop_bridge", lambda plan: None)
                monkeypatch.setattr(runner.ports, "port_available", lambda port: True)
                monkeypatch.setattr(runner.ports, "loopback_only", lambda *a: None)
                monkeypatch.setattr(runner.ports, "listeners", lambda *a: [])
                status = supervisor.serve(spec)
            finally:
                os._exit(status)
        children.append(pid)
        session.pid = pid
        sessions.save(session)
        return path, session

    yield start
    for pid in children:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        os.waitpid(pid, 0)


class _Connection:
    def __init__(self, path: str, rows: int = 24, cols: int = 80) -> None:
        deadline = time.monotonic() + 10
        self.socket = socket.socket(socket.AF_UNIX)
        while True:
            try:
                self.socket.connect(path)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        self.reader = supervisor.FrameReader()
        self.output = b""
        self.exit = None
        self.refused = None
        self.send_hello(rows, cols)

    def send_hello(self, rows, cols, version=supervisor.PROTOCOL_VERSION) -> None:
        self.socket.sendall(
            supervisor.control(supervisor.HELLO, version=version, rows=rows, cols=cols)
        )

    def wait_for(self, wanted, timeout: float = 10) -> bytes:
        self.socket.settimeout(0.1)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if wanted(self):
                return self.output
            try:
                data = self.socket.recv(65536)
            except TimeoutError:
                continue
            if not data:
                break
            for kind, payload in self.reader.feed(data):
                if kind == supervisor.OUTPUT:
                    self.output += payload
                elif kind == supervisor.EXIT:
                    self.exit = json.loads(payload)
                elif kind == supervisor.REFUSED:
                    self.refused = json.loads(payload)
        assert wanted(self), self.output
        return self.output

    def wait_for_output(self, text: bytes) -> bytes:
        return self.wait_for(lambda c: text in c.output)

    def wait_for_exit(self) -> dict:
        self.wait_for(lambda c: c.exit is not None)
        return self.exit

    def send_signal(self, name: str) -> None:
        self.socket.sendall(supervisor.control(supervisor.SIGNAL, signal=name))

    def close(self) -> None:
        self.socket.close()


def _eventually(check, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    assert check()


def test_the_application_gets_a_real_controlling_terminal(supervised) -> None:
    path, _ = supervised()
    client = _Connection(path, rows=29, cols=100)

    # Sized before the application starts, or by hello as it does.
    client.wait_for_output(b"CTTY ok")
    assert b"TTY True" in client.output


def test_input_reaches_the_application(supervised) -> None:
    path, _ = supervised()
    client = _Connection(path)
    client.wait_for_output(b"CTTY ok")

    client.socket.sendall(supervisor.frame(supervisor.INPUT, b"hello\n"))

    client.wait_for_output(b"GOT hello")


def test_resizing_reaches_the_application(supervised) -> None:
    path, _ = supervised()
    client = _Connection(path)
    client.wait_for_output(b"CTTY ok")

    client.socket.sendall(supervisor.control(supervisor.RESIZE, rows=10, cols=50))

    client.wait_for_output(b"SIGNAL SIGWINCH")


def test_a_stop_signals_the_application_and_reports_its_status(supervised) -> None:
    path, session = supervised()
    client = _Connection(path)
    client.wait_for_output(b"CTTY ok")

    client.send_signal("SIGINT")

    assert client.wait_for_exit()["status"] == 130
    assert b"SIGNAL SIGINT" in client.output
    _eventually(lambda: sessions._read(session.id) is None)


def test_leaving_keeps_the_application_and_marks_it_detached(
    supervised, tmp_path
) -> None:
    registry.record("demo", tmp_path, "custom", detached=False)
    path, session = supervised()
    first = _Connection(path)
    first.wait_for_output(b"CTTY ok")

    first.close()

    _eventually(lambda: sessions._read(session.id).detached)
    assert registry.entries()[0].detached is True
    assert sessions.alive(session)

    second = _Connection(path)
    # What it missed is replayed, and input still works.
    second.wait_for_output(b"CTTY ok")
    _eventually(lambda: not sessions._read(session.id).detached)
    assert registry.entries()[0].detached is False
    second.socket.sendall(supervisor.frame(supervisor.INPUT, b"back\n"))
    second.wait_for_output(b"GOT back")


def test_output_goes_to_the_log_with_or_without_a_terminal(
    supervised, tmp_path
) -> None:
    path, session = supervised()
    client = _Connection(path)
    client.wait_for_output(b"CTTY ok")
    client.close()

    _eventually(lambda: b"CTTY ok" in (tmp_path / "demo.log").read_bytes())


def test_two_terminals_both_see_output_and_the_end(supervised) -> None:
    path, _ = supervised()
    first = _Connection(path)
    second = _Connection(path)
    first.wait_for_output(b"CTTY ok")
    second.wait_for_output(b"CTTY ok")

    second.send_signal("SIGTERM")

    assert first.wait_for_exit()["status"] == 128 + signal.SIGTERM
    assert second.wait_for_exit()["status"] == 128 + signal.SIGTERM


def test_a_different_protocol_version_is_refused(supervised) -> None:
    path, _ = supervised()
    good = _Connection(path)
    good.wait_for_output(b"CTTY ok")
    other = socket.socket(socket.AF_UNIX)
    other.connect(path)
    other.sendall(
        supervisor.control(supervisor.HELLO, version=0, rows=24, cols=80)
    )
    other.settimeout(5)

    kind, payload = supervisor.FrameReader().feed(other.recv(65536))[0]

    assert kind == supervisor.REFUSED
    assert "different version" in json.loads(payload)["message"]


def test_an_application_ending_unattended_leaves_its_record_and_log(
    supervised, tmp_path
) -> None:
    plan = _plan(
        tmp_path, (sys.executable, "-c", "import time; time.sleep(0.5); print('bye')")
    )
    path, session = supervised(plan)
    _Connection(path).close()

    def ended() -> bool:
        record = sessions._read(session.id)
        return record is not None and record.pid is None

    _eventually(ended)
    assert not sessions.alive(sessions._read(session.id))
    assert b"bye" in (tmp_path / "demo.log").read_bytes()


def test_a_start_failure_reaches_the_terminal_as_a_message(
    supervised, tmp_path
) -> None:
    path, _ = supervised(_plan(tmp_path, ("/no/such/program",)))
    client = _Connection(path)

    ended = client.wait_for_exit()

    assert ended["status"] == 1
    assert "could not start application command" in ended["message"]


def test_stop_ends_a_supervised_run_through_its_process(supervised) -> None:
    path, session = supervised()
    client = _Connection(path)
    client.wait_for_output(b"CTTY ok")
    client.close()
    _eventually(lambda: sessions._read(session.id).detached)

    stopping = sessions._read(session.id)
    pid = stopping.pid
    deadline = time.monotonic() + 15
    os.kill(pid, signal.SIGTERM)
    while time.monotonic() < deadline:
        if os.waitpid(pid, os.WNOHANG)[0]:
            break
        time.sleep(0.02)
    else:
        pytest.fail("the supervisor did not exit on SIGTERM")


def test_stop_signals_a_supervised_pid_not_its_group(tmp_path, monkeypatch) -> None:
    session = sessions.create(
        mode="host", name="demo", port=1, cwd=tmp_path, command=(), log=None,
        pid=4321, detached=True, socket="/nowhere",
    )  # fmt: skip
    killed = []
    monkeypatch.setattr(sessions.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(sessions.os, "killpg", lambda pid, sig: pytest.fail("group"))
    monkeypatch.setattr(sessions, "alive", lambda item: not killed)

    sessions.stop(session)

    assert killed == [4321]


def test_marking_a_removed_record_does_not_bring_it_back(tmp_path) -> None:
    session = sessions.create(
        mode="host", name="demo", port=1, cwd=tmp_path, command=(), log=None, pid=1
    )
    sessions.discard_by_id(session.id)

    sessions.mark_detached(session.id, True)

    assert sessions.sessions() == []


def test_cleaning_a_dead_supervisors_record_removes_its_socket(tmp_path) -> None:
    left_behind = tmp_path / "left.sock"
    left_behind.touch()
    sessions.create(
        mode="host", name="demo", port=1, cwd=tmp_path, command=(), log=None,
        pid=None, socket=str(left_behind),
    )  # fmt: skip

    assert sessions.clean("demo") == 1

    assert not left_behind.exists()


def test_rotating_keeps_the_previous_log(tmp_path) -> None:
    log = tmp_path / "demo.log"
    log.write_text("crash")

    supervisor.rotate_log(log)

    assert not log.exists()
    assert (tmp_path / "demo.log.1").read_text() == "crash"


def test_frames_survive_arbitrary_chunking() -> None:
    data = supervisor.frame(supervisor.OUTPUT, b"abc") + supervisor.frame(
        supervisor.INPUT, b"de"
    )
    reader = supervisor.FrameReader()

    frames = [item for byte in data for item in reader.feed(bytes([byte]))]

    assert frames == [(supervisor.OUTPUT, b"abc"), (supervisor.INPUT, b"de")]


def test_an_oversized_frame_is_rejected() -> None:
    reader = supervisor.FrameReader()

    with pytest.raises(ValueError):
        reader.feed(supervisor._HEADER.pack(b"o", 1 << 30))


def test_a_plan_survives_the_trip_to_the_supervisor(tmp_path) -> None:
    plan = _plan(tmp_path, ("a", "b"))
    spec = supervisor.spec_for(
        plan, session_id="x", cwd=tmp_path, socket_path="s", log=tmp_path / "l",
        rows=1, cols=1,
    )  # fmt: skip

    assert supervisor.plan_from(json.loads(json.dumps(spec))["plan"]) == plan


def test_runs_without_a_terminal_stay_in_process() -> None:
    assert attach.supervised_run_available() is False


def test_the_legacy_setting_keeps_runs_in_process(monkeypatch) -> None:
    monkeypatch.setattr(attach, "detach_key_available", lambda: True)
    assert attach.supervised_run_available() is True

    monkeypatch.setenv("LOCALGHOST_LEGACY_RUN", "1")

    assert attach.supervised_run_available() is False


def test_attach_refuses_a_compose_project(tmp_path, monkeypatch) -> None:
    sessions.create(
        mode="compose", name="demo", port=0, cwd=tmp_path, command=(), log=None,
        pid=None, project="demo",
    )  # fmt: skip
    monkeypatch.setattr("localghost.cli.session_alive", lambda item: True)
    monkeypatch.setattr(sessions, "alive", lambda item: True)

    result = CliRunner().invoke(cli, ["sessions", "attach", "demo"])

    assert result.exit_code != 0
    assert "Compose project" in result.output


def test_attach_refuses_without_a_terminal(tmp_path, monkeypatch) -> None:
    sessions.create(
        mode="host", name="demo", port=1, cwd=tmp_path, command=(), log=None,
        pid=os.getpid(),
    )  # fmt: skip

    result = CliRunner().invoke(cli, ["sessions", "attach", "demo"])

    assert result.exit_code != 0
    assert "needs a terminal" in result.output


def test_run_on_a_fixed_port_held_by_its_own_session_is_not_a_conflict(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / ".localghost.toml").write_text(
        '[run]\nname = "demo"\nport = 4567\ncommand = ["serve"]\n'
    )
    sessions.create(
        mode="host", name="demo", port=4567, cwd=tmp_path, command=("serve",),
        log=None, pid=os.getpid(),
    )  # fmt: skip
    monkeypatch.setattr(runner.ports, "port_available", lambda port: False)

    result = CliRunner().invoke(cli, ["run", "-C", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "already running" in result.output


def test_a_port_held_by_something_else_is_still_a_conflict(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / ".localghost.toml").write_text(
        '[run]\nname = "demo"\nport = 4567\ncommand = ["serve"]\n'
    )
    monkeypatch.setattr(runner.ports, "port_available", lambda port: False)

    result = CliRunner().invoke(cli, ["run", "-C", str(tmp_path)])

    assert result.exit_code != 0
    assert "already running" not in result.output


def test_logs_keep_colour_but_not_cursor_movement() -> None:
    from localghost.cli import _readable_log

    raw = "\x1b[2J\x1b[H\x1b[32mgreen\x1b[0m\r\n\x1b[?25lnext\x1b]0;title\x07\n"

    assert _readable_log(raw) == "\x1b[32mgreen\x1b[0m\nnext\n"
