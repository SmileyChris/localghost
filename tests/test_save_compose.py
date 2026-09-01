"""Validate saved Compose files through Docker Compose's resolved model."""

import json
import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from localghost.cli import cli

ROOT = Path(__file__).resolve().parents[1]
GENERATOR_FIXTURE = ROOT / "tests" / "fixtures" / "generator" / "compose.yaml"


def compose_model(*paths: Path, project_name: str) -> dict:
    command = ["docker", "compose"]
    for path in paths:
        command.extend(["--file", str(path)])
    command.extend(["config", "--format", "json"])
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "COMPOSE_PROJECT_NAME": project_name},
    )
    return json.loads(result.stdout)


def test_saved_compose_files_resolve_correctly(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    override = tmp_path / "compose.override.yaml"
    result = runner.invoke(
        cli,
        [
            "save",
            "compose",
            "--no-input",
            "--file",
            str(GENERATOR_FIXTURE),
            "--output",
            str(override),
        ],
        env={"COMPOSE_PROJECT_NAME": "generator-fixture"},
    )

    assert result.exit_code == 0, result.output
    model = compose_model(
        GENERATOR_FIXTURE, override, project_name="generator-fixture"
    )
    web = model["services"]["web"]
    assert set(web["networks"]) == {"application", "localghost"}
    assert web["labels"]["traefik.enable"] == "true"
    assert web["labels"]["traefik.docker.network"] == "localghost"
    assert web["labels"]["traefik.http.routers.generator-fixture-web.rule"] == (
        "Host(`generator-fixture.localhost`)"
    )
    assert web["labels"][
        "traefik.http.services.generator-fixture-web.loadbalancer.server.port"
    ] == "8000"
    assert model["networks"]["localghost"]["external"] is True
    assert "localghost" not in model["services"]["worker"]["networks"]

    dockerfile_dir = tmp_path / "dockerfile-app"
    dockerfile_dir.mkdir()
    (dockerfile_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.chdir(dockerfile_dir)
    result = runner.invoke(
        cli,
        ["save", "dockerfile", "--no-input", "--port", "80"],
        env={"COMPOSE_PROJECT_NAME": "dockerfile-fixture"},
    )
    assert result.exit_code == 0, result.output

    dockerfile_model = compose_model(
        dockerfile_dir / "compose.yaml", project_name="dockerfile-fixture"
    )
    app = dockerfile_model["services"]["app"]
    assert app["build"]["context"].endswith("dockerfile-app")
    assert app["expose"] == ["80"]
    assert set(app["networks"]) == {"default", "localghost"}
    assert app["labels"]["traefik.http.routers.dockerfile-fixture-app.rule"] == (
        "Host(`dockerfile-fixture.localhost`)"
    )
    assert app["labels"][
        "traefik.http.services.dockerfile-fixture-app.loadbalancer.server.port"
    ] == "80"

    extended_override = tmp_path / "existing.override.yaml"
    fixture_override = (
        ROOT / "tests" / "fixtures" / "generator" / "compose.override.yaml"
    )
    extended_override.write_bytes(fixture_override.read_bytes())
    result = runner.invoke(
        cli,
        [
            "save",
            "compose",
            "--no-input",
            "--extend",
            "--file",
            str(GENERATOR_FIXTURE),
            "--output",
            str(extended_override),
        ],
        env={"COMPOSE_PROJECT_NAME": "generator-fixture"},
    )
    assert result.exit_code == 0, result.output
    assert extended_override.with_suffix(".yaml.bak").is_file()
    assert "Existing local settings must survive" in extended_override.read_text()
    compose_model(
        GENERATOR_FIXTURE, extended_override, project_name="generator-fixture"
    )

    result = runner.invoke(
        cli,
        [
            "save",
            "compose",
            "--no-input",
            "--file",
            str(GENERATOR_FIXTURE),
            "--output",
            str(override),
        ],
        env={"COMPOSE_PROJECT_NAME": "generator-fixture"},
    )
    assert result.exit_code != 0
    assert "refusing to overwrite" in result.output


def _fake_compose_model() -> dict:
    return {
        "name": "sample-project",
        "networks": {"default": {"name": "sample-project_default"}},
        "services": {
            "worker": {"expose": [9000], "networks": {"default": None}},
            "web": {"expose": [8000], "networks": {"default": None}},
        },
    }


def test_save_compose_subcommand_writes_an_override(monkeypatch) -> None:
    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files: _fake_compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["save", "compose", "--no-input"])

        assert result.exit_code == 0, result.output
        override = Path("compose.override.yaml").read_text(encoding="utf-8")
        assert "web:" in override
        assert "loadbalancer.server.port=8000" in override


def test_save_compose_help_lists_only_compose_options() -> None:
    result = CliRunner().invoke(cli, ["save", "compose", "--help"])

    assert result.exit_code == 0, result.output
    assert "--file" in result.output
    assert "--service" in result.output
    assert "--output" in result.output
    # Host-only options must not appear on the compose subcommand.
    assert "--config" not in result.output
    assert "--project-root" not in result.output


def test_bare_save_rejects_type_specific_flags() -> None:
    result = CliRunner().invoke(cli, ["save", "--service", "web"])

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()
