"""`save` is one command: `--type` picks the branch, detection fills it in."""

from pathlib import Path

from click.testing import CliRunner

from localghost.cli import cli


def _fake_compose_model() -> dict:
    return {
        "name": "sample-project",
        "networks": {"default": {"name": "sample-project_default"}},
        "services": {"web": {"expose": [8000], "networks": {"default": None}}},
    }


def test_save_help_is_flat_and_lists_every_option() -> None:
    result = CliRunner().invoke(cli, ["save", "--help"])

    assert result.exit_code == 0, result.output
    assert "Commands:" not in result.output
    for option in (
        "--type",
        "--directory",
        "--name",
        "--project-root",
        "--config",
        "--port",
        "--file",
        "--service",
        "--output",
        "--extend",
        "--dry-run",
        "--no-input",
        "--run",
    ):
        assert option in result.output, option


def test_save_type_compose_writes_the_override_and_pins_the_type(monkeypatch) -> None:
    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files: _fake_compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["save", "--type", "compose", "--no-input"])

        assert result.exit_code == 0, result.output
        assert Path("compose.override.yaml").exists()
        assert 'type = "compose"' in Path(".localghost.toml").read_text()


def test_detected_compose_save_never_pins(monkeypatch) -> None:
    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files: _fake_compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["save", "--no-input"])

        assert result.exit_code == 0, result.output
        assert Path("compose.override.yaml").exists()
        assert not Path(".localghost.toml").exists()


def test_save_rejects_compose_flags_for_a_host_type() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("manage.py").touch()
        result = runner.invoke(cli, ["save", "--no-input", "--service", "web"])

        assert result.exit_code != 0
        assert "--service" in result.output
        assert "django" in result.output


def test_save_rejects_host_flags_for_compose(monkeypatch) -> None:
    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files: _fake_compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(
            cli, ["save", "--type", "compose", "--no-input", "--name", "demo"]
        )

        assert result.exit_code != 0
        assert "--name" in result.output
        assert "compose" in result.output
        assert not Path("compose.override.yaml").exists()


def test_save_rejects_extend_for_dockerfile() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("Dockerfile").touch()
        result = runner.invoke(
            cli, ["save", "--type", "dockerfile", "--port", "80", "--extend"]
        )

        assert result.exit_code != 0
        assert "--extend" in result.output
        assert "dockerfile" in result.output


def test_save_rejects_a_command_for_compose() -> None:
    result = CliRunner().invoke(
        cli, ["save", "--type", "compose", "--port", "3000", "--", "./server"]
    )

    assert result.exit_code != 0
    assert "--type compose" in result.output


def test_save_explains_the_old_subcommand_spelling() -> None:
    """Muscle memory from the draft CLI: `save compose` is not a command
    called compose, but must not be mistaken for a host command either."""
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["save", "compose"])

        assert result.exit_code != 0
        assert "--type compose" in result.output
        assert "requires --port" not in result.output


def test_save_ambiguity_hint_matches_run(monkeypatch) -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        Path("manage.py").touch()
        result = runner.invoke(cli, ["save", "--no-input"])

        assert result.exit_code != 0
        assert "--type compose or --type django" in result.output


def test_save_project_root_pins_a_compose_project(monkeypatch, tmp_path) -> None:
    """`run --project-root` already accepts a Compose project; `save` must
    write the override into that pinned root rather than reject the flag."""
    seen: list[Path] = []

    def fake_resolve(files, **kwargs):
        seen.append(Path(kwargs["cwd"]))
        return _fake_compose_model()

    monkeypatch.setattr("localghost.cli.resolve_compose", fake_resolve)
    (tmp_path / ".git").mkdir()
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli, ["save", "--no-input", "--project-root", "backend"]
    )

    assert result.exit_code == 0, result.output
    assert (backend / "compose.override.yaml").exists()
    assert seen and seen[0].resolve() == backend.resolve()
