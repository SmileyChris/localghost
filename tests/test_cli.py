from pathlib import Path

from click.testing import CliRunner

from local_dev_proxy.cli import cli


def compose_model(
    *, project: str = "sample-project", ports: tuple[int, ...] = (8000,)
) -> dict:
    return {
        "name": project,
        "networks": {"default": {"name": f"{project}_default"}},
        "services": {
            "worker": {"expose": [9000], "networks": {"default": None}},
            "web": {"expose": list(ports), "networks": {"default": None}},
        },
    }


def install_compose(monkeypatch, model: dict) -> None:
    monkeypatch.setattr("local_dev_proxy.cli.resolve_compose", lambda files: model)


def test_interactive_user_can_choose_a_non_default_service(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    monkeypatch.setattr("local_dev_proxy.cli._is_interactive", lambda _: True)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["generate"], input="worker\n")

        assert result.exit_code == 0, result.output
        assert "web: ports 8000 (likely)" in result.output
        override = Path("compose.override.yaml").read_text(encoding="utf-8")
        assert "worker:" in override
        assert "server.port=9000" in override


def test_interactive_user_is_prompted_for_an_ambiguous_port(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model(ports=(7000, 9000)))
    monkeypatch.setattr("local_dev_proxy.cli._is_interactive", lambda _: True)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["generate"], input="\n9000\n")

        assert result.exit_code == 0, result.output
        assert "Container HTTP port" in result.output
        assert "server.port=9000" in Path("compose.override.yaml").read_text(
            encoding="utf-8"
        )


def test_no_input_requires_a_port_when_it_cannot_choose_safely(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model(ports=(7000, 9000)))
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["generate", "--no-input"])

    assert result.exit_code != 0
    assert "multiple possible ports (7000, 9000)" in result.output
    assert "--port" in result.output


def test_explicit_unknown_service_lists_valid_choices(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(
            cli, ["generate", "--no-input", "--service", "missing"]
        )

    assert result.exit_code != 0
    assert "choose one of: web, worker" in result.output


def test_unsafe_project_name_explains_env_remedy(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model(project="Not DNS Safe"))
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["generate", "--no-input"])

    assert result.exit_code != 0
    assert "set a safe, unique COMPOSE_PROJECT_NAME in .env" in result.output


def test_dry_run_prints_yaml_without_writing(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["generate", "--no-input", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "local-dev-proxy:" in result.output
        assert not Path("compose.override.yaml").exists()


def test_existing_override_requires_confirmation_or_extend(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    monkeypatch.setattr("local_dev_proxy.cli._is_interactive", lambda _: True)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        original = "# existing\nservices: {}\n"
        Path("compose.override.yaml").write_text(original, encoding="utf-8")
        declined = runner.invoke(
            cli, ["generate", "--service", "web"], input="n\n"
        )

        assert declined.exit_code != 0
        assert "refusing to overwrite" in declined.output
        assert Path("compose.override.yaml").read_text(encoding="utf-8") == original

        accepted = runner.invoke(
            cli, ["generate", "--service", "web"], input="y\n"
        )

        assert accepted.exit_code == 0, accepted.output
        assert "Backup:" in accepted.output
        assert Path("compose.override.yaml.bak").exists()


def test_existing_complete_override_reports_no_change(monkeypatch) -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        first_model = compose_model()
        install_compose(monkeypatch, first_model)
        first = runner.invoke(cli, ["generate", "--no-input"])
        assert first.exit_code == 0, first.output

        complete_model = compose_model()
        complete_model["networks"]["local-dev-proxy"] = {"external": True}
        complete_model["services"]["web"]["labels"] = {
            "traefik.enable": "true",
            "traefik.docker.network": "local-dev-proxy",
            "traefik.http.routers.sample-project-web.entrypoints": "web",
            "traefik.http.routers.sample-project-web.rule": (
                "Host(`sample-project.localhost`)"
            ),
            "traefik.http.routers.sample-project-web.service": "sample-project-web",
            "traefik.http.services.sample-project-web.loadbalancer.server.port": "8000",
        }
        install_compose(monkeypatch, complete_model)
        second = runner.invoke(cli, ["generate", "--no-input", "--extend"])

        assert second.exit_code == 0, second.output
        assert "already contains" in second.output
        assert not Path("compose.override.yaml.bak").exists()


def test_no_compose_mode_reads_project_name_from_dotenv() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path(".env").write_text(
            "COMPOSE_PROJECT_NAME='safe-project'\n", encoding="utf-8"
        )
        result = runner.invoke(
            cli,
            ["generate", "--no-input", "--mode", "host", "--port", "3000"],
        )

        assert result.exit_code == 0, result.output
        assert Path("compose.yaml").exists()


def test_no_compose_mode_validates_inputs_and_refuses_overwrite() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        environment = {"COMPOSE_PROJECT_NAME": "safe-project"}
        invalid_service = runner.invoke(
            cli,
            [
                "generate",
                "--no-input",
                "--mode",
                "host",
                "--port",
                "3000",
                "--service",
                "not valid",
            ],
            env=environment,
        )
        assert invalid_service.exit_code != 0
        assert "not a valid service name" in invalid_service.output

        missing_port = runner.invoke(
            cli, ["generate", "--no-input", "--mode", "host"], env=environment
        )
        assert missing_port.exit_code != 0
        assert "requires --port" in missing_port.output

        missing_dockerfile = runner.invoke(
            cli,
            [
                "generate",
                "--no-input",
                "--mode",
                "dockerfile",
                "--port",
                "8000",
            ],
            env=environment,
        )
        assert missing_dockerfile.exit_code != 0
        assert "requires a Dockerfile" in missing_dockerfile.output

        Path("generated.yaml").write_text("keep\n", encoding="utf-8")
        overwrite = runner.invoke(
            cli,
            [
                "generate",
                "--no-input",
                "--mode",
                "host",
                "--port",
                "3000",
                "--output",
                "generated.yaml",
            ],
            env=environment,
        )
        assert overwrite.exit_code != 0
        assert "refusing to overwrite" in overwrite.output
        assert Path("generated.yaml").read_text(encoding="utf-8") == "keep\n"


def test_no_compose_interactive_defaults_to_detected_dockerfile(monkeypatch) -> None:
    monkeypatch.setattr("local_dev_proxy.cli._is_interactive", lambda _: True)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["generate"],
            input="\n8080\n",
            env={"COMPOSE_PROJECT_NAME": "safe-project"},
        )

        assert result.exit_code == 0, result.output
        assert "Application type" in result.output
        assert "Container HTTP port" in result.output
        assert "build: ." in Path("compose.yaml").read_text(encoding="utf-8")
