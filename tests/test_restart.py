"""`restart`, and the run mode registry entries remember for it."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from localghost import cli as cli_module
from localghost import registry
from localghost.cli import cli
from localghost.sessions import create


def _session(tmp_path: Path, **overrides):
    values = {
        "mode": "host",
        "name": "blog",
        "port": 8080,
        "cwd": tmp_path,
        "command": ("server",),
        "log": tmp_path / "blog.log",
        "pid": None,
    }
    values.update(overrides)
    return create(**values)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "blog"
    directory.mkdir()
    return directory


@pytest.fixture
def captured(monkeypatch) -> dict:
    calls: dict = {}
    monkeypatch.setattr(
        cli_module.run, "callback", lambda **kwargs: calls.update(kwargs)
    )
    return calls


@pytest.fixture
def stopped(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(cli_module, "stop_session", calls.append)
    return calls


def test_restart_starts_a_stopped_project_the_way_it_last_ran(
    project, captured, stopped
) -> None:
    registry.record("blog", project, "django", detached=True)

    result = CliRunner().invoke(cli, ["restart", "blog"])

    assert result.exit_code == 0, result.output
    assert stopped == []
    assert captured["working_directory"] == project
    assert captured["detach"] is True
    assert captured["name"] == "blog"


def test_restart_stops_a_detached_project_and_starts_it_again(
    project, captured, stopped
) -> None:
    registry.record("blog", project, "django", detached=True)
    running = _session(project, pid=os.getpid())

    result = CliRunner().invoke(cli, ["restart", "blog"])

    assert result.exit_code == 0, result.output
    assert [item.id for item in stopped] == [running.id]
    assert captured["detach"] is True


@pytest.mark.parametrize(
    ("flag", "detach"), [("--foreground", False), ("--detach", True)]
)
def test_restart_mode_flags_override_the_remembered_mode(
    project, captured, stopped, flag, detach
) -> None:
    registry.record("blog", project, "django", detached=not detach)

    result = CliRunner().invoke(cli, ["restart", "blog", flag])

    assert result.exit_code == 0, result.output
    assert captured["detach"] is detach


def test_restart_rejects_both_mode_flags(project, captured) -> None:
    registry.record("blog", project, "django")

    result = CliRunner().invoke(cli, ["restart", "blog", "--foreground", "--detach"])

    assert result.exit_code == 2
    assert captured == {}


def test_restart_takes_over_a_foreground_run_after_confirming(
    project, captured, stopped, monkeypatch
) -> None:
    registry.record("blog", project, "django")
    running = _session(project, pid=os.getpid(), detached=False)
    monkeypatch.setattr(cli_module, "_restart_interactive", lambda: True)

    declined = CliRunner().invoke(cli, ["restart", "blog"], input="n\n")
    assert declined.exit_code == 1
    assert stopped == [] and captured == {}

    result = CliRunner().invoke(cli, ["restart", "blog"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "running in the foreground in another terminal" in result.output
    assert [item.id for item in stopped] == [running.id]
    assert captured["detach"] is False


def test_restart_refuses_a_foreground_takeover_without_a_terminal(
    project, captured, stopped, monkeypatch
) -> None:
    registry.record("blog", project, "django")
    _session(project, pid=os.getpid(), detached=False)
    monkeypatch.setattr(cli_module, "_restart_interactive", lambda: False)

    result = CliRunner().invoke(cli, ["restart", "blog"])

    assert result.exit_code == 1
    assert "localghost sessions stop blog" in result.output
    assert stopped == [] and captured == {}


def test_restart_accepts_a_session_id(project, captured, stopped) -> None:
    registry.record("blog", project, "django")
    running = _session(project, pid=os.getpid(), detached=True)

    result = CliRunner().invoke(cli, ["restart", running.id])

    assert result.exit_code == 0, result.output
    assert captured["name"] == "blog"
    assert captured["working_directory"] == project


def test_restart_starts_a_session_with_no_registry_entry(
    project, captured, stopped
) -> None:
    _session(project, pid=None, detached=True)

    result = CliRunner().invoke(cli, ["restart", "blog"])

    assert result.exit_code == 0, result.output
    assert captured["working_directory"] == project
    assert captured["detach"] is True


def test_bare_restart_uses_the_project_in_the_current_directory(
    project, captured, stopped, monkeypatch
) -> None:
    registry.record("blog", project, "django")
    nested = project / "app"
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setattr(
        cli_module.picker, "pick", lambda *args, **kwargs: pytest.fail("picked")
    )

    result = CliRunner().invoke(cli, ["restart"])

    assert result.exit_code == 0, result.output
    assert captured["working_directory"] == project


def test_bare_restart_outside_a_project_lists_without_a_terminal(
    tmp_path, project, captured, monkeypatch
) -> None:
    registry.record("blog", project, "django")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["restart"])

    assert result.exit_code == 0, result.output
    assert "blog.localhost" in result.output
    assert captured == {}


def test_summon_is_a_deprecated_alias_for_restart(project, captured) -> None:
    registry.record("blog", project, "django")

    result = CliRunner().invoke(cli, ["summon", "blog"])

    assert result.exit_code == 0, result.output
    assert "localghost restart" in result.output
    assert captured["working_directory"] == project
    assert "summon" not in CliRunner().invoke(cli, ["--help"]).output


def test_old_registry_entries_without_a_detached_field_still_load(tmp_path) -> None:
    entry = registry.registry_dir() / "old.json"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        json.dumps(
            {
                "hostname": "old.localhost",
                "name": "old",
                "directory": str(tmp_path),
                "type": "vite",
                "last_started": "2026-01-01T00:00:00+00:00",
            }
        )
    )

    assert registry.entries()[0].detached is False


def test_save_keeps_the_remembered_mode(tmp_path) -> None:
    registry.record("blog", tmp_path, "django", detached=True)
    registry.record("blog", tmp_path, "django")

    assert registry.entries()[0].detached is True
