from pathlib import Path

from click.testing import CliRunner

from localghost.cli import cli
from localghost.generator import choose_port, rank_services


def compose_model() -> dict:
    return {
        "name": "sample-project",
        "networks": {"default": {"name": "sample-project_default"}},
        "services": {
            "worker": {"expose": [9000], "networks": {"default": None}},
            "web": {"expose": [8000], "networks": {"default": None}},
        },
    }


def test_web_service_and_http_port_are_preferred() -> None:
    candidates = rank_services(compose_model(), "sample-project")

    assert [candidate.name for candidate in candidates] == ["web", "worker"]
    assert choose_port(candidates[0], None) == 8000


def test_save_writes_an_override(monkeypatch) -> None:
    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files: compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["save", "--no-input"])

        assert result.exit_code == 0, result.output
        override = Path("compose.override.yaml").read_text(encoding="utf-8")
        assert "web:" in override
        assert "localghost:" in override
        assert "${COMPOSE_PROJECT_NAME}-web.rule" in override
        assert "loadbalancer.server.port=8000" in override


def test_existing_override_is_extended_and_backed_up(monkeypatch) -> None:
    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files: compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        Path("compose.override.yaml").write_text(
            "# keep me\nservices:\n  web:\n    environment:\n      DEBUG: '1'\n",
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["save", "--no-input", "--extend"])

        assert result.exit_code == 0, result.output
        override = Path("compose.override.yaml").read_text(encoding="utf-8")
        assert "# keep me" in override
        assert "DEBUG: '1'" in override
        assert "localghost" in override
        assert Path("compose.override.yaml.bak").exists()


def test_host_run_defaults_are_saved_without_compose(monkeypatch) -> None:
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
        config = Path(".localghost.toml").read_text(encoding="utf-8")
        assert 'type = "php"' in config
        assert "port = 3000" in config


def test_dockerfile_is_scaffolded_without_compose() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["save", "--no-input", "--port", "8000"],
            env={"COMPOSE_PROJECT_NAME": "sample-project"},
        )

        assert result.exit_code == 0, result.output
        compose = Path("compose.yaml").read_text(encoding="utf-8")
        assert "build: ." in compose
        assert "- '8000'" in compose
        assert "loadbalancer.server.port=8000" in compose
