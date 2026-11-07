"""The `forward` command: checks, start-up and how it ends in the terminal."""

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from localghost import attach, forwards, registry, sessions
from localghost import cli as cli_module
from localghost.cli import cli


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(directory))
    return directory


@pytest.fixture(autouse=True)
def no_docker(monkeypatch) -> list:
    """Nothing here may reach Docker; record whether the hub was started."""
    hub: list = []
    monkeypatch.setattr(
        cli_module, "_run_proxy", lambda *args, **kwargs: hub.append(args)
    )
    monkeypatch.setattr(cli_module, "find_route_collision", lambda name: None)
    monkeypatch.setattr(forwards, "_docker_publisher", lambda port: None)
    monkeypatch.setattr(cli_module, "_proxy_origin", lambda name: f"http://{name}.localhost")
    monkeypatch.setattr(cli_module, "_tailnet_origin", lambda name, **kwargs: None)
    return hub


@pytest.fixture
def started(monkeypatch, tmp_path) -> list:
    """Fake the leader: write the record a real `_start_forward` would."""
    calls: list = []

    def start(name, port, *, detach):
        calls.append((name, port, detach))
        return sessions.create(
            mode="forward",
            name=name,
            port=port,
            cwd=None,
            command=(),
            log=tmp_path / f"{name}.log",
            pid=os.getpid(),
        )

    monkeypatch.setattr(cli_module, "_start_forward", start)
    return calls


def _forward(tmp_path: Path, name: str = "api", port: int = 5173, pid=None):
    return sessions.create(
        mode="forward",
        name=name,
        port=port,
        cwd=None,
        command=(),
        log=tmp_path / f"{name}.log",
        pid=os.getpid() if pid is None else pid,
    )


def test_forward_requires_a_port_for_a_new_name(started) -> None:
    result = CliRunner().invoke(cli, ["forward", "api"])

    assert result.exit_code != 0
    assert "forward needs a port:" in result.output
    assert "forward api PORT" in result.output
    assert started == []


def test_forward_reuses_a_remembered_forwards_port(started, no_docker) -> None:
    registry.record_forward("api", 5173, detached=True)

    result = CliRunner().invoke(cli, ["forward", "api"])

    assert result.exit_code == 0, result.output
    assert started == [("api", 5173, True)]
    assert no_docker == [("up",)]
    assert "Forwarding api.localhost to port 5173 in the background." in result.output


@pytest.mark.parametrize("name", ["localghost", "traefik"])
def test_forward_refuses_reserved_names(started, name) -> None:
    result = CliRunner().invoke(cli, ["forward", name, "5173"])

    assert result.exit_code != 0
    assert started == []


def test_forward_rejects_an_out_of_range_port(started) -> None:
    result = CliRunner().invoke(cli, ["forward", "api", "70000"])

    assert result.exit_code == 2
    assert started == []


def test_forward_refuses_a_remembered_project_name(started, tmp_path) -> None:
    registry.record("api", tmp_path, "node")

    result = CliRunner().invoke(cli, ["forward", "api", "5173"])

    assert result.exit_code != 0
    assert (
        "api is a remembered project; choose another name or run "
        "localghost sessions forget api"
    ) in result.output
    assert started == []


def test_forward_refuses_a_port_localghost_routes(started, tmp_path) -> None:
    sessions.create(
        mode="host",
        name="blog",
        port=5173,
        cwd=tmp_path,
        command=("npm", "run", "dev"),
        log=None,
        pid=os.getpid(),
    )

    result = CliRunner().invoke(cli, ["forward", "api", "5173"])

    assert result.exit_code != 0
    assert "5173 is blog's port; it is already at blog.localhost" in result.output
    assert started == []


def test_forward_refuses_the_hubs_port(started) -> None:
    result = CliRunner().invoke(cli, ["forward", "api", "80"])

    assert result.exit_code != 0
    assert "80 is the localghost hub's port" in result.output


def test_forward_reclaims_a_stale_bridge_route(started, monkeypatch) -> None:
    reclaimed: list = []
    monkeypatch.setattr(cli_module, "find_route_collision", lambda name: "abc123")
    monkeypatch.setattr(
        cli_module, "_reclaim_route", lambda *args: reclaimed.append(args)
    )

    result = CliRunner().invoke(cli, ["forward", "api", "5173"])

    assert result.exit_code == 0, result.output
    assert reclaimed == [("abc123", "api")]
    assert started == [("api", 5173, True)]


def test_forward_refuses_another_port_while_running(started, tmp_path) -> None:
    _forward(tmp_path, port=5173)

    result = CliRunner().invoke(cli, ["forward", "api", "8000"])

    assert result.exit_code != 0
    assert (
        "api is already forwarded to port 5173; use localghost restart api "
        "or localghost sessions stop api"
    ) in result.output
    assert started == []


def test_forward_refuses_a_name_a_project_serves(started, tmp_path) -> None:
    sessions.create(
        mode="host",
        name="api",
        port=3000,
        cwd=tmp_path,
        command=("npm", "run", "dev"),
        log=None,
        pid=os.getpid(),
    )

    result = CliRunner().invoke(cli, ["forward", "api", "5173"])

    assert result.exit_code != 0
    assert f"api.localhost is already served from {tmp_path}" in result.output
    assert started == []


def test_forward_reconnects_to_a_running_forward(
    started, tmp_path, monkeypatch
) -> None:
    running = _forward(tmp_path, port=5173)
    followed: list = []

    def follow(session, *, bar, public_origin, secondary_origin):
        followed.append((session.id, bar, public_origin, secondary_origin))
        return attach.Outcome(None)

    monkeypatch.setattr(cli_module, "follow_forward", follow)
    monkeypatch.setattr(cli_module, "_stdout_is_terminal", lambda: True)

    result = CliRunner().invoke(cli, ["forward", "api", "5173", "--no-status-bar"])

    assert result.exit_code == 0, result.output
    assert followed == [(running.id, False, "http://api.localhost", None)]
    assert started == []
    assert "Detached from api" in result.output


def test_forward_reconnects_by_name_alone(started, tmp_path, monkeypatch) -> None:
    running = _forward(tmp_path, port=5173)
    registry.record_forward("api", 5173, detached=True)
    followed: list = []
    monkeypatch.setattr(
        cli_module,
        "follow_forward",
        lambda session, **kwargs: followed.append(session.id) or attach.Outcome(None),
    )
    monkeypatch.setattr(cli_module, "_stdout_is_terminal", lambda: True)

    result = CliRunner().invoke(cli, ["forward", "api"])

    assert result.exit_code == 0, result.output
    assert followed == [running.id]


def test_forward_detached_reports_a_running_forward(started, tmp_path) -> None:
    _forward(tmp_path, port=5173)

    result = CliRunner().invoke(cli, ["forward", "api", "5173", "--detach"])

    assert result.exit_code == 0, result.output
    assert "api is already forwarded to port 5173" in result.output
    assert started == []


def test_forward_without_terminal_detaches(started, monkeypatch, state) -> None:
    monkeypatch.setattr(
        cli_module,
        "follow_forward",
        lambda *args, **kwargs: pytest.fail("followed without a terminal"),
    )

    result = CliRunner().invoke(cli, ["forward", "api", "5173"])

    assert result.exit_code == 0, result.output
    assert started == [("api", 5173, True)]
    assert "http://api.localhost" in result.output
    assert "port 5173" in result.output
    assert "background" in result.output
    assert "Forwarding api.localhost to port 5173 in the background." in result.output
    (entry,) = registry.entries()
    assert registry.is_forward(entry)
    assert entry.flags.port == 5173
    assert entry.detached is True


def test_forward_in_a_terminal_follows_the_leader(
    started, monkeypatch
) -> None:
    followed: list = []

    def follow(session, *, bar, public_origin, secondary_origin):
        followed.append((session.name, bar, public_origin))
        return attach.Outcome(130)

    monkeypatch.setattr(cli_module, "follow_forward", follow)
    monkeypatch.setattr(cli_module, "_stdout_is_terminal", lambda: True)

    result = CliRunner().invoke(cli, ["forward", "api", "5173"])

    # Ctrl+C ends it with the signal's status, as for a foreground run.
    assert result.exit_code == 130, result.output
    assert started == [("api", 5173, False)]
    assert followed == [("api", True, "http://api.localhost")]
    assert "foreground" in result.output
    assert "Stopped api." in result.output
    (entry,) = registry.entries()
    assert entry.detached is False


def test_second_forward_refuses_while_first_is_starting(started, tmp_path) -> None:
    # The first `forward` has written its record but not yet its leader's pid.
    sessions.create(
        mode="forward",
        name="api",
        port=5173,
        cwd=None,
        command=(),
        log=tmp_path / "api.log",
        pid=None,
    )

    same = CliRunner().invoke(cli, ["forward", "api", "5173"])
    other = CliRunner().invoke(cli, ["forward", "api", "8000"])

    assert same.exit_code != 0
    assert "api is already forwarded to port 5173" in same.output
    assert other.exit_code != 0
    assert "api is already forwarded to port 5173; use" in other.output
    assert started == []


def test_an_old_pid_less_forward_record_does_not_block(
    started, tmp_path, monkeypatch
) -> None:
    sessions.create(
        mode="forward",
        name="api",
        port=5173,
        cwd=None,
        command=(),
        log=tmp_path / "api.log",
        pid=None,
    )
    monkeypatch.setattr(sessions, "recorded_at", lambda session: 0.0)

    result = CliRunner().invoke(cli, ["forward", "api", "5173"])

    assert result.exit_code == 0, result.output
    assert started == [("api", 5173, True)]


def test_forward_gives_up_on_a_leader_that_never_starts(
    monkeypatch, tmp_path
) -> None:
    def start(name, port, *, detach):
        return _forward(tmp_path, name, port, pid=2**22 + 1)

    monkeypatch.setattr(cli_module, "_start_forward", start)
    monkeypatch.setattr(sessions, "alive", lambda item: False)
    monkeypatch.setattr(cli_module, "session_alive", lambda item: False)
    monkeypatch.setattr(cli_module, "_LEADER_WAIT", 0.0)

    result = CliRunner().invoke(cli, ["forward", "api", "5173"])

    assert result.exit_code != 0
    assert "localghost sessions logs api" in result.output
    # The dead record stays, so the log it points at can still be read.
    (record,) = sessions.sessions()
    assert record.pid == 2**22 + 1
    (entry,) = registry.entries()
    assert entry.forward_state == "stopped"


class _FakePopen:
    calls: list = []

    def __init__(self, argv, **kwargs) -> None:
        self.pid = 4242
        self.calls.append((argv, kwargs))
        kwargs["stdout"].write(b"leader output\n")

    def wait(self) -> int:
        return 0


def test_start_forward_spawns_the_leader(monkeypatch, state) -> None:
    _FakePopen.calls = []
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    log = cli_module._session_log_path("api")
    log.parent.mkdir(parents=True)
    log.write_bytes(b"earlier\n")

    session = cli_module._start_forward("api", 5173, detach=False)

    ((argv, kwargs),) = _FakePopen.calls
    assert argv == [sys.executable, "-I", "-m", "localghost.forward_leader", session.id]
    assert kwargs["start_new_session"] is True
    assert kwargs["stderr"] == subprocess.STDOUT
    assert Path(kwargs["stdout"].name) == log
    assert log.read_bytes() == b"earlier\nleader output\n"
    saved = sessions.read(session.id)
    assert saved is not None
    assert saved.pid == 4242
    assert saved.mode == "forward"
    assert saved.cwd is None
    assert saved.port == 5173
    assert saved.socket == ""
    assert saved.log == str(log)
    assert saved.detached is False
    assert session.pid == 4242


def test_start_forward_removes_record_if_spawn_fails(monkeypatch, state) -> None:
    created: list = []
    original = sessions.create

    def create(**kwargs):
        session = original(**kwargs)
        created.append(sessions.read(session.id))
        return session

    def fail(*args, **kwargs):
        raise OSError("no python")

    monkeypatch.setattr(cli_module, "create_session", create)
    monkeypatch.setattr(subprocess, "Popen", fail)

    with pytest.raises(Exception, match="no python"):
        cli_module._start_forward("api", 5173, detach=True)

    # The record was written before the spawn, with no pid yet.
    assert created[0].pid is None
    assert sessions.sessions() == []


def test_forward_name_completion_offers_only_forwards(tmp_path) -> None:
    registry.record_forward("api", 5173, detached=True)
    registry.record_forward("admin", 8000, detached=True)
    registry.record("app", tmp_path, "node")

    assert cli_module._complete_forward_name(None, None, "a") == ["admin", "api"]
    assert cli_module._complete_project_name(None, None, "ap") == ["api", "app"]


def _list(*args):
    return CliRunner().invoke(cli, ["sessions", "list", *args])


def test_sessions_list_shows_forwards(tmp_path) -> None:
    registry.record_forward("api", 5173, detached=True)
    _forward(tmp_path)

    table = _list()
    assert table.exit_code == 0, table.output
    assert "api.localhost" in table.output
    assert "forward" in table.output
    assert "→ port 5173" in table.output

    as_json = _list("--json")
    (project,) = json.loads(as_json.output)
    assert project["directory"] is None
    assert project["type"] == "forward"
    assert project["directory_exists"] is False
    assert project["session"]["port"] == 5173


def test_sessions_list_shows_a_stopped_forward(tmp_path) -> None:
    registry.record_forward("api", 5173, detached=True)

    result = _list("--stopped")

    assert result.exit_code == 0, result.output
    assert "→ port 5173" in result.output
    assert "(missing)" not in result.output


def test_sessions_list_marks_a_waiting_forward(tmp_path) -> None:
    registry.record_forward("api", 5173, detached=True)
    live = _forward(tmp_path)
    sessions.set_route(
        live.id, "waiting: nothing is listening", bridge_project=None, bridge_yaml=""
    )
    assert "running (waiting)" in _list().output

    sessions.set_route(live.id, "up", bridge_project=None, bridge_yaml="")
    assert "waiting" not in _list().output


def test_run_replaces_a_stopped_forward_with_a_note(tmp_path, monkeypatch) -> None:
    registry.record_forward("api", 5173, detached=True)
    resolved = types.SimpleNamespace(
        selected_type="compose", name="api", root=tmp_path, plan=None
    )
    messages: list = []
    monkeypatch.setattr(cli_module, "info", messages.append)

    cli_module._record_registry(resolved, detached=False)

    assert messages == ["Replacing the stopped forward api (port 5173)."]
    (entry,) = registry.entries()
    assert entry.type == "compose"
