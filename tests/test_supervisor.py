"""The supervisor: a real pseudo-terminal, socket, and application, no Docker."""

import io
import json
import os
import select
import signal
import socket
import sys
import time
from pathlib import Path

import coverage
import pytest
from click.testing import CliRunner

from localghost import attach, registry, runner, sessions, supervisor
from localghost import cli as cli_module
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


def _save_coverage() -> None:
    """`os._exit` skips what would save a forked child's coverage."""
    measuring = coverage.Coverage.current()
    if measuring is not None:
        measuring.stop()
        measuring.save()


def _stop_child(pid: int) -> None:
    """End it the way a run is really stopped, so it gets to save its coverage."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0]:
            return
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
            time.sleep(0.01)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ChildProcessError, ProcessLookupError):
        return


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
                _save_coverage()
                os._exit(status)
        children.append(pid)
        session.pid = pid
        sessions.save(session)
        return path, session

    yield start
    for pid in children:
        _stop_child(pid)


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


def test_detaching_offers_plain_run_where_it_would_attach(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    registry.record("demo", tmp_path, "html", detached=True)

    cli_module._report_detached("demo", attachable=True)

    output = capsys.readouterr().out
    assert "Bring it back: localghost run, or from anywhere" in output
    assert "sessions attach demo" in output


def test_detaching_offers_only_attach_after_explicit_flags(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    registry.record(
        "demo", tmp_path, "custom", detached=True,
        flags=registry.RunFlags(port=4567, command=("serve",)),
    )  # fmt: skip

    cli_module._report_detached("demo", attachable=True)

    output = capsys.readouterr().out
    assert "Bring it back: localghost sessions attach demo" in output


def test_detaching_offers_only_attach_from_another_directory(
    tmp_path, monkeypatch, capsys
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    monkeypatch.chdir(tmp_path)
    registry.record("demo", project, "html", detached=True)

    cli_module._report_detached("demo", attachable=True)

    output = capsys.readouterr().out
    assert "Bring it back: localghost sessions attach demo" in output


def test_plain_run_attaches_to_a_detected_project_it_detached_from(
    tmp_path, monkeypatch
) -> None:
    project = tmp_path / "blog"
    project.mkdir()
    (project / "index.html").write_text("hi")
    monkeypatch.chdir(project)
    session = sessions.create(
        mode="host", name="blog", port=4567, cwd=project, command=("serve",),
        log=None, pid=os.getpid(), detached=True, socket="/nowhere",
    )  # fmt: skip
    monkeypatch.setattr(cli_module, "supervised_run_available", lambda: True)
    attached = []
    monkeypatch.setattr(cli_module, "_attach_session", attached.append)

    result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    assert [item.id for item in attached] == [session.id]


def test_logs_keep_colour_but_not_cursor_movement() -> None:
    from localghost.cli import _readable_log

    raw = "\x1b[2J\x1b[H\x1b[32mgreen\x1b[0m\r\n\x1b[?25lnext\x1b]0;title\x07\n"

    assert _readable_log(raw) == "\x1b[32mgreen\x1b[0m\nnext\n"


@pytest.fixture
def server(tmp_path):
    """A server that nothing is driving, so each path can be taken directly."""
    master, terminal = os.openpty()
    listener = socket.socket(socket.AF_UNIX)
    built = supervisor._Server(master, listener, tmp_path / "demo.log", "id", "demo")
    yield built
    listener.close()
    os.close(master)
    os.close(terminal)


def _client_of(server) -> tuple[socket.socket, supervisor._Client]:
    theirs, ours = socket.socketpair()
    ours.setblocking(False)
    client = supervisor._Client(ours)
    server._clients[ours] = client
    return theirs, client


def test_the_log_rotates_at_its_limit(server, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(supervisor, "_LOG_LIMIT", 4)

    server._write_log(b"first")
    server._write_log(b"x")

    assert (tmp_path / "demo.log.1").read_bytes() == b"first"
    assert (tmp_path / "demo.log").read_bytes() == b"x"


def test_a_client_too_far_behind_is_dropped_not_waited_for(
    server, monkeypatch
) -> None:
    monkeypatch.setattr(supervisor, "_CLIENT_BACKLOG", 8)
    theirs, client = _client_of(server)
    # Nothing reads it, and it can't take any more.
    client.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1)
    while True:
        try:
            client.connection.send(b"x" * 65536)
        except BlockingIOError:
            break

    server._send(client, b"y" * 64)

    assert client.connection not in server._clients
    theirs.close()


def test_a_full_socket_keeps_output_for_later(server) -> None:
    theirs, client = _client_of(server)
    while True:
        try:
            client.connection.send(b"x" * 65536)
        except BlockingIOError:
            break

    server._send(client, b"later")

    assert bytes(client.backlog) == b"later"
    theirs.close()


def test_a_client_sending_nonsense_is_dropped(server) -> None:
    theirs, client = _client_of(server)
    theirs.sendall(supervisor._HEADER.pack(b"o", 1 << 30))
    _eventually(lambda: select.select([client.connection], [], [], 0)[0])

    server._read_client(client.connection)

    assert client.connection not in server._clients
    theirs.close()


def test_the_last_client_leaving_marks_the_run_detached(server, tmp_path) -> None:
    session = sessions.create(
        mode="host", name="demo", port=1, cwd=tmp_path, command=(), log=None,
        pid=1, detached=False,
    )  # fmt: skip
    server._session_id = session.id
    first, one = _client_of(server)
    second, other = _client_of(server)

    server._drop(one)
    assert sessions._read(session.id).detached is False
    server._drop(other)

    assert sessions._read(session.id).detached is True
    first.close()
    second.close()


def test_leaving_as_the_run_ends_is_not_detaching(server, tmp_path) -> None:
    session = sessions.create(
        mode="host", name="demo", port=1, cwd=tmp_path, command=(), log=None,
        pid=1, detached=False,
    )  # fmt: skip
    server._session_id = session.id
    theirs, client = _client_of(server)
    server.finish(0, None)

    server._drop(client)

    assert sessions._read(session.id).detached is False
    theirs.close()


def test_replay_starts_at_a_clean_line_once_it_has_been_cut(server) -> None:
    server._replay = bytearray(
        b"partial\x1b[3" + b"\n" + b"x" * supervisor._REPLAY_BYTES
    )

    assert server._replayable().startswith(b"xxx")


def test_the_replay_is_whole_until_then(server) -> None:
    server._replay = bytearray(b"line one\nline two")

    assert server._replayable() == b"line one\nline two"


def test_saying_goodbye_flushes_what_a_slow_client_is_owed(server) -> None:
    theirs, client = _client_of(server)
    client.backlog += b"owed"
    server.finish(5, "why")

    server._say_goodbye()

    assert server.attached_at_exit is True
    received = theirs.recv(65536)
    assert received.startswith(b"owed")
    frames = supervisor.FrameReader().feed(received[len(b"owed") :])
    assert frames[-1][0] == supervisor.EXIT
    assert json.loads(frames[-1][1]) == {"status": 5, "message": "why"}
    theirs.close()


def test_main_reads_its_spec_from_stdin(monkeypatch) -> None:
    served = []
    monkeypatch.setattr(supervisor, "serve", lambda spec: served.append(spec) or 4)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"plan": {}}'))

    assert supervisor.main() == 4
    assert served == [{"plan": {}}]


def test_an_unreadable_record_is_left_alone(tmp_path) -> None:
    sessions.state_dir().mkdir(parents=True, exist_ok=True)
    (sessions.state_dir() / "bad.json").write_text("not json")

    sessions.mark_detached("bad", True)
    sessions.mark_ended("bad")

    assert (sessions.state_dir() / "bad.json").read_text() == "not json"


def test_a_socket_prefers_the_runtime_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LOCALGHOST_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    path = Path(sessions.socket_path_for("demo"))

    assert path.parent == tmp_path / "localghost"
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_an_explicit_state_directory_keeps_the_socket_with_it(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    assert Path(sessions.socket_path_for("demo")).parent == tmp_path / "sessions"
