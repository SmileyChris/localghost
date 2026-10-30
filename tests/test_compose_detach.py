"""Detaching from a foreground `docker compose up`."""

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from localghost import cli as cli_module
from localghost import registry, runner, statusbar
from localghost.cli import cli
from localghost.sessions import sessions

# Stands in for the compose client: reports the signal it got, then exits with
# the status given for it. Ready once the file exists.
_CLIENT = """
import pathlib, signal, sys, time
out = pathlib.Path(sys.argv[1])
def handler(signum, frame):
    out.write_text(signal.Signals(signum).name)
    sys.exit(128 + signum)
for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(signum, handler)
out.write_text("ready")
time.sleep(30)
"""


@pytest.fixture
def client(tmp_path: Path) -> tuple[list[str], Path]:
    out = tmp_path / "client"
    return [sys.executable, "-c", _CLIENT, str(out)], out


def _when_ready(out: Path, action) -> None:
    def run() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if out.exists() and out.read_text() == "ready":
                action()
                return
            time.sleep(0.01)

    threading.Thread(target=run, daemon=True).start()


def _use_key_pipe(monkeypatch) -> int:
    """Have the wait watch a pipe in place of the terminal; returns its write end."""
    read, write = os.pipe()
    monkeypatch.setattr(runner, "detach_key_available", lambda: True)
    monkeypatch.setattr(runner, "_claim_detach_key", lambda: _yielding(read))
    return write


def _yielding(value):
    @contextlib.contextmanager
    def manager():
        yield value

    return manager()


def test_ctrl_z_hangs_up_the_client_and_reports_a_detach(
    client, tmp_path, monkeypatch
) -> None:
    command, out = client
    write = _use_key_pipe(monkeypatch)
    _when_ready(out, lambda: os.write(write, b"\x1a"))

    assert runner.attached_compose_up(command, cwd=tmp_path) is None
    assert out.read_text() == "SIGHUP"


def test_other_keys_do_not_detach(client, tmp_path, monkeypatch) -> None:
    command, out = client
    write = _use_key_pipe(monkeypatch)

    def press_then_stop() -> None:
        os.write(write, b"\nabc")
        time.sleep(0.3)
        assert out.read_text() == "ready"
        os.kill(os.getpid(), signal.SIGINT)

    _when_ready(out, press_then_stop)

    assert runner.attached_compose_up(command, cwd=tmp_path) == 130


def test_ctrl_c_reaches_the_clients_whole_group(client, tmp_path) -> None:
    command, out = client
    _when_ready(out, lambda: os.kill(os.getpid(), signal.SIGINT))

    assert runner.attached_compose_up(command, cwd=tmp_path) == 130
    assert out.read_text() == "SIGINT"


def test_sigterm_stops_rather_than_detaches(client, tmp_path) -> None:
    command, out = client
    _when_ready(out, lambda: os.kill(os.getpid(), signal.SIGTERM))

    assert runner.attached_compose_up(command, cwd=tmp_path) == 128 + signal.SIGTERM
    assert out.read_text() == "SIGTERM"


def test_hangup_from_a_terminal_detaches(client, tmp_path, monkeypatch) -> None:
    command, out = client
    _use_key_pipe(monkeypatch)
    _when_ready(out, lambda: os.kill(os.getpid(), signal.SIGHUP))

    assert runner.attached_compose_up(command, cwd=tmp_path) is None
    assert out.read_text() == "SIGHUP"


def test_a_second_hangup_cannot_end_the_handover(
    client, tmp_path, monkeypatch
) -> None:
    command, out = client
    _use_key_pipe(monkeypatch)
    previous = signal.getsignal(signal.SIGHUP)
    _when_ready(out, lambda: os.kill(os.getpid(), signal.SIGHUP))
    try:
        assert runner.attached_compose_up(command, cwd=tmp_path) is None
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_hangup_without_a_terminal_stops(client, tmp_path) -> None:
    command, out = client
    _when_ready(out, lambda: os.kill(os.getpid(), signal.SIGHUP))

    assert runner.attached_compose_up(command, cwd=tmp_path) == 128 + signal.SIGHUP
    assert out.read_text() == "SIGINT"


def test_the_clients_own_exit_status_is_returned(tmp_path) -> None:
    command = [sys.executable, "-c", "raise SystemExit(3)"]

    assert runner.attached_compose_up(command, cwd=tmp_path) == 3


def test_handlers_are_restored_afterwards(tmp_path) -> None:
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGHUP)}

    runner.attached_compose_up([sys.executable, "-c", ""], cwd=tmp_path)

    assert {s: signal.getsignal(s) for s in before} == before


def test_the_compose_menu_is_off_and_the_client_has_its_own_session(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    real = subprocess.Popen

    def spy(command, **kwargs):
        captured.update(kwargs)
        return real(command, **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", spy)

    runner.attached_compose_up([sys.executable, "-c", ""], cwd=tmp_path)

    assert captured["env"]["COMPOSE_MENU"] == "0"
    assert captured["start_new_session"] is True


def test_the_key_is_only_offered_on_a_terminal() -> None:
    # Under pytest stdin is not a terminal.
    assert runner.detach_key_available() is False


@pytest.fixture
def compose_project(monkeypatch, tmp_path: Path) -> list:
    """`_run_compose` as far as the client, recording every docker call."""
    calls: list = []
    monkeypatch.setattr(cli_module, "_run_proxy", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "_https_configured", lambda: False)
    monkeypatch.setattr(cli_module, "_tailnet_origin", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "_report_tailnet_origin", lambda *a, **k: None)

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(cli_module.subprocess, "run", run)
    return calls


def test_detaching_records_a_detached_session(
    compose_project, monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(
        cli_module, "attached_compose_up", lambda command, *, cwd: None
    )
    registry.record(
        "demo",
        tmp_path,
        "compose",
        detached=False,
        flags=registry.RunFlags(type="compose"),
    )

    cli_module._run_compose(tmp_path, "demo", False)

    recorded = sessions()
    assert [(s.name, s.detached, s.pid) for s in recorded] == [("demo", True, None)]
    # The stack is brought to rest the way a plain detached run leaves it.
    assert compose_project == [
        ["docker", "compose", "--project-name", "demo", "up", "--detach"]
    ]
    entry = registry.entries()[0]
    assert entry.detached is True
    assert entry.flags == registry.RunFlags(type="compose")
    output = capsys.readouterr().out
    assert "Detached from demo" in output
    assert "sessions stop demo" in output


def test_a_failed_converge_is_reported_but_the_session_stays(
    compose_project, monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(
        cli_module, "attached_compose_up", lambda command, *, cwd: None
    )
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, b"", b"no such image"
        ),
    )

    cli_module._run_compose(tmp_path, "demo", False)

    assert [s.detached for s in sessions()] == [True]
    captured = capsys.readouterr()
    assert "no such image" in captured.out + captured.err


def test_a_closed_terminal_still_leaves_a_detached_session(
    compose_project, monkeypatch, tmp_path
) -> None:
    class DeadBar:
        def status(self, message: str) -> None:
            if message == "detaching":
                raise OSError(5, "Input/output error")

    @contextlib.contextmanager
    def pinned(*args, **kwargs):
        yield DeadBar()

    monkeypatch.setattr(cli_module.statusbar, "pinned", pinned)
    monkeypatch.setattr(
        cli_module, "attached_compose_up", lambda command, *, cwd: None
    )

    cli_module._run_compose(tmp_path, "demo", False)

    assert [s.detached for s in sessions()] == [True]


def test_a_stopped_foreground_run_leaves_no_session(
    compose_project, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        cli_module, "attached_compose_up", lambda command, *, cwd: 130
    )

    with pytest.raises(cli_module.click.exceptions.Exit) as stopped:
        cli_module._run_compose(tmp_path, "demo", False)

    assert stopped.value.exit_code == 130
    assert sessions() == []
    assert compose_project == []


def test_registry_update_changes_only_the_named_field(tmp_path) -> None:
    flags = registry.RunFlags(port=4567)
    registry.record("blog", tmp_path, "html", detached=False, flags=flags)
    started = registry.entries()[0].last_started

    registry.update("blog", detached=True)

    entry = registry.entries()[0]
    assert (entry.detached, entry.flags, entry.last_started) == (True, flags, started)


def test_registry_update_ignores_an_unknown_project() -> None:
    registry.update("missing", detached=True)

    assert registry.entries() == []


def test_bar_offers_detaching_only_when_asked() -> None:
    plain = statusbar._bar_line("https://a.localhost", 100)
    offered = statusbar._bar_line("https://a.localhost", 100, detachable=True)

    assert "Ctrl+Z" not in plain
    assert "Ctrl+C to stop" in offered and "Ctrl+Z to detach" in offered


def test_bar_drops_the_detach_hint_before_the_stop_hint() -> None:
    url = "https://a.localhost"
    narrow = 13 + len(url) + len("Ctrl+C to stop") + 6

    line = statusbar._bar_line(url, narrow, detachable=True)

    assert "Ctrl+C to stop" in line and "Ctrl+Z" not in line


def test_cli_run_stops_cleanly_on_ctrl_c(monkeypatch, tmp_path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    monkeypatch.setattr(cli_module, "_check_compose_routing", lambda *a: None)
    monkeypatch.setattr(cli_module, "_run_proxy", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_module, "attached_compose_up", lambda command, *, cwd: 130
    )

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--type", "compose", "--name", "demo"]
    )

    assert result.exit_code == 130
    assert "Stopped demo." in result.output
