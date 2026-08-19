import json
from pathlib import Path

import click
import pytest

from localghost import cli as cli_module
from localghost import sessions as session_store
from localghost.config import (
    RunConfig,
    detect_mode,
    load_config,
    render_run_config,
    write_run_config,
)
from localghost.sessions import create, sessions


def test_config_loads_argv_and_preserves_other_tables(tmp_path: Path):
    path = tmp_path / ".localghost.toml"
    path.write_text(
        '[project]\nlabel = "keep"\n\n[run]\nport = 8080\n'
        'command = ["./server", "--port", "{port}"]\n'
    )
    config = load_config(path)
    assert config.port == 8080
    assert config.command == ("./server", "--port", "{port}")
    rendered = render_run_config(RunConfig(mode="host", port=9000), path.read_text())
    assert 'label = "keep"' in rendered
    assert "port = 9000" in rendered


def test_mode_detection_is_conservative(tmp_path: Path):
    (tmp_path / "compose.yaml").touch()
    assert detect_mode(tmp_path) == "compose"
    (tmp_path / "manage.py").touch()
    with pytest.raises(click.ClickException, match="both Compose"):
        detect_mode(tmp_path)
    assert detect_mode(tmp_path, command=("./server",)) == "host"


def test_mode_detection_finds_php_frameworks(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "artisan").touch()
    (tmp_path / "composer.json").write_text(
        '{"require": {"laravel/framework": "^11.0"}}'
    )

    assert detect_mode(tmp_path) == "host"


def test_mode_detection_walks_up_to_the_project_root(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "manage.py").touch()
    nested = tmp_path / "accounts" / "migrations"
    nested.mkdir(parents=True)

    assert detect_mode(nested) == "host"


def test_config_update_requires_extend_noninteractive_and_backups(tmp_path: Path):
    path = tmp_path / ".localghost.toml"
    path.write_text('[other]\nvalue = "keep"\n')
    with pytest.raises(click.ClickException, match="--extend"):
        write_run_config(path, RunConfig(mode="host"))
    backup = write_run_config(path, RunConfig(mode="host"), extend=True)
    assert backup and backup.read_text() == '[other]\nvalue = "keep"\n'
    assert load_config(path).mode == "host"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('[run]\nmode = "other"\n', "mode"),
        ('[run]\nframework = "other"\n', "framework"),
        ('[run]\nport = true\n', "port"),
        ('[run]\ncommand = "server --port 80"\n', "argv array"),
        ('[run]\nprot = 8000\n', "unknown"),
    ],
)
def test_config_rejects_invalid_or_misspelled_values(tmp_path, content, message):
    path = tmp_path / ".localghost.toml"
    path.write_text(content)

    with pytest.raises(click.ClickException, match=message):
        load_config(path)


@pytest.mark.parametrize(
    "framework", ["django", "vite", "astro", "cakephp", "laravel"]
)
def test_config_accepts_every_framework_the_runner_supports(tmp_path, framework):
    path = tmp_path / ".localghost.toml"
    path.write_text(f'[run]\nframework = "{framework}"\n')

    assert load_config(path).framework == framework


def test_config_reports_unreadable_toml(tmp_path):
    path = tmp_path / ".localghost.toml"
    path.write_text("[run\nport = 8080\n")

    with pytest.raises(click.ClickException, match="could not read"):
        load_config(path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("run = 5\n", r"\[run\] must be a table"),
        ('[run]\nname = ""\n', "non-empty string"),
        ("[run]\nname = 5\n", "non-empty string"),
    ],
)
def test_config_rejects_a_malformed_run_table(tmp_path, content, message):
    path = tmp_path / ".localghost.toml"
    path.write_text(content)

    with pytest.raises(click.ClickException, match=message):
        load_config(path)


def test_missing_config_is_empty(tmp_path):
    assert load_config(tmp_path / "absent.toml") == RunConfig()


def test_mode_detection_reports_an_undetectable_directory(tmp_path):
    (tmp_path / ".git").mkdir()

    with pytest.raises(click.ClickException, match="could not detect a run mode"):
        detect_mode(tmp_path)


def test_writing_a_new_config_makes_no_backup(tmp_path):
    path = tmp_path / ".localghost.toml"

    assert write_run_config(path, RunConfig(mode="host", port=3000)) is None
    assert load_config(path).port == 3000


def test_repeated_writes_rotate_backups(tmp_path):
    path = tmp_path / ".localghost.toml"
    path.write_text('[run]\nport = 1000\n')
    write_run_config(path, RunConfig(port=2000), extend=True)

    second = write_run_config(path, RunConfig(port=3000), extend=True)

    assert second is not None and second.name == ".localghost.toml.bak.1"
    assert load_config(path).port == 3000


def test_rendered_config_escapes_control_characters(tmp_path):
    rendered = render_run_config(
        RunConfig(mode="host", command=("server", "line\nvalue"))
    )
    path = tmp_path / ".localghost.toml"
    path.write_text(rendered)

    assert load_config(path).command == ("server", "line\nvalue")


def test_session_metadata_is_filesystem_backed(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(state))
    session = create(
        mode="host",
        name="demo",
        port=8080,
        cwd=tmp_path,
        command=("server",),
        log=tmp_path / "run.log",
        pid=999999,
    )
    assert sessions()[0].id == session.id
    data = json.loads(next((state / "sessions").glob("*.json")).read_text())
    assert data["command"] == ["server"]


def test_unreadable_session_metadata_is_reported_not_silently_dropped(
    tmp_path, monkeypatch, capsys
):
    """A corrupt record must not make a running application invisible."""
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path / "state"))
    good = create(
        mode="host",
        name="good",
        port=8080,
        cwd=tmp_path,
        command=("server",),
        log=tmp_path / "good.log",
        pid=None,
    )
    (session_store.state_dir() / "broken.json").write_text("{not json")

    listed = sessions()

    assert [item.id for item in listed] == [good.id]
    assert "broken.json" in capsys.readouterr().err


def test_stop_keeps_the_record_when_the_process_survives(tmp_path, monkeypatch):
    """Unlinking a surviving session would orphan the process beyond recovery."""
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path / "state"))
    session = create(
        mode="host",
        name="stubborn",
        port=8080,
        cwd=tmp_path,
        command=("server",),
        log=tmp_path / "run.log",
        pid=4321,
    )
    clock = iter(range(0, 1000))
    monkeypatch.setattr(session_store, "alive", lambda item: True)
    monkeypatch.setattr(session_store.os, "killpg", lambda pid, signum: None)
    monkeypatch.setattr(session_store.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(session_store.time, "sleep", lambda seconds: None)

    with pytest.raises(click.ClickException, match="did not stop"):
        session_store.stop(session)

    assert [item.id for item in sessions()] == [session.id]


def test_session_state_follows_xdg_state_home(tmp_path, monkeypatch):
    """Sessions must share the state directory documented in operations.md."""
    monkeypatch.delenv("LOCALGHOST_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

    assert session_store.state_dir() == tmp_path / "xdg" / "localghost" / "sessions"
    assert cli_module._state_directory() == tmp_path / "xdg" / "localghost"


def test_localghost_state_dir_overrides_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path / "explicit"))

    assert session_store.state_dir() == tmp_path / "explicit" / "sessions"
    assert cli_module._state_directory() == tmp_path / "explicit"


def test_detached_bridge_cleanup_reuses_stored_compose_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path / "state"))
    session = create(
        mode="host",
        name="demo",
        port=8080,
        cwd=tmp_path,
        command=("server",),
        log=tmp_path / "run.log",
        pid=None,
        bridge_project="bridge-project",
        bridge_yaml="services:\n  bridge: {}\n",
    )
    calls = []
    monkeypatch.setattr(
        session_store.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    session_store.stop(session)

    command, kwargs = calls[0]
    assert command == [
        "docker",
        "compose",
        "--project-name",
        "bridge-project",
        "--file",
        "-",
        "down",
        "--remove-orphans",
    ]
    assert kwargs["input"] == "services:\n  bridge: {}\n"
    assert not sessions()
