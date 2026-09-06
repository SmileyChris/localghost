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


def test_save_compose_run_starts_the_application(monkeypatch) -> None:
    started: list[str | None] = []

    def fake_run_compose(root: Path, name: str | None, detach: bool, **kwargs) -> None:
        started.append(name)

    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files, **kwargs: compose_model()
    )
    monkeypatch.setattr("localghost.cli._check_compose_routing", lambda *a, **k: None)
    monkeypatch.setattr("localghost.cli._run_compose", fake_run_compose)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["save", "compose", "--no-input", "--run"],
            env={"COMPOSE_PROJECT_NAME": "sample-project"},
        )

        assert result.exit_code == 0, result.output
        assert Path("compose.override.yaml").exists()
        assert started, "save compose --run must start the application after saving"


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


def test_save_host_run_is_a_noop_with_dry_run(monkeypatch) -> None:
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
            [
                "save", "host", "--no-input", "--dry-run", "--run",
                "--port", "8080", "--", "./server",
            ],
            env={"COMPOSE_PROJECT_NAME": "sample-host"},
        )

        assert result.exit_code == 0, result.output
        assert not Path(".localghost.toml").exists()
        assert not started, "save --dry-run --run must not start the application"


def test_save_host_run_forwards_the_project_root(monkeypatch, tmp_path) -> None:
    """`--run` re-enters `run`, which re-resolves from its own flags. With
    only `-C` forwarded, `run` searched upward from the invocation
    directory, never saw the `.localghost.toml` just written under
    `--project-root`, and exited 1 on the setup that had just succeeded."""
    started: list[str] = []
    monkeypatch.setattr(
        "localghost.cli.execute",
        lambda plan, *args, **kwargs: started.append(plan.name) or 0,
    )
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    (tmp_path / ".git").mkdir()
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "manage.py").touch()
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["save", "host", "--no-input", "--project-root", "backend", "--run"],
        env={"COMPOSE_PROJECT_NAME": "backend-project"},
    )

    assert result.exit_code == 0, result.output
    assert (backend / ".localghost.toml").exists()
    assert started, "save host --project-root --run must start the application"


def test_save_host_run_forwards_a_custom_configuration_path(
    monkeypatch, tmp_path
) -> None:
    """Same failure through `--config`: `run` discovers `.localghost.toml`,
    never the custom path just written, so the command and port saved a
    line earlier were invisible to it."""
    started: list[str] = []
    monkeypatch.setattr(
        "localghost.cli.execute",
        lambda plan, *args, **kwargs: started.append(plan.name) or 0,
    )
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    (tmp_path / ".git").mkdir()
    (tmp_path / "custom.toml").write_text("[run]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "save", "host", "--no-input", "--config", "custom.toml", "--extend",
            "--run", "--port", "8080", "--", "./server",
        ],
        env={"COMPOSE_PROJECT_NAME": "custom-config-project"},
    )

    assert result.exit_code == 0, result.output
    assert 'command = ["./server"]' in (tmp_path / "custom.toml").read_text(
        encoding="utf-8"
    )
    assert not (tmp_path / ".localghost.toml").exists()
    assert started, "save host --config --run must start the application"


def test_save_host_run_prints_the_wordmark_once(monkeypatch, tmp_path) -> None:
    """`save host --run` prints the title and then re-enters `run`, which
    prints one of its own. The wordmark is a per-invocation brand mark, so
    `feedback.title` shows it at most once per process instead of three
    subcommands each carrying a suppression flag."""
    printed: list[str] = []

    class Recorder:
        def print(self, item: object = "", **kwargs: object) -> None:
            printed.append(str(item))

    monkeypatch.setattr("localghost.feedback._rich_terminal", lambda err: True)
    monkeypatch.setattr("localghost.feedback._console", lambda err: Recorder())
    monkeypatch.setattr("localghost.cli.execute", lambda *args, **kwargs: 0)
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    (tmp_path / ".git").mkdir()
    (tmp_path / "manage.py").touch()
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["save", "host", "--no-input", "--run"],
        env={"COMPOSE_PROJECT_NAME": "wordmark-project"},
    )

    assert result.exit_code == 0, result.output
    assert printed.count("localghost") == 1


def test_save_dockerfile_no_input_never_prompts_for_a_port(monkeypatch) -> None:
    """`--no-input` is declared on `save dockerfile`, so it has to reach the
    port prompt. It was read out of the group's context but never passed
    on, and `_save_dockerfile_project` asked `_is_interactive(False)`
    regardless -- a declared option that did nothing."""
    monkeypatch.setattr("localghost.cli._is_interactive", lambda no_input: not no_input)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["save", "dockerfile", "--no-input"],
            env={"COMPOSE_PROJECT_NAME": "dockerfile-fixture"},
        )

        assert result.exit_code != 0
        assert "Container HTTP port" not in result.output
        assert "requires --port" in result.output


def test_bare_save_no_input_reaches_the_dockerfile_branch(monkeypatch) -> None:
    """Bare `save` stores `--no-input` in its context for the subcommand to
    pick up; the dockerfile branch dropped it on the floor."""
    monkeypatch.setattr("localghost.cli._is_interactive", lambda no_input: not no_input)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["save", "--no-input"],
            env={"COMPOSE_PROJECT_NAME": "dockerfile-fixture"},
        )

        assert result.exit_code != 0
        assert "Container HTTP port" not in result.output
        assert "requires --port" in result.output


def test_bare_save_help_lists_only_type_neutral_options() -> None:
    result = CliRunner().invoke(cli, ["save", "--help"])

    assert result.exit_code == 0, result.output
    for option in ("--directory", "--dry-run", "--no-input", "--run"):
        assert option in result.output
    # Everything type-specific lives on the subcommand that accepts it --
    # the whole point of the split, and what makes `--help` truthful.
    for option in (
        "--type",
        "--file",
        "--service",
        "--output",
        "--name",
        "--config",
        "--project-root",
        "--port",
        "--extend",
    ):
        assert option not in result.output
    for subcommand in ("host", "compose", "dockerfile"):
        assert subcommand in result.output


def test_save_dockerfile_help_lists_only_dockerfile_options() -> None:
    result = CliRunner().invoke(cli, ["save", "dockerfile", "--help"])

    assert result.exit_code == 0, result.output
    for option in ("--project-root", "--service", "--port", "--output", "--no-input"):
        assert option in result.output
    # A Dockerfile scaffold writes a new compose.yaml rather than editing
    # one, and never touches .localghost.toml.
    for option in ("--file", "--config", "--name", "--extend"):
        assert option not in result.output


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


def test_save_dockerfile_run_starts_the_application(monkeypatch) -> None:
    started: list[str | None] = []

    def fake_run_compose(root: Path, name: str | None, detach: bool, **kwargs) -> None:
        started.append(name)

    monkeypatch.setattr("localghost.cli._check_compose_routing", lambda *a, **k: None)
    monkeypatch.setattr("localghost.cli._run_compose", fake_run_compose)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\nEXPOSE 8000\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["save", "dockerfile", "--no-input", "--run", "--port", "8000"],
            env={"COMPOSE_PROJECT_NAME": "dockerfile-fixture"},
        )

        assert result.exit_code == 0, result.output
        assert Path("compose.yaml").exists()
        assert started, "save dockerfile --run must start the application after saving"


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


def test_bare_save_run_starts_the_application(monkeypatch) -> None:
    """Bare `save` has no `--type`; it dispatches to `save_host` via
    `ctx.invoke(save_host)`, which never forwards `--run` explicitly. This
    only works because the group stores `run_after` in `ctx.obj` and the
    subcommand falls back to it -- the same mechanism `dry_run`/`no_input`
    already rely on."""
    started: list[str] = []
    monkeypatch.setattr(
        "localghost.cli.execute",
        lambda plan, *args, **kwargs: started.append(plan.name) or 0,
    )
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr("localghost.runner.shutil.which", lambda _: "/usr/bin/php")
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("index.php").touch()
        result = runner.invoke(
            cli,
            ["save", "--no-input", "--run"],
            env={"COMPOSE_PROJECT_NAME": "sample-project"},
        )

        assert result.exit_code == 0, result.output
        assert Path(".localghost.toml").exists()
        assert started, "bare save --run must start the application after saving"


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
