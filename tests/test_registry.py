import json
from pathlib import Path
from subprocess import CompletedProcess

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


def test_forget_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    escape_target = tmp_path / "escape.json"
    escape_target.write_text("not a registry entry")
    assert registry.forget("../escape") is False
    assert escape_target.exists()


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


# -- recording wiring: `save` and `run` -------------------------------------


def test_save_host_type_records_registry_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("localghost.runner.shutil.which", lambda _: "/usr/bin/php")
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("index.php").touch()
        result = runner.invoke(
            cli,
            ["save", "--no-input", "--type", "php", "--port", "3000"],
            env={"COMPOSE_PROJECT_NAME": "sample-project"},
        )
        assert result.exit_code == 0, result.output
    saved = registry.entries()
    assert len(saved) == 1
    assert saved[0].type == "php"


def test_save_dry_run_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("localghost.runner.shutil.which", lambda _: "/usr/bin/php")
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("index.php").touch()
        result = runner.invoke(
            cli,
            ["save", "--no-input", "--dry-run", "--type", "php", "--port", "3000"],
            env={"COMPOSE_PROJECT_NAME": "sample-project"},
        )
        assert result.exit_code == 0, result.output
    assert registry.entries() == []


def test_save_compose_via_files_records_registry_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "localghost.cli.resolve_compose",
        lambda files: {
            "name": "sample-project",
            "networks": {"default": {"name": "sample-project_default"}},
            "services": {"web": {"expose": [8000], "networks": {"default": None}}},
        },
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["save", "--no-input", "--file", "compose.yaml"],
            env={"COMPOSE_PROJECT_NAME": "sample-project"},
        )
        assert result.exit_code == 0, result.output
    saved = registry.entries()
    assert len(saved) == 1
    assert saved[0].name == "sample-project"
    assert saved[0].type == "compose"


def test_save_dockerfile_records_registry_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["save", "--no-input", "--port", "8000"],
            env={"COMPOSE_PROJECT_NAME": "sample-project"},
        )
        assert result.exit_code == 0, result.output
    saved = registry.entries()
    assert len(saved) == 1
    assert saved[0].type == "dockerfile"


def test_run_host_type_records_registry_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)
    monkeypatch.setattr("localghost.cli.execute", lambda *args, **kwargs: 0)
    result = CliRunner().invoke(
        cli, ["run", "--name", "demo", "--port", "3000", "--", "echo"]
    )
    assert result.exit_code == 0, result.output
    saved = registry.entries()
    assert len(saved) == 1
    assert saved[0].name == "demo"
    assert saved[0].type == "custom"


def test_run_dry_run_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "localghost.cli.find_route_collision",
        lambda name: (_ for _ in ()).throw(AssertionError("inspected Docker")),
    )
    monkeypatch.setattr(
        "localghost.cli.execute",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ran")),
    )
    result = CliRunner().invoke(
        cli, ["run", "--dry-run", "--name", "demo", "--port", "3000", "--", "echo"]
    )
    assert result.exit_code == 0, result.output
    assert registry.entries() == []


def test_run_compose_records_registry_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    monkeypatch.setattr(
        "localghost.cli.resolve_compose",
        lambda files, **kwargs: {
            "name": "demo",
            "networks": {"localghost": {"external": True}},
            "services": {
                "web": {
                    "labels": {"traefik.enable": "true"},
                    "networks": {"localghost": None},
                }
            },
        },
    )
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 0),
    )
    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--type", "compose", "--name", "demo"]
    )
    assert result.exit_code == 0, result.output
    saved = registry.entries()
    assert len(saved) == 1
    assert saved[0].name == "demo"
    assert saved[0].type == "compose"
