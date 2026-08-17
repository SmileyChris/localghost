import json
from pathlib import Path

import click
import pytest

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


def test_config_update_requires_extend_noninteractive_and_backups(tmp_path: Path):
    path = tmp_path / ".localghost.toml"
    path.write_text('[other]\nvalue = "keep"\n')
    with pytest.raises(click.ClickException, match="--extend"):
        write_run_config(path, RunConfig(mode="host"))
    backup = write_run_config(path, RunConfig(mode="host"), extend=True)
    assert backup and backup.read_text() == '[other]\nvalue = "keep"\n'
    assert load_config(path).mode == "host"


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
