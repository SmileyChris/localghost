"""Behaviour of `localghost manage` and the session store it drives."""

import json
import os
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from localghost import sessions as session_store
from localghost.cli import cli
from localghost.sessions import Session, clean, create, sessions, stop


def _session(tmp_path: Path, **overrides) -> Session:
    values = {
        "mode": "host",
        "name": "demo",
        "port": 8080,
        "cwd": tmp_path,
        "command": ("server",),
        "log": tmp_path / "demo.log",
        "pid": None,
    }
    values.update(overrides)
    return create(**values)


def test_manage_list_reports_no_sessions() -> None:
    result = CliRunner().invoke(cli, ["manage", "list"])

    assert result.exit_code == 0, result.output
    assert "No managed sessions." in result.output


def test_bare_manage_lists_sessions(tmp_path) -> None:
    _session(tmp_path, pid=os.getpid())

    result = CliRunner().invoke(cli, ["manage"])

    assert result.exit_code == 0, result.output
    assert "demo.localhost" in result.output
    assert "running" in result.output


def test_manage_list_marks_a_dead_session_stopped(tmp_path) -> None:
    _session(tmp_path, pid=None)

    result = CliRunner().invoke(cli, ["manage", "list"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output


def test_manage_list_json_is_machine_readable(tmp_path) -> None:
    session = _session(tmp_path, pid=os.getpid())

    result = CliRunner().invoke(cli, ["manage", "list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [item["id"] for item in payload] == [session.id]
    assert payload[0]["status"] == "running"


def test_manage_attach_rejects_an_unknown_session() -> None:
    result = CliRunner().invoke(cli, ["manage", "attach", "nope"])

    assert result.exit_code != 0
    assert "unknown session 'nope'" in result.output


def test_manage_attach_prints_the_log(tmp_path) -> None:
    log = tmp_path / "demo.log"
    log.write_text("first line\n")
    session = _session(tmp_path, log=log)

    result = CliRunner().invoke(cli, ["manage", "attach", session.id])

    assert result.exit_code == 0, result.output
    assert "first line" in result.output


def test_manage_attach_reports_a_missing_log(tmp_path) -> None:
    session = _session(tmp_path, log=tmp_path / "absent.log")

    result = CliRunner().invoke(cli, ["manage", "attach", session.id])

    assert result.exit_code == 0, result.output
    assert "has no log yet" in result.output


@pytest.mark.parametrize("arguments", [[], ["one", "--all"]])
def test_manage_stop_requires_exactly_one_target(arguments) -> None:
    result = CliRunner().invoke(cli, ["manage", "stop", *arguments])

    assert result.exit_code != 0
    assert "provide a session ID or --all" in result.output


def test_manage_stop_reports_no_match() -> None:
    result = CliRunner().invoke(cli, ["manage", "stop", "missing"])

    assert result.exit_code != 0
    assert "no matching managed session" in result.output


def test_manage_stop_removes_the_named_session(tmp_path) -> None:
    session = _session(tmp_path)
    other = _session(tmp_path, name="other")

    result = CliRunner().invoke(cli, ["manage", "stop", session.id])

    assert result.exit_code == 0, result.output
    assert "Stopped 1 session(s)." in result.output
    assert [item.id for item in sessions()] == [other.id]


def test_manage_stop_all_removes_every_session(tmp_path) -> None:
    _session(tmp_path)
    _session(tmp_path, name="other")

    result = CliRunner().invoke(cli, ["manage", "stop", "--all"])

    assert result.exit_code == 0, result.output
    assert "Stopped 2 session(s)." in result.output
    assert sessions() == []


def test_manage_stop_all_continues_past_a_session_that_will_not_die(
    tmp_path, monkeypatch
) -> None:
    stubborn = _session(tmp_path, name="stubborn")
    other = _session(tmp_path, name="other")
    stopped = []

    def _stop(session):
        if session.id == stubborn.id:
            raise click.ClickException(f"session {session.id} did not stop")
        stopped.append(session.id)

    # Pin the order so the failing session is always attempted first.
    monkeypatch.setattr("localghost.cli.sessions", lambda: [stubborn, other])
    monkeypatch.setattr("localghost.cli.stop_session", _stop)

    result = CliRunner().invoke(cli, ["manage", "stop", "--all"])

    assert stopped == [other.id], "a failure must not strand the other sessions"
    assert result.exit_code != 0
    assert "did not stop" in result.output


def test_manage_clean_removes_only_dead_sessions(tmp_path) -> None:
    live = _session(tmp_path, name="live", pid=os.getpid())
    _session(tmp_path, name="dead", pid=None)

    result = CliRunner().invoke(cli, ["manage", "clean"])

    assert result.exit_code == 0, result.output
    assert "Removed 1 stale session(s)." in result.output
    assert [item.id for item in sessions()] == [live.id]


def test_clean_tears_down_the_bridge_of_a_dead_session(tmp_path, monkeypatch) -> None:
    _session(tmp_path, pid=None, bridge_project="bridge", bridge_yaml="services: {}\n")
    calls = []
    monkeypatch.setattr(
        session_store.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command),
    )

    assert clean() == 1
    assert calls[0][:4] == ["docker", "compose", "--project-name", "bridge"]
    assert calls[0][-2:] == ["down", "--remove-orphans"]


def test_alive_probes_compose_projects_with_docker(tmp_path, monkeypatch) -> None:
    session = _session(tmp_path, mode="compose", project="demo")
    recorded = {}

    class _Result:
        stdout = "container-id\n"

    def _run(command, **kwargs):
        recorded["command"] = command
        return _Result()

    monkeypatch.setattr(session_store.subprocess, "run", _run)

    assert session_store.alive(session) is True
    assert recorded["command"] == [
        "docker",
        "compose",
        "--project-name",
        "demo",
        "ps",
        "-q",
    ]


def test_alive_reports_a_compose_project_with_no_containers(
    tmp_path, monkeypatch
) -> None:
    session = _session(tmp_path, mode="compose", project="demo")

    class _Result:
        stdout = "\n"

    monkeypatch.setattr(
        session_store.subprocess, "run", lambda command, **kwargs: _Result()
    )

    assert session_store.alive(session) is False


def test_alive_treats_an_unknown_pid_as_stopped(tmp_path) -> None:
    session = _session(tmp_path, pid=2**22)

    assert session_store.alive(session) is False


def test_stop_takes_a_compose_project_down(tmp_path, monkeypatch) -> None:
    session = _session(tmp_path, mode="compose", project="demo", pid=None)
    calls = []
    monkeypatch.setattr(
        session_store.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command),
    )

    stop(session)

    assert calls[0] == ["docker", "compose", "--project-name", "demo", "down"]
    assert sessions() == []


def test_stop_escalates_to_sigkill_when_sigterm_is_ignored(
    tmp_path, monkeypatch
) -> None:
    session = _session(tmp_path, pid=4321)
    signals = []
    clock = iter(float(tick) for tick in range(0, 1000))
    # Alive through SIGTERM and the grace period, dead once SIGKILL lands.
    liveness = iter([True, True, True, True, False])
    monkeypatch.setattr(session_store, "alive", lambda item: next(liveness))
    monkeypatch.setattr(
        session_store.os, "killpg", lambda pid, signum: signals.append(signum)
    )
    monkeypatch.setattr(session_store.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(session_store.time, "sleep", lambda seconds: None)

    stop(session)

    import signal as signal_module

    assert signals == [signal_module.SIGTERM, signal_module.SIGKILL]
    assert sessions() == []


def test_stop_ignores_a_process_that_disappeared(tmp_path, monkeypatch) -> None:
    session = _session(tmp_path, pid=4321)
    monkeypatch.setattr(session_store, "alive", lambda item: True)

    def _killpg(pid, signum):
        raise ProcessLookupError

    monkeypatch.setattr(session_store.os, "killpg", _killpg)
    clock = iter(float(tick) for tick in range(0, 1000))
    monkeypatch.setattr(session_store.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(session_store.time, "sleep", lambda seconds: None)

    with pytest.raises(click.ClickException, match="did not stop"):
        stop(session)
