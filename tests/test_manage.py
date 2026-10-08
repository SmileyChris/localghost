"""Behaviour of `localghost sessions` and the session store it drives."""

import json
import os
import subprocess
import threading
from pathlib import Path
from subprocess import CompletedProcess

import click
import pytest
from click.testing import CliRunner

from localghost import registry
from localghost import sessions as session_store
from localghost.cli import cli
from localghost.runner import RunPlan
from localghost.sessions import Session, clean, create, find, sessions, stop


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


def test_sessions_list_reports_no_sessions() -> None:
    result = CliRunner().invoke(cli, ["sessions", "list"])

    assert result.exit_code == 0, result.output
    assert "No managed sessions." in result.output


def test_bare_sessions_lists_sessions(tmp_path) -> None:
    _session(tmp_path, pid=os.getpid())

    result = CliRunner().invoke(cli, ["sessions"])

    assert result.exit_code == 0, result.output
    assert "demo.localhost" in result.output
    assert "running" in result.output


def test_sessions_list_marks_a_dead_session_stopped(tmp_path) -> None:
    _session(tmp_path, pid=None)

    result = CliRunner().invoke(cli, ["sessions", "list"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output


def test_sessions_list_json_is_machine_readable(tmp_path) -> None:
    session = _session(tmp_path, pid=os.getpid())

    result = CliRunner().invoke(cli, ["sessions", "list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [item["id"] for item in payload] == [session.id]
    assert payload[0]["status"] == "running"


def test_sessions_logs_rejects_an_unknown_session() -> None:
    result = CliRunner().invoke(cli, ["sessions", "logs", "nope"])

    assert result.exit_code != 0
    assert "no session for 'nope'" in result.output


def test_sessions_logs_prints_the_log(tmp_path) -> None:
    log = tmp_path / "demo.log"
    log.write_text("first line\n")
    session = _session(tmp_path, log=log)

    result = CliRunner().invoke(cli, ["sessions", "logs", session.id])

    assert result.exit_code == 0, result.output
    assert "first line" in result.output


def test_sessions_logs_reports_a_missing_log(tmp_path) -> None:
    session = _session(tmp_path, log=tmp_path / "absent.log")

    result = CliRunner().invoke(cli, ["sessions", "logs", session.id])

    assert result.exit_code == 0, result.output
    assert "has no log yet" in result.output


def test_sessions_logs_defers_to_compose_for_compose_sessions(
    tmp_path, monkeypatch
) -> None:
    session = _session(tmp_path, mode="compose", project="blog")
    calls = []
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or CompletedProcess(command, 0),
    )

    result = CliRunner().invoke(cli, ["sessions", "logs", session.id, "-f"])

    assert result.exit_code == 0, result.output
    assert calls[0][0] == [
        "docker",
        "compose",
        "--project-name",
        "blog",
        "logs",
        "--follow",
    ]
    assert calls[0][1]["cwd"] == str(tmp_path)


def test_manage_group_and_attach_command_are_gone() -> None:
    for arguments in (
        ["manage"],
        ["manage", "list"],
        ["sessions", "attach", "abc123"],
    ):
        result = CliRunner().invoke(cli, arguments)

        assert result.exit_code != 0, arguments
        assert "no such command" in result.output.lower()


@pytest.mark.parametrize("arguments", [[], ["one", "--all"]])
def test_sessions_stop_requires_exactly_one_target(arguments) -> None:
    result = CliRunner().invoke(cli, ["sessions", "stop", *arguments])

    assert result.exit_code != 0
    assert "provide a project name, a session ID, or --all" in result.output


def test_sessions_stop_reports_no_match() -> None:
    result = CliRunner().invoke(cli, ["sessions", "stop", "missing"])

    assert result.exit_code != 0
    assert "no session for 'missing'" in result.output


def test_sessions_stop_removes_the_named_session(tmp_path) -> None:
    session = _session(tmp_path)
    other = _session(tmp_path, name="other")

    result = CliRunner().invoke(cli, ["sessions", "stop", session.id])

    assert result.exit_code == 0, result.output
    assert "Stopped 1 session(s)." in result.output
    assert [item.id for item in sessions()] == [other.id]


def test_sessions_stop_all_removes_every_session(tmp_path) -> None:
    _session(tmp_path)
    _session(tmp_path, name="other")

    result = CliRunner().invoke(cli, ["sessions", "stop", "--all"])

    assert result.exit_code == 0, result.output
    assert "Stopped 2 session(s)." in result.output
    assert sessions() == []


def test_sessions_stop_all_continues_past_a_session_that_will_not_die(
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

    result = CliRunner().invoke(cli, ["sessions", "stop", "--all"])

    assert stopped == [other.id], "a failure must not strand the other sessions"
    assert result.exit_code != 0
    assert "did not stop" in result.output


def test_sessions_clean_removes_only_dead_sessions(tmp_path) -> None:
    live = _session(tmp_path, name="live", pid=os.getpid())
    _session(tmp_path, name="dead", pid=None)

    result = CliRunner().invoke(cli, ["sessions", "clean"])

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


def test_sessions_logs_follow_tails_until_the_session_exits(
    tmp_path, monkeypatch
) -> None:
    log = tmp_path / "demo.log"
    log.write_text("first line\n")
    session = _session(tmp_path, log=log)
    polls = iter([True, False])

    def alive(_session):
        # Append a line while the follower thinks the session is alive, so
        # the tail loop has to pick it up before noticing the exit.
        if next(polls):
            with log.open("a") as handle:
                handle.write("second line\n")
            return True
        return False

    monkeypatch.setattr("localghost.cli.session_alive", alive)
    monkeypatch.setattr("localghost.cli.time.sleep", lambda _seconds: None)

    result = CliRunner().invoke(cli, ["sessions", "logs", session.id, "--follow"])

    assert result.exit_code == 0, result.output
    assert result.output == "first line\nsecond line\n"


def test_find_prefers_an_exact_id_then_the_running_record(tmp_path) -> None:
    stale = _session(tmp_path, pid=None)
    live = _session(tmp_path, pid=os.getpid())
    named_like_an_id = _session(tmp_path, name=stale.id, pid=os.getpid())

    assert find(stale.id).id == stale.id
    assert find("demo").id == live.id
    assert find(named_like_an_id.name).id == stale.id
    assert find("missing") is None


def test_sessions_stop_and_logs_accept_a_project_name(tmp_path) -> None:
    session = _session(tmp_path, pid=None)
    Path(session.log).write_text("hello\n", encoding="utf-8")

    logs = CliRunner().invoke(cli, ["sessions", "logs", "demo"])
    stopped = CliRunner().invoke(cli, ["sessions", "stop", "demo"])

    assert logs.exit_code == 0 and "hello" in logs.output
    assert stopped.exit_code == 0, stopped.output
    assert sessions() == []


def test_sessions_logs_of_a_foreground_run_points_at_its_terminal(tmp_path) -> None:
    _session(tmp_path, pid=os.getpid(), detached=False, log=None)

    result = CliRunner().invoke(cli, ["sessions", "logs", "demo"])

    assert result.exit_code == 0, result.output
    assert "foreground in another terminal" in result.output


def test_old_records_without_a_detached_field_still_load(tmp_path) -> None:
    _session(tmp_path)
    path = next(session_store.state_dir().glob("*.json"))
    payload = json.loads(path.read_text())
    del payload["detached"]
    path.write_text(json.dumps(payload))

    assert sessions()[0].detached is True


def test_stopping_a_foreground_run_signals_only_its_process(tmp_path) -> None:
    # Not a process-group leader, as a foreground localghost often isn't;
    # signalling a group by its pid would miss it.
    child = subprocess.Popen(["sleep", "30"])
    threading.Thread(target=child.wait, daemon=True).start()
    session = _session(tmp_path, pid=child.pid, detached=False)

    stop(session)

    assert child.wait(timeout=5) == -15
    assert sessions() == []


@pytest.fixture
def host_plan(monkeypatch, tmp_path) -> RunPlan:
    project = tmp_path / "demo"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "manage.py").touch()
    plan = RunPlan(
        "demo",
        "django",
        ("run",),
        3000,
        "session",
        "services: {}\n",
        project_root=project,
        working_directory=project,
    )
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)
    monkeypatch.setattr(
        "localghost.cli.django_settings_warnings", lambda *args, **kwargs: []
    )
    monkeypatch.setattr("localghost.cli._print_run_plan", lambda *args, **kwargs: None)
    monkeypatch.setattr("localghost.cli._tailnet_origin", lambda name: None)
    return plan


def test_foreground_run_is_recorded_while_it_runs(host_plan, monkeypatch) -> None:
    project = host_plan.project_root
    _session(project, pid=None)
    seen = []

    def _execute(*args, **kwargs):
        seen.extend(sessions())
        return 0

    monkeypatch.setattr("localghost.cli.execute", _execute)

    result = CliRunner().invoke(cli, ["run", "-C", str(project)])

    assert result.exit_code == 0, result.output
    assert [(item.detached, item.pid) for item in seen] == [(False, os.getpid())]
    assert sessions() == [], "the record goes when the run ends; stale ones too"
    assert registry.entries()[0].detached is False


def test_run_refuses_a_name_served_from_another_directory(
    tmp_path, host_plan, monkeypatch
) -> None:
    worktree = tmp_path / "demo-feature"
    worktree.mkdir()
    _session(worktree, pid=os.getpid(), detached=False)
    monkeypatch.setattr(
        "localghost.cli.execute", lambda *args, **kwargs: pytest.fail("ran")
    )

    result = CliRunner().invoke(cli, ["run", "-C", str(host_plan.project_root)])

    assert result.exit_code == 1
    assert f"already served from {worktree}" in result.output
    assert "--name" in result.output
