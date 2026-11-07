"""Lookups that turn a directory into a Path must skip forwards."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from localghost import cli as cli_module
from localghost import picker, registry, runner, sessions
from localghost.cli import cli


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(directory))
    return directory


def _live_forward(tmp_path: Path, monkeypatch, name: str = "api") -> None:
    sessions.create(
        mode="forward",
        name=name,
        port=5173,
        cwd=None,
        command=(),
        log=tmp_path / "forward.log",
        pid=1,
    )
    monkeypatch.setattr(sessions, "alive", lambda item: True)


def test_entry_for_directory_never_picks_a_forward(tmp_path) -> None:
    registry.record_forward("api", 5173, detached=False)
    entries = registry.entries()

    assert cli_module._entry_for_directory(entries, tmp_path) is None
    assert cli_module._entry_for_directory(entries, Path("/")) is None


def test_run_refuses_a_name_a_live_forward_serves(tmp_path, monkeypatch) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "x"}})
    )
    _live_forward(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        cli, ["run", "--name", "api", "-C", str(tmp_path), "--dry-run"]
    )

    assert result.exit_code != 0
    assert "forwarded to port 5173" in result.output
    assert "sessions stop api" in result.output


def test_forget_missing_keeps_forwards(state) -> None:
    registry.record_forward("api", 5173, detached=False)

    result = CliRunner().invoke(cli, ["sessions", "forget", "--missing"])

    assert result.exit_code == 0, result.output
    assert (state / "registry" / "api.json").exists()


def test_live_host_session_in_ignores_forwards(tmp_path, monkeypatch) -> None:
    _live_forward(tmp_path, monkeypatch)

    assert cli_module._live_host_session_in(tmp_path) is None


def test_bare_restart_outside_projects_ignores_forwards(
    tmp_path, monkeypatch
) -> None:
    registry.record_forward("api", 5173, detached=False)
    calls: list = []
    monkeypatch.setattr(cli_module, "_restart_entry", lambda *a, **k: calls.append(a))
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["restart"])

    assert result.exit_code == 0, result.output
    assert calls == []


def test_resolve_name_survives_a_remembered_forward(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    registry.record_forward("app", 5173, detached=False)
    # A generic folder name climbs to its parent, which is when the registry
    # is scanned for a project already remembered under the folder name.
    project = tmp_path / "blog" / "app"
    project.mkdir(parents=True)
    (tmp_path / "blog" / ".git").mkdir()

    assert runner.resolve_name(project) == "blog"


def test_picker_row_shows_a_forward_port() -> None:
    registry.record_forward("api", 5173, detached=False)
    model = picker.PickerModel(registry.entries())

    rows = "\n".join(picker.render_lines(model))

    assert "→ port 5173" in rows
