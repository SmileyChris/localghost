import json

from click.testing import CliRunner

from localghost import registry
from localghost.cli import cli


def test_record_writes_entry_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record("blog", tmp_path / "blog", "django")
    payload = json.loads((tmp_path / "registry" / "blog.json").read_text())
    assert payload["hostname"] == "blog.localhost"
    assert payload["name"] == "blog"
    assert payload["directory"] == str(tmp_path / "blog")
    assert payload["type"] == "django"
    # ISO-8601 with offset, parseable by Go's RFC3339.
    assert "T" in payload["last_started"]


def test_record_overwrites_and_refreshes(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record("blog", tmp_path / "old", "django")
    registry.record("blog", tmp_path / "new", "compose")
    entries = registry.entries()
    assert len(entries) == 1
    assert entries[0].directory == str(tmp_path / "new")
    assert entries[0].type == "compose"


def test_entries_skips_unreadable_files(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record("good", tmp_path, "vite")
    (tmp_path / "registry" / "bad.json").write_text("{not json")
    entries = registry.entries()
    assert [e.name for e in entries] == ["good"]


def test_entries_empty_when_directory_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    assert registry.entries() == []


def test_forget_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record("blog", tmp_path, "django")
    assert registry.forget("blog") is True
    assert registry.forget("blog") is False
    assert registry.entries() == []


def test_forget_all(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record("a", tmp_path, "django")
    registry.record("b", tmp_path, "vite")
    assert registry.forget_all() == 2
    assert registry.entries() == []


def test_record_warns_instead_of_raising(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    (tmp_path / "registry").write_text("a file, not a directory")
    registry.record("blog", tmp_path, "django")  # must not raise


def test_forget_command_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record("blog", tmp_path, "django")
    result = CliRunner().invoke(cli, ["forget", "blog"])
    assert result.exit_code == 0
    assert "blog" in result.output
    assert registry.entries() == []


def test_forget_unknown_name_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    result = CliRunner().invoke(cli, ["forget", "nothing"])
    assert result.exit_code != 0
    assert "no ghost page entry" in result.output


def test_forget_all_command(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record("a", tmp_path, "django")
    registry.record("b", tmp_path, "vite")
    result = CliRunner().invoke(cli, ["forget", "--all"])
    assert result.exit_code == 0
    assert registry.entries() == []


def test_forget_requires_name_or_all(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    result = CliRunner().invoke(cli, ["forget"])
    assert result.exit_code != 0
