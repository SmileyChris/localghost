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
        result = runner.invoke(cli, ["save", "compose", "--no-input", "--extend"])

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
            ["save", "host", "--no-input", "--type", "php", "--port", "3000"],
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
            ["save", "dockerfile", "--no-input", "--port", "8000"],
            env={"COMPOSE_PROJECT_NAME": "sample-project"},
        )

        assert result.exit_code == 0, result.output
        compose = Path("compose.yaml").read_text(encoding="utf-8")
        assert "build: ." in compose
        assert "- '8000'" in compose
        assert "loadbalancer.server.port=8000" in compose


def test_save_host_writes_run_config() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["save", "host", "--no-input", "--port", "8080", "--", "./server"],
            # A random isolated_filesystem() directory name can contain an
            # underscore, which fails DNS-safe project-name validation; pin
            # a safe name so this test does not depend on that draw.
            env={"COMPOSE_PROJECT_NAME": "sample-host"},
        )

        assert result.exit_code == 0, result.output
        config = Path(".localghost.toml").read_text(encoding="utf-8")
        assert "./server" in config
        assert "8080" in config


def test_save_host_run_starts_the_application(monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(
        "localghost.cli.execute",
        lambda plan, *args, **kwargs: started.append(plan.name) or 0,
    )
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["save", "host", "--no-input", "--run", "--port", "8080", "--", "./server"],
            env={"COMPOSE_PROJECT_NAME": "sample-host"},
        )

        assert result.exit_code == 0, result.output
        assert Path(".localghost.toml").exists()
        assert started, "save --run must start the application after saving"


def test_save_host_help_omits_compose_options() -> None:
    result = CliRunner().invoke(cli, ["save", "host", "--help"])

    assert result.exit_code == 0, result.output
    assert "--config" in result.output
    assert "--file" not in result.output
    assert "--service" not in result.output
    assert "--output" not in result.output


def test_save_host_rejects_compose_as_a_type() -> None:
    result = CliRunner().invoke(cli, ["save", "host", "--type", "compose"])

    assert result.exit_code != 0
    assert "invalid value" in result.output.lower()
    assert "compose" in result.output.lower()


def test_save_host_refuses_a_detected_compose_project() -> None:
    """`_resolve_application`'s own detection (`discover_type(cwd, None)`,
    default `allowed=RUN_TYPES`) includes "compose" -- HOST_TYPES only
    blocks an explicit `--type compose` (see
    test_save_host_rejects_compose_as_a_type above), not detection walking
    straight past it. Without this guard, `save host` -- documented as
    writing .localghost.toml -- would silently write a Compose override
    instead."""
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(
            cli, ["save", "host", "--no-input"], env={"COMPOSE_PROJECT_NAME": "demo"}
        )

        assert result.exit_code != 0
        assert "save compose" in result.output
        assert not Path("compose.override.yaml").exists()
        assert not Path(".localghost.toml").exists()


def test_save_dockerfile_subcommand_writes_compose() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\nEXPOSE 8000\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["save", "dockerfile", "--no-input", "--port", "8000"],
            # A random isolated_filesystem() directory name can contain an
            # underscore, which fails DNS-safe project-name validation; pin
            # a safe name so this test does not depend on that draw.
            env={"COMPOSE_PROJECT_NAME": "dockerfile-fixture"},
        )

        assert result.exit_code == 0, result.output
        assert Path("compose.yaml").exists()


def test_bare_save_dispatches_to_dockerfile(monkeypatch) -> None:
    # Bare `save` carries no `--port` (type-neutral flags only), and
    # `_save_dockerfile_project` requires one when it can't prompt — so a
    # silent, fully non-interactive `--no-input` save of a Dockerfile-only
    # project has no route to a port and cannot succeed here (there is no
    # Dockerfile EXPOSE-parsing fallback anywhere in the codebase). This
    # mirrors test_cli.py's pre-existing
    # test_save_interactively_detects_a_lone_dockerfile: simulate an
    # interactive session so the dispatch reaches `save_dockerfile` and it
    # can prompt for the port it needs.
    monkeypatch.setattr("localghost.cli._is_interactive", lambda _: True)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\nEXPOSE 8000\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["save"],
            input="8000\n",
            env={"COMPOSE_PROJECT_NAME": "dockerfile-fixture"},
        )

        assert result.exit_code == 0, result.output
        assert Path("compose.yaml").exists()


def test_bare_save_reports_the_detected_type_when_nothing_matches() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["save", "--no-input"])

        assert result.exit_code != 0
        assert "could not detect" in result.output.lower()


def test_bare_save_ambiguity_names_the_save_host_subcommand(monkeypatch) -> None:
    """The two-phase RUN_TYPES-then-SAVE_TYPES retry only exists for the
    "nothing detected at all" case; an ambiguity between two host types
    (found on the first, RUN_TYPES-scoped pass) must not be retried with
    dockerfile allowed too, which would discard this message for a worse
    one that also names a type neither `save host --type` nor bare `save`
    can act on directly. The message must also say `save host --type`, not
    bare `save`'s own (nonexistent) `--type`."""
    monkeypatch.setattr("localghost.runner.shutil.which", lambda _: "/usr/bin/php")
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("manage.py").touch()
        Path("index.php").touch()
        result = runner.invoke(cli, ["save", "--no-input"])

        assert result.exit_code != 0
        assert "both django and php were detected" in result.output
        assert "save host --type django" in result.output
        assert "save host --type php" in result.output
        assert "dockerfile" not in result.output.lower()
