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
    # --name is gone entirely: _save_compose_project derives router names
    # from the resolved Compose model and never sees it, so
    # `save compose --name foo` would write routers for the Compose
    # project's own name while registering a ghost page for foo.localhost.
    assert "--name" not in result.output


def test_bare_save_detects_compose_from_a_subdirectory_and_writes_at_the_root(
    tmp_path, monkeypatch
) -> None:
    """Mirrors test_save_detects_a_dockerfile_from_a_subdirectory_and_
    writes_at_the_root in tests/test_cli.py, for the Compose path:
    `_save_compose_project` never searches upward on its own (unlike
    save_host/save_dockerfile's own discover_type fallbacks), so the root
    bare `save`'s dispatch detects must be threaded through explicitly to
    `save_compose` or the override lands next to the invocation directory
    instead of next to compose.yaml."""
    monkeypatch.setattr(
        "localghost.cli.resolve_compose",
        lambda files, **kwargs: _fake_compose_model(),
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    nested = root / "services" / "api"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = CliRunner().invoke(
        cli,
        ["save", "--no-input"],
        env={"COMPOSE_PROJECT_NAME": "nested-compose-project"},
    )

    assert result.exit_code == 0, result.output
    assert (root / "compose.override.yaml").exists()
    assert not (nested / "compose.override.yaml").exists()


def test_save_compose_from_a_subdirectory_writes_at_the_compose_root(
    tmp_path, monkeypatch
) -> None:
    """The named subcommand has to resolve the root the way bare `save`
    does. Writing the override next to the invocation directory puts it
    where Compose never merges it, and the follow-up hint then names
    `localghost save` -- the command that just appeared to succeed."""
    monkeypatch.setattr(
        "localghost.cli.resolve_compose",
        lambda files, **kwargs: _fake_compose_model(),
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    nested = root / "services" / "api"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = CliRunner().invoke(
        cli,
        ["save", "compose", "--no-input"],
        env={"COMPOSE_PROJECT_NAME": "nested-compose-project"},
    )

    assert result.exit_code == 0, result.output
    assert (root / "compose.override.yaml").exists()
    assert not (nested / "compose.override.yaml").exists()
    # The type pin and the registry entry are keyed off the same resolved
    # root, so the remembered hostname matches the routers just written.
    assert (root / ".localghost.toml").exists()
    assert not (nested / ".localghost.toml").exists()


def test_save_compose_with_an_explicit_file_stack_stays_where_it_was_invoked(
    tmp_path, monkeypatch
) -> None:
    """`-f/--file` names the model to inspect, so the output directory
    stays the invocation directory -- its documented behaviour, and the
    reason the upward search above is gated on `files` being empty."""
    monkeypatch.setattr(
        "localghost.cli.resolve_compose",
        lambda files, **kwargs: _fake_compose_model(),
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    nested = root / "services" / "api"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = CliRunner().invoke(
        cli,
        ["save", "compose", "--no-input", "--file", str(root / "compose.yaml")],
        env={"COMPOSE_PROJECT_NAME": "explicit-stack-project"},
    )

    assert result.exit_code == 0, result.output
    assert (nested / "compose.override.yaml").exists()
    assert not (root / "compose.override.yaml").exists()
    # No pin either: an explicit stack is inspected from wherever the
    # command was typed, which is not necessarily a project root.
    assert not (nested / ".localghost.toml").exists()


def test_save_compose_pins_the_type_so_later_runs_need_no_flag(monkeypatch) -> None:
    """`compose.yaml` beside `manage.py` is genuinely ambiguous and
    `discover_type` refuses to guess. `save host --type django` pins the
    host side of that fork; this pin is the only way to pin the Compose
    side, and without it every later `run` needs `--type compose` by
    hand."""
    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files: _fake_compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        Path("manage.py").touch()
        result = runner.invoke(cli, ["save", "compose", "--no-input"])

        assert result.exit_code == 0, result.output
        assert 'type = "compose"' in Path(".localghost.toml").read_text(
            encoding="utf-8"
        )


def test_bare_save_does_not_pin_the_compose_type(monkeypatch) -> None:
    """Bare `save` only reaches the compose branch when detection was
    already unambiguous, so there is nothing to remember -- and writing a
    pin no one asked for would make a plain `save` touch a second file."""
    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files, **kwargs: _fake_compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["save", "--no-input"])

        assert result.exit_code == 0, result.output
        assert Path("compose.override.yaml").exists()
        assert not Path(".localghost.toml").exists()


def test_save_compose_run_rejects_a_nonstandard_output(monkeypatch) -> None:
    """Compose only merges `compose.override.yaml` automatically, so
    `--output` and `--run` can never both hold: the pair could only save
    successfully and then fail the routing check. It is rejected at parse
    time instead, before anything is written."""
    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files, **kwargs: _fake_compose_model()
    )
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(
            cli, ["save", "compose", "--no-input", "--output", "custom.yaml", "--run"]
        )

        assert result.exit_code != 0
        assert "--output cannot be combined with --run" in result.output
        assert not Path("custom.yaml").exists()


def test_save_compose_run_starts_an_ambiguous_compose_project(
    tmp_path, monkeypatch
) -> None:
    """`run` re-resolves from scratch, so without the subcommand's own type
    being forwarded this saved the override and then died on
    "both compose and django were detected; rerun with --type compose" --
    advice that cannot be followed from `save compose --run`, where the
    subcommand name already is the type."""
    started: list[str | None] = []
    monkeypatch.setattr(
        "localghost.cli.resolve_compose", lambda files, **kwargs: _fake_compose_model()
    )
    monkeypatch.setattr("localghost.cli._check_compose_routing", lambda *a, **k: None)
    monkeypatch.setattr(
        "localghost.cli._run_compose",
        lambda root, name, detach, **kwargs: started.append(name),
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "manage.py").touch()
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["save", "compose", "--no-input", "--run"],
        env={"COMPOSE_PROJECT_NAME": "ambiguous-project"},
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "compose.override.yaml").exists()
    assert started, "save compose --run must start the application after saving"


def test_bare_save_rejects_type_specific_flags() -> None:
    result = CliRunner().invoke(cli, ["save", "--service", "web"])

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()
