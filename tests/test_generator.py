from pathlib import Path

from click.testing import CliRunner

from localhost.cli import cli
from localhost.generator import choose_port, rank_services


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


def test_generate_writes_an_override(monkeypatch) -> None:
    monkeypatch.setattr(
        "localhost.cli.resolve_compose", lambda files: compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["generate", "--no-input"])

        assert result.exit_code == 0, result.output
        override = Path("compose.override.yaml").read_text(encoding="utf-8")
        assert "web:" in override
        assert "localhost-proxy:" in override
        assert "${COMPOSE_PROJECT_NAME}-web.rule" in override
        assert "loadbalancer.server.port=8000" in override


def test_existing_override_is_extended_and_backed_up(monkeypatch) -> None:
    monkeypatch.setattr(
        "localhost.cli.resolve_compose", lambda files: compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        Path("compose.override.yaml").write_text(
            "# keep me\nservices:\n  web:\n    environment:\n      DEBUG: '1'\n",
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["generate", "--no-input", "--extend"])

        assert result.exit_code == 0, result.output
        override = Path("compose.override.yaml").read_text(encoding="utf-8")
        assert "# keep me" in override
        assert "DEBUG: '1'" in override
        assert "localhost-proxy" in override
        assert Path("compose.override.yaml.bak").exists()


def test_host_bridge_is_scaffolded_without_compose() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["generate", "--no-input", "--mode", "host", "--port", "3000"],
            env={"COMPOSE_PROJECT_NAME": "sample-project"},
        )

        assert result.exit_code == 0, result.output
        compose = Path("compose.yaml").read_text(encoding="utf-8")
        assert "caddy:2.11.4-alpine" in compose
        assert "http://host.docker.internal:3000" in compose
        assert "host.docker.internal:host-gateway" in compose
        assert "loadbalancer.server.port=8080" in compose


def test_dockerfile_is_scaffolded_without_compose() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["generate", "--no-input", "--port", "8000"],
            env={"COMPOSE_PROJECT_NAME": "sample-project"},
        )

        assert result.exit_code == 0, result.output
        compose = Path("compose.yaml").read_text(encoding="utf-8")
        assert "build: ." in compose
        assert "- '8000'" in compose
        assert "loadbalancer.server.port=8000" in compose
