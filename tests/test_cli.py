import json
import os
from pathlib import Path
from subprocess import CompletedProcess

import click
import pytest
from click.testing import CliRunner

from localghost import registry
from localghost.cli import LOCALGHOST_VERSION, cli
from localghost.runner import RunPlan
from localghost.sessions import create
from localghost.sessions import sessions as sessions_list
from localghost.trust import PublicCertificate


def test_version_is_machine_readable() -> None:
    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output == f"{LOCALGHOST_VERSION}\n"


def test_default_command_starts_the_bundled_proxy(monkeypatch) -> None:
    commands = []

    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli.active_routes", lambda: [])

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    runner = CliRunner()

    result = runner.invoke(cli, ["hub", "up"])

    assert result.exit_code == 0, result.output
    assert commands[0] == (
        ["docker", "image", "inspect", f"localghost-traefik:v{LOCALGHOST_VERSION}"],
        {"check": False, "capture_output": True},
    )
    command, kwargs = next(
        entry for entry in commands if entry[0][:2] == ["docker", "compose"]
    )
    assert command[:5] == [
        "docker",
        "compose",
        "--project-name",
        "localghost",
        "--file",
    ]
    bundled = Path(command[5]).read_text(encoding="utf-8")
    assert "context: ." in bundled
    assert "image: localghost-traefik:${LOCALGHOST_IMAGE_TAG}" in bundled
    assert command[6:] == [
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "60",
        "--remove-orphans",
        "--no-build",
    ]
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["env"]["LOCALGHOST_IMAGE_TAG"] == f"v{LOCALGHOST_VERSION}"
    assert "Hub is ready at http://traefik.localhost" in result.output
    assert "Stop the hub: uvx localghost hub down" in result.output
    assert "Save a setup: uvx localghost save" in result.output
    assert "uvx localghost run to run a local app." in " ".join(result.output.split())


def test_default_command_reports_existing_proxy_and_routes(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: True)
    monkeypatch.setattr(
        "localghost.cli.active_routes",
        lambda: [
            type(
                "Route", (), {"hostname": "demo.localhost", "location": "/work/demo"}
            )()
        ],
    )
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 0),
    )

    result = CliRunner().invoke(cli, ["hub", "up"])

    assert result.exit_code == 0, result.output
    assert "Hub is already ready" in result.output
    assert "demo.localhost: /work/demo" in result.output


def test_default_command_warns_when_route_listing_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr(
        "localghost.cli.active_routes",
        lambda: (_ for _ in ()).throw(click.ClickException("inspect failed")),
    )
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 0),
    )

    result = CliRunner().invoke(cli, ["hub", "up"])

    assert result.exit_code == 0, result.output
    assert "inspect failed" in result.output


def test_first_launch_introduces_localghost_before_the_https_prompt(
    monkeypatch,
) -> None:
    events = []
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli.active_routes", lambda: [])
    monkeypatch.setattr("localghost.cli._managed_image_is_available", lambda: False)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)
    monkeypatch.setattr("localghost.cli._is_interactive", lambda no_input: True)
    monkeypatch.setattr("localghost.cli.shutil.which", lambda name: "mkcert")
    monkeypatch.setattr(
        "localghost.cli.title", lambda *, welcome: events.append(("title", welcome))
    )
    monkeypatch.setattr(
        "localghost.cli.click.confirm",
        lambda prompt, default: events.append(("prompt", prompt)) or False,
    )
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)

    result = CliRunner().invoke(cli, ["hub", "up"])

    assert result.exit_code == 0, result.output
    assert events == [
        ("title", True),
        ("prompt", "HTTPS is optional. Enable trusted https://*.localhost URLs now?"),
    ]


def test_first_launch_skips_https_prompt_without_mkcert(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli.active_routes", lambda: [])
    monkeypatch.setattr("localghost.cli._managed_image_is_available", lambda: False)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)
    monkeypatch.setattr("localghost.cli._is_interactive", lambda no_input: True)
    monkeypatch.setattr("localghost.cli.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "localghost.cli.click.confirm",
        lambda *args, **kwargs: pytest.fail("prompted without mkcert"),
    )
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)

    result = CliRunner().invoke(cli, ["hub", "up"])

    assert result.exit_code == 0, result.output
    assert "Hub is ready at http://" in result.output
    assert (
        "Enable HTTPS: uvx localghost trust install after installing mkcert."
        in result.output
    )


def test_status_reports_proxy_state_without_reconciling(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)
    monkeypatch.setattr(
        "localghost.cli._run_proxy", lambda *args, **kwargs: pytest.fail("reconciled")
    )

    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "Hub: stopped" in result.output
    assert "HTTPS configuration: HTTP only" in result.output
    assert "localghost trust status" in result.output


def test_status_lists_remembered_projects(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    registry.record("blog", tmp_path, "django")

    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "blog.localhost" in result.output


def test_status_json_is_machine_readable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    registry.record("blog", tmp_path, "django")

    result = CliRunner().invoke(cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["hub"] == "stopped"
    assert payload["remembered"][0]["hostname"] == "blog.localhost"


def test_status_flag_is_gone() -> None:
    result = CliRunner().invoke(cli, ["--status"])

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_hub_up_starts_the_hub(monkeypatch) -> None:
    started = []
    monkeypatch.setattr(
        "localghost.cli._run_proxy",
        lambda action, **kwargs: started.append(action),
    )
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli._ensure_https_or_warn", lambda: False)
    monkeypatch.setattr("localghost.cli.active_routes", lambda: [])

    result = CliRunner().invoke(cli, ["hub", "up"])

    assert result.exit_code == 0, result.output
    assert started == ["up"]


def test_hub_logs_streams_the_traefik_container(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: (
            calls.append((command, kwargs)) or CompletedProcess(command, 0)
        ),
    )

    result = CliRunner().invoke(cli, ["hub", "logs", "-f", "--tail", "50"])

    assert result.exit_code == 0, result.output
    command, kwargs = calls[0]
    assert command[:4] == ["docker", "compose", "--project-name", "localghost"]
    assert command[-4:] == ["--follow", "--tail", "50", "traefik"]
    assert kwargs["env"]["LOCALGHOST_IMAGE_TAG"] == f"v{LOCALGHOST_VERSION}"


def test_top_level_down_is_gone() -> None:
    result = CliRunner().invoke(cli, ["down"])

    assert result.exit_code != 0
    assert "no such command" in result.output.lower()


def test_bare_hub_reports_without_starting(monkeypatch) -> None:
    started = []
    monkeypatch.setattr(
        "localghost.cli._run_proxy",
        lambda action, **kwargs: started.append(action),
    )
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)

    result = CliRunner().invoke(cli, ["hub"])

    assert result.exit_code == 0, result.output
    assert started == []
    assert "stopped" in result.output


def test_bare_invocation_reports_without_starting(monkeypatch) -> None:
    started = []
    monkeypatch.setattr(
        "localghost.cli._run_proxy",
        lambda action, **kwargs: started.append(action),
    )
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)

    result = CliRunner().invoke(cli)

    assert result.exit_code == 0, result.output
    assert started == []
    assert "stopped" in result.output


def test_down_stops_the_bundled_proxy(monkeypatch) -> None:
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    runner = CliRunner()

    result = runner.invoke(cli, ["hub", "down"])

    assert result.exit_code == 0, result.output
    assert "down" in commands[0][0]
    assert commands[0][0][:4] == ["docker", "compose", "--project-name", "localghost"]
    assert "Hub stopped and removed." in result.output


def test_down_also_removes_the_profiled_bootstrap_container(monkeypatch) -> None:
    """`down` must clear the one-shot bootstrap service too.

    It sits behind `profiles: [bootstrap]`, so a plain `down` leaves its
    exited container behind and Docker still reports the `localghost` project
    as existing — which silently blocks the integration suite's precondition.
    """
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)

    result = CliRunner().invoke(cli, ["hub", "down"])

    assert result.exit_code == 0, result.output
    down = next(item for item in commands if "down" in item)
    assert "--profile" in down and "bootstrap" in down


def test_down_removes_containers_the_current_files_no_longer_describe(
    monkeypatch,
) -> None:
    """`down` must clear services that left the compose file set.

    The tailnet overlay is only passed while saved state names a suffix, so a
    lost or unreadable state file would otherwise leave the gateway running —
    and Docker cannot remove the project network while it stays attached.
    """
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)

    result = CliRunner().invoke(cli, ["hub", "down"])

    assert result.exit_code == 0, result.output
    down = next(item for item in commands if "down" in item)
    assert "--remove-orphans" in down


def test_trust_configures_a_stopped_proxy_without_starting_it(
    monkeypatch, tmp_path
) -> None:
    commands = []
    certificate = PublicCertificate(b"public root", "SHA256:" + "A" * 64)
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli._bootstrap_public_root", lambda: certificate)

    class Installer:
        def install(self, **kwargs):
            return None

    monkeypatch.setattr("localghost.cli.MkcertInstaller", lambda path: Installer())
    monkeypatch.setattr(
        "localghost.cli.ZenNssInstaller", lambda path, **kwargs: Installer()
    )

    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)

    result = CliRunner().invoke(
        cli, ["trust", "install"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "https-enabled").is_file()
    assert "public-root fingerprint: SHA256:" in result.output
    assert commands == []
    assert "Start the hub: localghost hub up" in result.output


def test_trust_restarts_a_running_proxy_when_https_becomes_configured(
    monkeypatch, tmp_path
) -> None:
    commands = []
    certificate = PublicCertificate(b"public root", "SHA256:" + "A" * 64)
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: True)
    monkeypatch.setattr("localghost.cli._bootstrap_public_root", lambda: certificate)

    class Installer:
        def install(self, **kwargs):
            return None

    monkeypatch.setattr("localghost.cli.MkcertInstaller", lambda path: Installer())
    monkeypatch.setattr(
        "localghost.cli.ZenNssInstaller", lambda path, **kwargs: Installer()
    )

    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    result = CliRunner().invoke(
        cli, ["trust", "install"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    compose = next(
        command for command in commands if command[:2] == ["docker", "compose"]
    )
    assert any("proxy_compose_https.yaml" in item for item in compose)
    assert "--force-recreate" not in compose


def test_trust_remove_disables_https_before_mutating_managed_stores(
    monkeypatch, tmp_path
) -> None:
    commands = []
    (tmp_path / "rootCA.pem").write_bytes(b"public root")
    (tmp_path / "https-enabled").touch()
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)

    class Installer:
        def uninstall(self):
            return None

    monkeypatch.setattr("localghost.cli.MkcertInstaller", lambda path: Installer())
    monkeypatch.setattr(
        "localghost.cli.ZenNssInstaller", lambda path, **kwargs: Installer()
    )

    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)

    result = CliRunner().invoke(
        cli, ["trust", "remove"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "https-enabled").exists()
    assert commands == []


def test_proxy_command_preserves_docker_compose_failure_status(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 17, "", "compose failed"),
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["hub", "up"])

    assert result.exit_code == 17
    assert "compose failed" in result.output
    assert "Proxy is running" not in result.output


def test_proxy_commands_report_missing_docker(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)

    def run(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    runner = CliRunner()

    result = runner.invoke(cli, ["hub", "up"])

    assert result.exit_code != 0
    assert "docker is required" in result.output


def test_proxy_port_defaults_when_the_environment_value_is_empty(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli.active_routes", lambda: [])
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 0),
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["hub", "up"], env={"LOCALGHOST_HTTP_PORT": ""})

    assert result.exit_code == 0, result.output
    assert "http://traefik.localhost\n" in result.output


def test_run_reports_the_type_not_the_framework(monkeypatch, tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "manage.py").touch()
    monkeypatch.setattr(
        "localghost.cli.execute", lambda *args, **kwargs: pytest.fail("ran")
    )

    result = CliRunner().invoke(
        cli, ["run", "--dry-run", "--name", "demo", "-C", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "Type: django" in result.output
    assert "Framework:" not in result.output


def test_run_rejects_the_removed_mode_flag(tmp_path) -> None:
    result = CliRunner().invoke(cli, ["run", "--mode", "host", "-C", str(tmp_path)])

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_run_no_longer_accepts_save() -> None:
    result = CliRunner().invoke(cli, ["run", "--save"])

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_run_no_longer_accepts_service() -> None:
    result = CliRunner().invoke(cli, ["run", "--service", "web"])

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_run_accepts_short_port_flag(tmp_path) -> None:
    # COMPOSE_PROJECT_NAME sidesteps deriving the project name from
    # tmp_path's own directory name, which (built from this test's name)
    # contains underscores and isn't DNS-safe -- unrelated to -p itself.
    result = CliRunner().invoke(
        cli,
        ["run", "-C", str(tmp_path), "--dry-run", "-p", "8080", "--", "./server"],
        env={"COMPOSE_PROJECT_NAME": "short-port-flag"},
    )

    assert result.exit_code == 0, result.output
    assert "8080" in result.output


def test_run_rejects_the_removed_framework_alias() -> None:
    result = CliRunner().invoke(cli, ["run", "--framework", "django"])

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_save_rejects_the_removed_mode_flag() -> None:
    runner_ = CliRunner()
    with runner_.isolated_filesystem():
        result = runner_.invoke(cli, ["save", "--mode", "host", "--no-input"])

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_save_takes_the_per_type_default_port() -> None:
    runner_ = CliRunner()
    with runner_.isolated_filesystem():
        Path("manage.py").touch()

        result = runner_.invoke(
            cli,
            ["save", "host", "--no-input", "--dry-run", "--type", "django"],
            # A random isolated_filesystem() directory name can contain an
            # underscore, which fails DNS-safe project-name validation; pin
            # a safe name so this test does not depend on that draw.
            env={"COMPOSE_PROJECT_NAME": "django-default-port"},
        )

        assert result.exit_code == 0, result.output
        assert "8000" in result.output


def test_save_custom_command_does_not_record_an_irrelevant_detected_type() -> None:
    runner_ = CliRunner()
    with runner_.isolated_filesystem():
        Path("manage.py").touch()

        result = runner_.invoke(
            cli,
            [
                "save",
                "host",
                "--no-input",
                "--name",
                "demo",
                "--port",
                "8000",
                "--",
                "./serve",
            ],
        )

        assert result.exit_code == 0, result.output
        assert 'type = "django"' not in Path(".localghost.toml").read_text()


def test_save_accepts_dockerfile_as_a_type() -> None:
    runner_ = CliRunner()
    with runner_.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\n")

        result = runner_.invoke(
            cli,
            ["save", "dockerfile", "--no-input", "--dry-run", "-p", "80"],
            # See test_save_takes_the_per_type_default_port: pin a safe
            # name so a random isolated_filesystem() directory name cannot
            # fail DNS-safe project-name validation.
            env={"COMPOSE_PROJECT_NAME": "dockerfile-type"},
        )

        assert result.exit_code == 0, result.output
        assert "build:" in result.output


def test_save_uses_run_detection_when_dockerfile_and_django_are_present() -> None:
    """Dockerfile scaffolding does not make save diverge from run detection.

    Deliberately invokes bare `save`, not `save host`: this is testing
    bare save's own type-priority (a host/Compose type wins over a
    coexisting Dockerfile), not `save host`'s always-non-dockerfile
    resolution, which would pass here trivially."""
    runner_ = CliRunner()
    with runner_.isolated_filesystem():
        Path(".git").mkdir()
        Path("manage.py").touch()
        Path("Dockerfile").write_text("FROM scratch\n")

        result = runner_.invoke(
            cli,
            ["save", "--no-input", "--dry-run"],
            # --name isn't a bare-save flag any more; pin the project name
            # via the environment instead so this doesn't depend on the
            # isolated_filesystem() directory's random (and possibly
            # DNS-unsafe) name.
            env={"COMPOSE_PROJECT_NAME": "demo-app"},
        )

        assert result.exit_code == 0, result.output
        assert 'type = "django"' in result.output


def test_run_pins_the_root_with_the_flag(monkeypatch, tmp_path) -> None:
    root = tmp_path / "backend"
    root.mkdir()
    (root / "manage.py").touch()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        "localghost.cli.execute", lambda *args, **kwargs: pytest.fail("ran")
    )

    result = CliRunner().invoke(
        cli, ["run", "--dry-run", "--project-root", str(root), "-C", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "backend.localhost" in result.output


def test_run_project_root_discovers_config_inside_the_pinned_root(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("localghost.runner._port_available", lambda port: True)
    root = tmp_path / "backend"
    root.mkdir()
    (root / ".localghost.toml").write_text(
        '[run]\nname = "configured-backend"\nport = 4321\ncommand = ["serve"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["run", "--project-root", "backend", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "configured-backend.localhost" in result.output
    assert "Host port: 4321" in result.output
    assert "serve" in result.output


def test_a_pinned_root_without_a_type_errors(tmp_path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    (tmp_path / ".git").mkdir()

    result = CliRunner().invoke(
        cli, ["run", "--project-root", str(root), "-C", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert "could not detect a project type" in result.output


def test_a_pinned_root_rejects_a_type_not_present_there(monkeypatch, tmp_path) -> None:
    """--project-root must never walk: an ancestor holding the requested
    type must not be silently adopted just because the pin itself doesn't
    match."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "x"}})
    )
    root = tmp_path / "backend"
    root.mkdir()
    (root / "manage.py").touch()
    monkeypatch.setattr(
        "localghost.cli.execute", lambda *args, **kwargs: pytest.fail("ran")
    )

    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--dry-run",
            "--project-root",
            str(root),
            "--type",
            "vite",
            "-C",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert "no vite project at" in result.output
    assert str(root) in result.output
    assert "detected django there" in result.output
    assert "Project root:" not in result.output
    # --project-root was actually used here, so the hint to drop it is
    # accurate.
    assert "drop --project-root" in result.output


def test_a_pinned_root_from_config_rejects_a_mismatched_type(
    monkeypatch, tmp_path
) -> None:
    """The same rejection must hold with no CLI flags at all: [run].root
    and [run].type pinning a directory whose type doesn't match."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "x"}})
    )
    root = tmp_path / "backend"
    root.mkdir()
    (root / "manage.py").touch()
    (tmp_path / ".localghost.toml").write_text(
        '[run]\ntype = "vite"\nroot = "backend"\n'
    )
    monkeypatch.setattr(
        "localghost.cli.execute", lambda *args, **kwargs: pytest.fail("ran")
    )

    result = CliRunner().invoke(cli, ["run", "--dry-run", "-C", str(tmp_path)])

    assert result.exit_code != 0
    assert "no vite project at" in result.output
    assert str(root) in result.output


def test_a_pinned_empty_root_rejects_an_explicit_type(tmp_path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    (tmp_path / ".git").mkdir()

    result = CliRunner().invoke(
        cli,
        ["run", "--project-root", str(root), "--type", "django", "-C", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "no django project at" in result.output
    assert "detected nothing there" in result.output


def test_run_uses_project_root(tmp_path) -> None:
    result = CliRunner().invoke(cli, ["run", "--project-root", str(tmp_path), "--help"])

    assert result.exit_code == 0, result.output
    assert "--project-root" in result.output
    # Click wraps help text, so a naive "--root " substring check could miss
    # a line break landing right after --root; check tokens instead.
    assert not any("--root" in line.split() for line in result.output.splitlines())


def test_run_dry_run_prints_plan_without_starting(monkeypatch) -> None:
    plan = RunPlan("demo", "custom", ("echo", "ok"), 3000, "session", "services: {}\n")
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        "localghost.cli.find_route_collision",
        lambda name: pytest.fail("inspected Docker"),
    )
    monkeypatch.setattr(
        "localghost.cli.execute", lambda *args, **kwargs: pytest.fail("ran")
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["run", "--dry-run", "--port", "3000", "--", "echo"])

    assert result.exit_code == 0, result.output
    assert "Public URL: http://demo.localhost" in result.output
    assert "services: {}" in result.output


def test_run_executes_and_refuses_collision(monkeypatch) -> None:
    plan = RunPlan("demo", "custom", ("echo",), 3000, "session", "services: {}\n")
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)
    monkeypatch.setattr("localghost.cli.execute", lambda *args, **kwargs: 0)
    result = CliRunner().invoke(cli, ["run", "--port", "3000", "--", "echo"])
    assert result.exit_code == 0, result.output
    assert "Starting foreground application" in result.output

    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: "old")
    monkeypatch.setattr(
        "localghost.cli._reclaim_route",
        lambda cid, name: (_ for _ in ()).throw(
            click.ClickException(
                f"{name}.localhost is already claimed by container {cid}; "
                f"remove it with: docker rm -f {cid}"
            )
        ),
    )
    result = CliRunner().invoke(cli, ["run", "--port", "3000", "--", "echo"])
    assert result.exit_code != 0
    assert "docker rm -f old" in result.output


def _mock_docker_inspect(monkeypatch, container_id, exit_code, stdout_json):
    """Helper to mock docker inspect via subprocess.run in _reclaim_route."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[-1] == container_id:
            return CompletedProcess(command, exit_code, stdout_json, "")
        return CompletedProcess(command, 1, "", "unexpected command")

    monkeypatch.setattr("localghost.cli.subprocess.run", fake_run)
    return calls


@pytest.fixture
def cli_module():
    from localghost import cli as cli_mod

    return cli_mod


def test_reclaim_route_auto_removes_stale_bridge(monkeypatch, cli_module):
    container_id = "abc123"
    payload = json.dumps(
        [
            {
                "Config": {
                    "Labels": {
                        "io.localghost.managed": "true",
                        "io.localghost.kind": "host-run-bridge",
                    }
                }
            }
        ]
    )
    calls = _mock_docker_inspect(monkeypatch, container_id, 0, payload)
    cli_module._reclaim_route(container_id, "demo")
    assert len(calls) == 2
    assert calls[0] == ["docker", "inspect", "abc123"]
    assert calls[1] == ["docker", "rm", "-f", "abc123"]


def test_reclaim_route_raises_for_non_managed_container(monkeypatch, cli_module):
    container_id = "abc123"
    payload = json.dumps([{"Config": {"Labels": {"something": "else"}}}])
    _mock_docker_inspect(monkeypatch, container_id, 0, payload)
    with pytest.raises(click.ClickException, match="docker rm -f"):
        cli_module._reclaim_route(container_id, "demo")


def test_reclaim_route_raises_if_rm_fails(monkeypatch, cli_module):
    container_id = "abc123"
    payload = json.dumps(
        [
            {
                "Config": {
                    "Labels": {
                        "io.localghost.managed": "true",
                        "io.localghost.kind": "host-run-bridge",
                    }
                }
            }
        ]
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[-1] == container_id and command[1] == "inspect":
            return CompletedProcess(command, 0, payload, "")
        if command[-1] == container_id and command[1] == "rm":
            return CompletedProcess(command, 1, "", "permission denied")
        return CompletedProcess(command, 1, "", "unexpected")

    monkeypatch.setattr("localghost.cli.subprocess.run", fake_run)
    with pytest.raises(
        click.ClickException, match="failed to remove.*permission denied"
    ):
        cli_module._reclaim_route(container_id, "demo")


def test_reclaim_route_continues_if_container_gone(monkeypatch, cli_module):
    container_id = "abc123"
    _mock_docker_inspect(monkeypatch, container_id, 1, "error: no such container")
    cli_module._reclaim_route(container_id, "demo")  # should not raise


def test_reclaim_route_raises_if_docker_missing(monkeypatch, cli_module):
    def no_docker(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("localghost.cli.subprocess.run", no_docker)
    with pytest.raises(click.ClickException, match="docker is required"):
        cli_module._reclaim_route("abc", "demo")


def test_reclaim_route_raises_on_invalid_inspect_output(monkeypatch, cli_module):
    container_id = "abc123"
    _mock_docker_inspect(monkeypatch, container_id, 0, "not valid json{{")
    with pytest.raises(click.ClickException, match="could not inspect"):
        cli_module._reclaim_route(container_id, "demo")


def test_detect_root_rotation_returns_false_on_oserror(monkeypatch, tmp_path):
    from localghost import cli as cli_mod

    fingerprint = tmp_path / "root-fingerprint"
    fingerprint.write_text("old-fingerprint")

    def fail_read(*args, **kwargs):
        raise OSError("read failure")

    monkeypatch.setattr(
        "localghost.cli._trust_fingerprint_path",
        lambda: fingerprint,
    )
    monkeypatch.setattr(Path, "read_text", fail_read)
    assert cli_mod._detect_root_rotation("new-fingerprint") is False


def test_run_uses_effective_origin_for_django_warning_and_preserves_status(
    monkeypatch,
) -> None:
    plan = RunPlan("demo", "django", ("echo",), 3000, "session", "services: {}\n")
    recorded = {}
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: True)

    def settings_warnings(plan, cwd, *, public_origin):
        recorded["origin"] = public_origin
        return ["Django origin needs updating"]

    monkeypatch.setattr("localghost.cli.django_settings_warnings", settings_warnings)
    monkeypatch.setattr("localghost.cli.execute", lambda *args, **kwargs: 7)

    result = CliRunner().invoke(
        cli,
        ["run", "--port", "3000", "--", "echo"],
        env={"LOCALGHOST_HTTPS_PORT": "8443"},
    )

    assert result.exit_code == 7
    assert recorded["origin"] == "https://demo.localhost:8443"
    assert "Django origin needs updating" in result.output


def test_run_uses_the_requested_application_directory(monkeypatch, tmp_path) -> None:
    recorded = {}
    plan = RunPlan("demo", "custom", ("echo",), 3000, "session", "services: {}\n")

    def build(cwd, *args, **kwargs):
        recorded["build_cwd"] = cwd
        return plan

    def execute(*args, **kwargs):
        recorded["execute_cwd"] = kwargs["cwd"]
        return 0

    monkeypatch.setattr("localghost.cli.build_plan", build)
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)
    monkeypatch.setattr("localghost.cli.execute", execute)

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--port", "3000", "--", "echo"]
    )

    assert result.exit_code == 0, result.output
    assert recorded == {"build_cwd": tmp_path, "execute_cwd": tmp_path}


@pytest.mark.parametrize("value", ["70000", "not-a-port"])
def test_proxy_port_rejects_invalid_environment_values(monkeypatch, value) -> None:
    called = False

    def run(*args, **kwargs):
        nonlocal called
        called = True
        return CompletedProcess([], 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    runner = CliRunner()

    result = runner.invoke(cli, ["hub", "up"], env={"LOCALGHOST_HTTP_PORT": value})

    assert result.exit_code != 0
    assert "integer from 1 to 65535" in result.output
    assert called is False


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
    monkeypatch.setattr("localghost.cli.resolve_compose", lambda files, **kwargs: model)


def routed_compose_model(*, project: str = "demo") -> dict:
    """A Compose model wired to the hub: labeled and on the localghost network."""
    return {
        "name": project,
        "networks": {"localghost": {"external": True}},
        "services": {
            "web": {
                "labels": {"traefik.enable": "true"},
                "networks": {"localghost": None},
            }
        },
    }


def test_interactive_user_can_choose_a_non_default_service(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    monkeypatch.setattr("localghost.cli._is_interactive", lambda _: True)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["save"], input="worker\n")

        assert result.exit_code == 0, result.output
        assert "web: ports 8000 (likely)" in result.output
        override = Path("compose.override.yaml").read_text(encoding="utf-8")
        assert "worker:" in override
        assert "server.port=9000" in override


def test_interactive_user_is_prompted_for_an_ambiguous_port(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model(ports=(7000, 9000)))
    monkeypatch.setattr("localghost.cli._is_interactive", lambda _: True)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["save"], input="\n9000\n")

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
        result = runner.invoke(cli, ["save", "--no-input"])

    assert result.exit_code != 0
    assert "multiple possible ports (7000, 9000)" in result.output
    assert "--port" in result.output


def test_explicit_unknown_service_lists_valid_choices(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(
            cli, ["save", "compose", "--no-input", "--service", "missing"]
        )

    assert result.exit_code != 0
    assert "choose one of: web, worker" in result.output


def test_unsafe_project_name_explains_env_remedy(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model(project="Not DNS Safe"))
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["save", "--no-input"])

    assert result.exit_code != 0
    assert "set a safe, unique COMPOSE_PROJECT_NAME in .env" in result.output


def test_dry_run_prints_yaml_without_writing(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(cli, ["save", "--no-input", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "localghost:" in result.output
        assert not Path("compose.override.yaml").exists()


def test_save_can_select_a_host_type_in_a_compose_project(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        Path("manage.py").touch()
        host = runner.invoke(
            cli, ["save", "host", "--type", "django", "--name", "demo"]
        )
        assert host.exit_code == 0, host.output
        assert 'type = "django"' in Path(".localghost.toml").read_text()


def test_compose_file_environment_selects_compose_mode(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["save", "--no-input", "--dry-run"],
            env={"COMPOSE_FILE": "custom.yaml"},
        )

    assert result.exit_code == 0, result.output
    assert "localghost:" in result.output


def test_new_override_refuses_a_router_owned_by_another_service(monkeypatch) -> None:
    model = compose_model()
    model["services"]["worker"]["labels"] = {
        "traefik.http.routers.sample-project-web.rule": (
            "Host(`sample-project.localhost`)"
        )
    }
    install_compose(monkeypatch, model)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        result = runner.invoke(
            cli, ["save", "compose", "--no-input", "--service", "web"]
        )

        assert result.exit_code != 0
        assert "already defined for service 'worker'" in result.output
        assert not Path("compose.override.yaml").exists()


def test_existing_override_requires_confirmation_or_extend(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    monkeypatch.setattr("localghost.cli._is_interactive", lambda _: True)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        original = "# existing\nservices: {}\n"
        Path("compose.override.yaml").write_text(original, encoding="utf-8")
        declined = runner.invoke(
            cli, ["save", "compose", "--service", "web"], input="n\n"
        )

        assert declined.exit_code != 0
        assert "refusing to overwrite" in declined.output
        assert Path("compose.override.yaml").read_text(encoding="utf-8") == original

        accepted = runner.invoke(
            cli, ["save", "compose", "--service", "web"], input="y\n"
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
        first = runner.invoke(cli, ["save", "--no-input"])
        assert first.exit_code == 0, first.output

        complete_model = compose_model()
        complete_model["networks"]["localghost"] = {"external": True}
        complete_model["services"]["web"]["labels"] = {
            "traefik.enable": "true",
            "traefik.docker.network": "localghost",
            "traefik.http.routers.sample-project-web.entrypoints": "web",
            "traefik.http.routers.sample-project-web.rule": (
                "Host(`sample-project.localhost`)"
            ),
            "traefik.http.routers.sample-project-web.service": "sample-project-web",
            ("traefik.http.routers.sample-project-web-secure.entrypoints"): "websecure",
            "traefik.http.routers.sample-project-web-secure.rule": (
                "Host(`sample-project.localhost`)"
            ),
            (
                "traefik.http.routers.sample-project-web-secure.service"
            ): "sample-project-web",
            "traefik.http.routers.sample-project-web-secure.tls": "true",
            (
                "traefik.http.services.sample-project-web.loadbalancer.server.port"
            ): "8000",
        }
        install_compose(monkeypatch, complete_model)
        second = runner.invoke(cli, ["save", "compose", "--no-input", "--extend"])

        assert second.exit_code == 0, second.output
        assert "already contains" in second.output
        assert not Path("compose.override.yaml.bak").exists()


def test_save_host_defaults_to_toml_with_project_name_from_dotenv(monkeypatch) -> None:
    monkeypatch.setattr("localghost.runner.shutil.which", lambda _: "/usr/bin/php")
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path(".env").write_text(
            "COMPOSE_PROJECT_NAME='safe-project'\n", encoding="utf-8"
        )
        Path("index.php").touch()
        result = runner.invoke(
            cli,
            ["save", "host", "--no-input", "--type", "php", "--port", "3000"],
        )

        assert result.exit_code == 0, result.output
        config = Path(".localghost.toml").read_text()
        assert 'type = "php"' in config
        assert "port = 3000" in config


def test_save_host_defaults_reject_compose_options_and_refuse_overwrite(
    monkeypatch,
) -> None:
    """`save host` declares neither --service nor --output any more (see
    test_save_host_help_omits_compose_options), so what used to be a
    runtime guard ("--service/--output can only be used with Compose") is
    now a parse-time rejection from click itself -- exactly the point of
    giving each subcommand only the options its type accepts."""
    monkeypatch.setattr("localghost.runner.shutil.which", lambda _: "/usr/bin/php")
    runner = CliRunner()

    with runner.isolated_filesystem():
        environment = {"COMPOSE_PROJECT_NAME": "safe-project"}
        Path("index.php").touch()
        invalid_service = runner.invoke(
            cli,
            [
                "save",
                "host",
                "--no-input",
                "--type",
                "php",
                "--port",
                "3000",
                "--service",
                "not valid",
            ],
            env=environment,
        )
        assert invalid_service.exit_code != 0
        assert "no such option" in invalid_service.output.lower()
        assert "--service" in invalid_service.output

        # php (like every non-dockerfile, non-compose type) has a default
        # port, so only dockerfile -- which has none -- can still exercise
        # the "requires --port" guard here.
        Path("Dockerfile").write_text("FROM scratch\n")
        missing_port = runner.invoke(
            cli, ["save", "dockerfile", "--no-input"], env=environment
        )
        assert missing_port.exit_code != 0
        # Exact match, not just a substring: "save dockerfile" is this
        # subcommand's own name, not the removed "--type dockerfile" flag
        # -- a loose "requires --port" substring check would stay green
        # even if the message regressed to naming a flag that no longer
        # exists on `save dockerfile`.
        assert "Error: save dockerfile requires --port" in missing_port.output

        Path("Dockerfile").unlink()
        missing_dockerfile = runner.invoke(
            cli,
            ["save", "dockerfile", "--no-input", "--port", "8000"],
            env=environment,
        )
        assert missing_dockerfile.exit_code != 0
        assert "could not find a dockerfile project root" in (missing_dockerfile.output)

        Path("saved.yaml").write_text("keep\n", encoding="utf-8")
        overwrite = runner.invoke(
            cli,
            [
                "save",
                "host",
                "--no-input",
                "--type",
                "php",
                "--port",
                "3000",
                "--output",
                "saved.yaml",
            ],
            env=environment,
        )
        assert overwrite.exit_code != 0
        assert "no such option" in overwrite.output.lower()
        assert "--output" in overwrite.output
        assert Path("saved.yaml").read_text(encoding="utf-8") == "keep\n"


def test_save_interactively_detects_a_lone_dockerfile(monkeypatch) -> None:
    """save now shares run's detect-first behaviour: an unambiguous
    Dockerfile is picked up without an "Application type" confirmation
    prompt -- only the still-unresolved port is asked for."""
    monkeypatch.setattr("localghost.cli._is_interactive", lambda _: True)
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        result = runner.invoke(
            cli,
            ["save"],
            input="8080\n",
            env={"COMPOSE_PROJECT_NAME": "safe-project"},
        )

        assert result.exit_code == 0, result.output
        assert "Application type" not in result.output
        assert "Container HTTP port" in result.output
        assert "build: ." in Path("compose.yaml").read_text(encoding="utf-8")


def test_save_errors_when_nothing_is_detected(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli._is_interactive", lambda _: True)
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["save"],
            env={"COMPOSE_PROJECT_NAME": "safe-project"},
        )

        assert result.exit_code != 0
        assert "could not detect a project type" in result.output


def test_compose_run_detached_records_a_session(monkeypatch, tmp_path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    install_compose(monkeypatch, routed_compose_model())
    commands = []
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)

    def _run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", _run)

    result = CliRunner().invoke(
        cli,
        ["run", "-C", str(tmp_path), "--type", "compose", "--name", "demo", "--detach"],
    )

    assert result.exit_code == 0, result.output
    assert commands[0][-1] == "--detach"
    assert "Started detached Compose session for demo." in result.output
    recorded = sessions_list()
    assert [(item.mode, item.name) for item in recorded] == [("compose", "demo")]


def test_compose_run_propagates_a_failure(monkeypatch, tmp_path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    install_compose(monkeypatch, routed_compose_model())
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 2),
    )

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--type", "compose", "--name", "demo"]
    )

    assert result.exit_code == 2
    assert sessions_list() == []


def test_compose_run_rejects_host_only_settings(tmp_path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--type", "compose", "--port", "3000"]
    )

    assert result.exit_code != 0
    assert "compose does not accept" in result.output.lower()


def test_compose_run_rejects_a_host_command(tmp_path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--type", "compose", "--", "echo"]
    )

    assert result.exit_code != 0
    assert "cannot be combined with --type compose" in result.output.lower()


def test_compose_run_dry_run_prints_the_plan_and_starts_nothing(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    # docker compose config is read-only, but _run_proxy and a docker
    # compose ... up must never run on a dry run, so those stay pytest.fail.
    install_compose(monkeypatch, routed_compose_model())
    monkeypatch.setattr(
        "localghost.cli._run_proxy",
        lambda *args, **kwargs: pytest.fail("started the proxy"),
    )
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda *args, **kwargs: pytest.fail("ran docker compose"),
    )

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--type", "compose", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "Dry run:" in result.output
    assert "Type: compose" in result.output
    assert f"Project: {tmp_path.name}" in result.output
    assert "Public URL: http://" in result.output


def test_run_dry_run_detects_compose_over_a_coexisting_dockerfile(
    monkeypatch, tmp_path
) -> None:
    """`run`'s detection (`discover_type`'s default `allowed=RUN_TYPES`,
    which excludes "dockerfile") already gives Compose priority over a
    coexisting Dockerfile with no --type needed -- no explicit pin
    required. This locks that in with a test instead of leaving it to
    detection order alone."""
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    install_compose(monkeypatch, routed_compose_model())

    result = CliRunner().invoke(cli, ["run", "-C", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Type: compose" in result.output


def test_unconfigured_compose_run_points_at_save() -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        Path("compose.yaml").write_text(
            "services:\n  web:\n    image: nginx\n", encoding="utf-8"
        )
        result = runner.invoke(cli, ["run"])

        assert result.exit_code != 0
        assert "localghost save" in result.output
        assert "--save" not in result.output


def test_compose_run_refuses_an_unrouted_project(monkeypatch, tmp_path) -> None:
    (tmp_path / "compose.yaml").write_text("services:\n  web:\n    image: nginx\n")
    monkeypatch.setattr(
        "localghost.cli.resolve_compose",
        lambda files, **kwargs: {"networks": {}, "services": {"web": {}}},
    )
    monkeypatch.setattr(
        "localghost.cli._run_proxy",
        lambda *args, **kwargs: pytest.fail("started the proxy"),
    )

    result = CliRunner().invoke(cli, ["run", "-C", str(tmp_path), "--name", "demo"])

    assert result.exit_code != 0
    assert "compose.yaml" in result.output
    assert "localghost save" in result.output
    # The failure names the URL nothing would answer at, but must never
    # present it as a working destination.
    assert "Public URL" not in result.output
    assert "demo.localhost" in result.output


def test_compose_routing_check_never_pins_an_explicit_file(
    monkeypatch, tmp_path
) -> None:
    """Passing --file to `docker compose config` disables Compose's own
    compose.override.yaml merge, so a project `save` just fixed would
    still be refused with the identical error. The check must therefore let
    Compose's own discovery run, against compose_root -- not the process's
    own cwd, for -C/--project-root runs -- rather than pinning an explicit
    file."""
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    recorded: dict[str, object] = {}

    def spy(files, **kwargs):
        recorded["files"] = files
        recorded["cwd"] = kwargs.get("cwd")
        return routed_compose_model()

    monkeypatch.setattr("localghost.cli.resolve_compose", spy)
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 0),
    )

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--type", "compose", "--name", "demo"]
    )

    assert result.exit_code == 0, result.output
    assert recorded["files"] == ()
    assert recorded["cwd"] == tmp_path


def test_compose_run_routes_after_save_fixes_an_override() -> None:
    """End-to-end against real `docker compose config` (no mocking): a run
    refused for missing routing labels must un-refuse once `save` writes
    them, proving the check reads the same merged model `save` writes to
    and `_run_compose` itself would read. --dry-run avoids needing a daemon."""
    runner = CliRunner()
    # A random isolated_filesystem() directory name can contain an
    # underscore; real `docker compose config` accepts that as a project
    # name, but our stricter DNS-safe check does not, so pin a safe name
    # for every invocation instead of leaving it to the directory's draw.
    environment = {"COMPOSE_PROJECT_NAME": "routes-after-save"}
    with runner.isolated_filesystem():
        Path("compose.yaml").write_text(
            "services:\n  web:\n    image: nginx\n    expose:\n      - '80'\n"
        )

        refused = runner.invoke(
            cli, ["run", "--type", "compose", "--dry-run"], env=environment
        )
        assert refused.exit_code != 0, refused.output
        assert "localghost save" in refused.output

        saved = runner.invoke(
            cli, ["save", "compose", "--no-input", "--port", "80"], env=environment
        )
        assert saved.exit_code == 0, saved.output
        assert Path("compose.override.yaml").exists()

        routed = runner.invoke(
            cli, ["run", "--type", "compose", "--dry-run"], env=environment
        )
        assert routed.exit_code == 0, routed.output
        assert "Public URL:" in routed.output


def test_a_configured_compose_type_still_validates_routing(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / ".localghost.toml").write_text('[run]\ntype = "compose"\n')
    monkeypatch.setattr(
        "localghost.cli.resolve_compose",
        lambda files, **kwargs: {"networks": {}, "services": {"web": {}}},
    )

    result = CliRunner().invoke(cli, ["run", "-C", str(tmp_path), "--name", "demo"])

    assert result.exit_code != 0
    assert "localghost save" in result.output


def test_a_pinned_root_with_only_a_configured_name_still_detects_compose(
    monkeypatch, tmp_path
) -> None:
    """A .localghost.toml that sets only `name` still pins the root (through
    config_dir), but that must not skip compose detection at that same root.
    Before the fix, a pinned root with no explicit --type never called
    discover_type at all, so selected_type stayed None, fell through to
    build_plan, and build_plan's pinned branch cannot build a compose plan
    (compose is a Compose-owned dispatch, not a host type)."""
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / ".localghost.toml").write_text('[run]\nname = "demo"\n')
    install_compose(monkeypatch, routed_compose_model())
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)

    result = CliRunner().invoke(cli, ["run", "-C", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Type: compose" in result.output
    assert "Project: demo" in result.output
    assert "demo.localhost" in result.output


def test_a_root_flag_pinned_compose_project_is_still_detected(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "compose.yaml").write_text("services: {}\n")
    install_compose(monkeypatch, routed_compose_model(project="app"))
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)

    result = CliRunner().invoke(cli, ["run", "--project-root", str(root), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Type: compose" in result.output


def test_compose_run_from_a_subdirectory_uses_the_project_root(
    monkeypatch, tmp_path
) -> None:
    """`docker compose config` performs its own upward discovery from
    compose_root, so it can find a parent's wired project even when the
    printed URL and project name were wrongly derived from cwd instead. This
    proves --project-name, the printed URL, and the real `docker compose
    up`'s cwd= all come from the SAME resolved root, not the subdirectory --
    do not pass --name here, or the project-name derivation bug would be
    masked."""
    root = tmp_path / "site"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "compose.yaml").write_text("services: {}\n")
    nested = root / "services" / "api"
    nested.mkdir(parents=True)
    install_compose(monkeypatch, routed_compose_model(project="site"))
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    recorded: dict[str, object] = {}

    def _run(command, **kwargs):
        recorded["command"] = command
        recorded["cwd"] = kwargs.get("cwd")
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", _run)

    result = CliRunner().invoke(cli, ["run", "-C", str(nested)])

    assert result.exit_code == 0, result.output
    assert recorded["command"] == [
        "docker",
        "compose",
        "--project-name",
        "site",
        "up",
    ]
    assert recorded["cwd"] == root
    assert "site.localhost" in result.output


def test_compose_run_with_explicit_type_from_a_subdirectory_uses_the_project_root(
    monkeypatch, tmp_path
) -> None:
    """An explicit --type compose must resolve the project root exactly like
    the unpinned, no --type path does: walking up from a subdirectory to the
    parent that actually carries compose.yaml. Before the fix, the root
    resolution block at the top of `run` only ran when `selected_type is
    None`, so `--type compose` from a subdirectory left `resolved_root`
    unset, `compose_root` fell back to `cwd` (the subdirectory), and the
    real `docker compose up` would start a second, wrongly-named stack from
    the subdirectory's own (nonexistent or wrong) compose file -- hijacking
    the parent's routed hostname. Recording both cwd= and --project-name of
    the real invocation catches a divergence between them that asserting
    only the printed output would miss."""
    root = tmp_path / "site"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "compose.yaml").write_text("services: {}\n")
    nested = root / "services" / "api"
    nested.mkdir(parents=True)
    install_compose(monkeypatch, routed_compose_model(project="site"))
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    recorded: dict[str, object] = {}

    def _run(command, **kwargs):
        recorded["command"] = command
        recorded["cwd"] = kwargs.get("cwd")
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", _run)

    result = CliRunner().invoke(cli, ["run", "-C", str(nested), "--type", "compose"])

    assert result.exit_code == 0, result.output
    assert recorded["command"] == [
        "docker",
        "compose",
        "--project-name",
        "site",
        "up",
    ]
    assert recorded["cwd"] == root
    assert "site.localhost" in result.output


def test_pinned_root_from_discovered_config_does_not_claim_a_root_flag(
    tmp_path,
) -> None:
    """The pin here comes from a discovered .localghost.toml, not
    --project-root, so the mismatch error must not tell the user to drop a
    flag they never used."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".localghost.toml").write_text("[run]\n")
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "x"}})
    )

    result = CliRunner().invoke(cli, ["run", "--type", "django", "-C", str(tmp_path)])

    assert result.exit_code != 0
    assert "no django project at" in result.output
    assert "--project-root" not in result.output


def test_save_command_requires_a_port() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["save", "host", "--no-input", "--", "./server"])

    assert result.exit_code != 0
    assert "a custom command requires --port" in result.output


def test_save_dockerfile_takes_no_trailing_command() -> None:
    """`save dockerfile` declares no positional command argument at all, so
    a command passed to it is rejected by click itself as an unexpected
    extra argument -- the old "a command cannot be combined with --type
    dockerfile" guard in `_resolve_application` is simply never reached
    from this subcommand. This is an arity assertion, not a --type one:
    the invocation carries no --type at all."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["save", "dockerfile", "--no-input", "--port", "3000", "--", "./server"],
        )

    assert result.exit_code != 0
    assert "unexpected extra argument" in result.output.lower()


def test_save_compose_surfaces_a_resolution_failure(monkeypatch) -> None:
    """`save compose` reports a missing Compose model through
    `resolve_compose` (`docker compose config`) itself. Its upward search
    for the project root is deliberately best-effort -- a directory where
    `discover_type` finds no compose file falls back to the invocation
    directory -- so what the user sees is Compose's own diagnosis, not
    "could not find a compose project root". ("compose" is a subcommand
    now, not a --type value -- renamed from
    test_save_offers_compose_as_a_type_but_requires_a_compose_file.)

    `resolve_compose` is stubbed to raise directly, matching the other
    tests in this file (see `install_compose`), rather than letting a real
    `docker compose config` run: without docker installed, the actual
    failure text is "docker is required and was not found" instead of a
    resolution error, which would make this test depend on a docker binary
    being present -- `uv run pytest` (a Global Constraint) must pass
    without one.
    """

    def _no_compose_file(files: object, **kwargs: object) -> None:
        raise click.ClickException("no configuration file provided: not found")

    monkeypatch.setattr("localghost.cli.resolve_compose", _no_compose_file)
    runner_ = CliRunner()
    with runner_.isolated_filesystem():
        result = runner_.invoke(cli, ["save", "compose", "--no-input", "--port", "80"])

    assert result.exit_code != 0
    assert "no configuration file provided" in result.output


def test_save_compose_takes_no_trailing_command() -> None:
    """`save compose` declares no positional command argument either, so
    this is the same parse-time arity rejection as
    test_save_dockerfile_takes_no_trailing_command, not the old
    `_resolve_application` guard."""
    result = CliRunner().invoke(
        cli,
        ["save", "compose", "--port", "3000", "--", "./server"],
    )

    assert result.exit_code != 0
    assert "unexpected extra argument" in result.output.lower()


def test_save_detects_a_dockerfile_from_a_subdirectory_and_writes_at_the_root(
    tmp_path, monkeypatch
) -> None:
    """save's upward detection can resolve a type from an ancestor
    directory; every check and default downstream of that detection must
    use the same root, not silently fall back to the invocation directory.
    Before the fix, the Dockerfile existence check stayed cwd-only, so a
    Dockerfile detected two levels up produced "requires a Dockerfile in the
    current directory" -- naming a flag (--type dockerfile) the user never
    passed, about a file that manifestly exists.

    Invokes `save dockerfile` directly rather than bare `save`: bare
    `save` has no `--port`, and the interactive prompt that would supply
    one can't be exercised under CliRunner (stdin never reports as a tty).
    `_save_dockerfile_project`'s own upward `discover_type` fallback is the
    same root-resolution machinery bare save's dispatch would use, so this
    still exercises the property under test."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    nested = root / "services" / "api"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = CliRunner().invoke(
        cli,
        ["save", "dockerfile", "--no-input", "--port", "80"],
        env={"COMPOSE_PROJECT_NAME": "root-detected-dockerfile"},
    )

    assert result.exit_code == 0, result.output
    assert (root / "compose.yaml").exists()
    assert not (nested / "compose.yaml").exists()
    assert "build:" in (root / "compose.yaml").read_text(encoding="utf-8")


def test_run_rejects_a_missing_config_path(tmp_path) -> None:
    result = CliRunner().invoke(
        cli,
        ["run", "-C", str(tmp_path), "--config", str(tmp_path / "missing.toml")],
    )

    assert result.exit_code != 0
    assert "does not exist" in result.output.lower()


def test_save_command_writes_and_then_extends_the_config() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        created = runner.invoke(
            cli,
            [
                "save",
                "host",
                "--no-input",
                "--name",
                "demo",
                "--port",
                "3000",
                "--",
                "./server",
            ],
        )
        assert created.exit_code == 0, created.output
        assert "Created .localghost.toml." in created.output

        updated = runner.invoke(
            cli,
            [
                "save",
                "host",
                "--no-input",
                "--extend",
                "--name",
                "demo",
                "--port",
                "4000",
                "--",
                "./server",
            ],
        )

        assert updated.exit_code == 0, updated.output
        assert "Updated .localghost.toml." in updated.output
        assert "Backup: .localghost.toml.bak" in updated.output
        assert "port = 4000" in Path(".localghost.toml").read_text(encoding="utf-8")


def test_save_command_refuses_to_overwrite_without_extend() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".localghost.toml").write_text("[run]\nport = 1\n")

        result = runner.invoke(
            cli,
            [
                "save",
                "host",
                "--no-input",
                "--name",
                "demo",
                "--port",
                "3000",
                "--",
                "./server",
            ],
        )

        assert result.exit_code != 0
        assert "--extend" in result.output


def test_save_command_dry_run_prints_without_writing() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            [
                "save",
                "host",
                "--no-input",
                "--dry-run",
                "--name",
                "demo",
                "--port",
                "3000",
                "--",
                "./server",
            ],
        )

        assert result.exit_code == 0, result.output
        assert not Path(".localghost.toml").exists()
        assert "[run]" in result.output
        assert "port = 3000" in result.output


def test_save_host_run_persists_a_custom_command_before_running(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / ".git").mkdir()
    executed = []
    monkeypatch.setattr(
        "localghost.cli.execute",
        lambda plan, *args, **kwargs: executed.append(plan) or 0,
    )

    result = CliRunner().invoke(
        cli,
        [
            "save",
            "host",
            "-C",
            str(tmp_path),
            "--run",
            "--name",
            "custom-save",
            "--port",
            "34567",
            "--",
            "./server",
        ],
    )

    assert result.exit_code == 0, result.output
    assert executed
    config = (tmp_path / ".localghost.toml").read_text(encoding="utf-8")
    assert "port = 34567" in config
    assert 'command = ["./server"]' in config


def test_save_compose_run_writes_compose_integration_and_starts(
    monkeypatch, tmp_path
) -> None:
    """`save compose --run` saves and then invokes the full `run` command,
    which -- unlike the old `run --save` -- always re-validates routing
    (see test_compose_run_refuses_an_unrouted_project). The mocked model
    must therefore already look routed, the way a real `docker compose
    config` would once the override this test just wrote is on disk."""
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    model = compose_model()
    model["networks"]["localghost"] = {"external": True}
    model["services"]["web"]["labels"] = {"traefik.enable": "true"}
    model["services"]["web"]["networks"]["localghost"] = None
    install_compose(monkeypatch, model)
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    commands = []
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: (
            commands.append(command) or CompletedProcess(command, 0)
        ),
    )

    result = CliRunner().invoke(
        cli,
        [
            "save",
            "compose",
            "-C",
            str(tmp_path),
            "--run",
            "--service",
            "web",
            "--port",
            "8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "compose.override.yaml").exists()
    assert any(command[:2] == ["docker", "compose"] for command in commands)


def test_save_and_save_host_run_write_the_same_host_configuration(
    monkeypatch, tmp_path
) -> None:
    save_root = tmp_path / "save-only"
    run_root = tmp_path / "save-and-run"
    for root in (save_root, run_root):
        root.mkdir()
        (root / ".git").mkdir()
        (root / "manage.py").touch()

    runner = CliRunner()
    monkeypatch.chdir(save_root)
    saved = runner.invoke(
        cli,
        ["save", "host", "--type", "django", "--name", "demo", "--port", "34567"],
    )
    assert saved.exit_code == 0, saved.output

    monkeypatch.chdir(run_root)
    monkeypatch.setattr("localghost.cli.execute", lambda *args, **kwargs: 0)
    run = runner.invoke(
        cli,
        [
            "save",
            "host",
            "--run",
            "--type",
            "django",
            "--name",
            "demo",
            "--port",
            "34567",
        ],
    )
    assert run.exit_code == 0, run.output

    assert (save_root / ".localghost.toml").read_bytes() == (
        run_root / ".localghost.toml"
    ).read_bytes()


def test_compose_run_starts_the_proxy_and_reports_the_public_url(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    install_compose(monkeypatch, routed_compose_model())
    calls: list[object] = []
    real_kwargs: dict[str, object] = {}
    monkeypatch.setattr(
        "localghost.cli._run_proxy", lambda *args, **kwargs: calls.append("proxy")
    )

    def _run(command, **kwargs):
        calls.append(command)
        real_kwargs.update(kwargs)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", _run)

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--type", "compose", "--name", "demo"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0] == "proxy", "the proxy must be up before the application starts"
    assert calls[1] == ["docker", "compose", "--project-name", "demo", "up"]
    # The real docker compose up's cwd= must match compose_root; a spy that
    # discards kwargs (as this one used to) can't catch a regression that
    # derives the project name/URL from one directory and the real
    # invocation's cwd from another.
    assert real_kwargs.get("cwd") == tmp_path
    assert "http://demo.localhost" in result.output


def test_run_matches_an_existing_session_started_from_the_project_root(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "site"
    nested = root / "accounts"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "manage.py").touch()
    plan = RunPlan(
        "demo",
        "django",
        ("run",),
        3000,
        "session",
        "services: {}\n",
        project_root=root,
        working_directory=root,
    )
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        "localghost.cli._run_proxy",
        lambda *args, **kwargs: pytest.fail("started the proxy"),
    )
    monkeypatch.setattr(
        "localghost.cli.execute", lambda *args, **kwargs: pytest.fail("ran")
    )
    create(
        mode="host",
        name="demo",
        port=3000,
        cwd=root,
        command=("run",),
        log=tmp_path / "demo.log",
        pid=os.getpid(),
    )

    result = CliRunner().invoke(cli, ["run", "-C", str(nested)])

    assert result.exit_code == 0, result.output
    assert "is already running" in result.output


def test_detached_run_does_not_print_the_foreground_banner(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "manage.py").touch()
    plan = RunPlan(
        "demo",
        "django",
        ("run",),
        3000,
        "session",
        "services: {}\n",
        project_root=tmp_path,
        working_directory=tmp_path,
    )
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)
    monkeypatch.setattr("localghost.cli._detach_host", lambda *args: None)

    result = CliRunner().invoke(cli, ["run", "--detach", "-C", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Starting foreground application" not in result.output
    assert "Ctrl+C" not in result.output


def test_detached_host_runs_in_the_planned_working_directory(
    monkeypatch, tmp_path, cli_module
):
    root = tmp_path / "app"
    docroot = root / "webroot"
    docroot.mkdir(parents=True)
    plan = RunPlan(
        "demo",
        "cakephp",
        ("php", "-S", "127.0.0.1:3000"),
        3000,
        "session",
        "services: {}\n",
        project_root=root,
        working_directory=docroot,
    )
    recorded: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "_run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "start_bridge", lambda selected: None)
    monkeypatch.setattr(
        cli_module, "_session_log_path", lambda name: tmp_path / f"{name}.log"
    )

    class _Child:
        pid = 4321

    def _popen(command, **kwargs):
        recorded["popen_cwd"] = kwargs["cwd"]
        return _Child()

    monkeypatch.setattr(cli_module.subprocess, "Popen", _popen)
    monkeypatch.setattr(
        cli_module,
        "create_session",
        lambda **kwargs: recorded.update(session_cwd=kwargs["cwd"]),
    )

    cli_module._detach_host(plan, tmp_path)

    assert recorded["popen_cwd"] == docroot
    assert recorded["session_cwd"] == root


def test_detached_start_failure_removes_bridge(monkeypatch, tmp_path, cli_module):
    plan = RunPlan("demo", "custom", ("missing",), 3000, "session", "services: {}\n")
    calls = []
    monkeypatch.setattr(cli_module, "_run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli_module, "start_bridge", lambda selected: calls.append(("start", selected))
    )
    monkeypatch.setattr(
        cli_module, "stop_bridge", lambda selected: calls.append(("stop", selected))
    )
    monkeypatch.setattr(
        cli_module, "_session_log_path", lambda name: tmp_path / f"{name}.log"
    )
    monkeypatch.setattr(
        cli_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("missing")),
    )

    with pytest.raises(click.ClickException, match="could not start detached"):
        cli_module._detach_host(plan, tmp_path)

    assert calls == [("start", plan), ("stop", plan)]


def test_compose_run_honours_compose_project_name_from_dotenv(
    monkeypatch, tmp_path
) -> None:
    """`.env` must win over the directory name, as it does for `save`.

    Otherwise `localghost run --type compose` and a plain `docker compose up`
    build two different projects from the same directory.
    """
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME=custom-name\n")
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "localghost.cli.resolve_compose",
        lambda files, **kwargs: {
            "networks": {"localghost": {}},
            "services": {
                "web": {
                    "labels": {"traefik.enable": "true"},
                    "networks": {"localghost": None},
                }
            },
        },
    )

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--type", "compose", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "custom-name" in result.output
    assert tmp_path.name not in result.output


def test_compose_run_honours_compose_project_name_from_the_environment(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "from-env")
    commands = []
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "localghost.cli.resolve_compose",
        lambda files, **kwargs: {
            "networks": {"localghost": {}},
            "services": {
                "web": {
                    "labels": {"traefik.enable": "true"},
                    "networks": {"localghost": None},
                }
            },
        },
    )

    def _run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", _run)

    result = CliRunner().invoke(cli, ["run", "-C", str(tmp_path), "--type", "compose"])

    assert result.exit_code == 0, result.output
    up = next(item for item in commands if item[-1] == "up")
    assert up[:4] == ["docker", "compose", "--project-name", "from-env"]


def test_run_passes_the_public_origin_to_the_status_bar(monkeypatch) -> None:
    plan = RunPlan("demo", "custom", ("echo",), 3000, "session", "services: {}\n")
    recorded = {}
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)

    def fake_execute(plan, start_proxy, **kwargs):
        recorded.update(kwargs)
        return 0

    monkeypatch.setattr("localghost.cli.execute", fake_execute)

    result = CliRunner().invoke(cli, ["run", "--port", "3000", "--", "echo"])

    assert result.exit_code == 0
    assert recorded["public_origin"] == "http://demo.localhost"
    assert recorded["status_bar"] is True


def test_run_honours_no_status_bar(monkeypatch) -> None:
    plan = RunPlan("demo", "custom", ("echo",), 3000, "session", "services: {}\n")
    recorded = {}
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)

    def fake_execute(plan, start_proxy, **kwargs):
        recorded.update(kwargs)
        return 0

    monkeypatch.setattr("localghost.cli.execute", fake_execute)

    result = CliRunner().invoke(
        cli, ["run", "--no-status-bar", "--port", "3000", "--", "echo"]
    )

    assert result.exit_code == 0
    assert recorded["status_bar"] is False


def test_run_leaves_a_persistent_nonzero_exit_diagnosis(monkeypatch) -> None:
    plan = RunPlan("demo", "custom", ("false",), 3000, "session", "services: {}\n")
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)
    monkeypatch.setattr("localghost.cli.execute", lambda *args, **kwargs: 7)

    result = CliRunner().invoke(cli, ["run", "--port", "3000", "--", "false"])

    assert result.exit_code == 7
    assert "exited with status 7" in result.output
    assert "--no-status-bar" in result.output


def test_compose_run_pins_before_the_hub_is_reconciled(monkeypatch, tmp_path) -> None:
    order = []

    import contextlib as _contextlib

    @_contextlib.contextmanager
    def fake_pinned(
        url,
        *,
        secondary_url=None,
        stream=None,
        enabled=True,
        probe=None,
        message="starting",
    ):
        order.append(f"pin:{message}")

        class Bar:
            def status(self, text):
                order.append(f"status:{text}")

        try:
            yield Bar()
        finally:
            order.append("release")

    monkeypatch.setattr("localghost.cli.statusbar.pinned", fake_pinned)
    monkeypatch.setattr(
        "localghost.cli._run_proxy", lambda *args, **kwargs: order.append("proxy")
    )
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)
    monkeypatch.setattr("localghost.cli._check_compose_routing", lambda *a: None)

    def fake_subprocess_run(command, **kwargs):
        order.append("compose-up")
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", fake_subprocess_run)
    (tmp_path / "compose.yaml").write_text("services: {}\n")

    result = CliRunner().invoke(cli, ["run", "-C", str(tmp_path), "--type", "compose"])

    assert result.exit_code == 0, result.output
    assert order[0] == "pin:starting hub"
    assert order.index("pin:starting hub") < order.index("proxy")
    assert order.index("status:starting") < order.index("compose-up")
    assert order.index("release") > order.index("compose-up")


def test_status_json_includes_routes_and_route_errors(monkeypatch) -> None:
    import click

    from localghost.routes import Route

    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: True)
    monkeypatch.setattr(
        "localghost.cli.active_routes",
        lambda: [Route(hostname="blog.localhost", location="/tmp/blog")],
    )
    result = CliRunner().invoke(cli, ["status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["routes"][0]["hostname"] == "blog.localhost"

    monkeypatch.setattr(
        "localghost.cli.active_routes",
        lambda: (_ for _ in ()).throw(click.ClickException("inspect failed")),
    )
    result = CliRunner().invoke(cli, ["status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["routes_error"] == "inspect failed"


def test_save_subcommand_hint_names_each_project_kind() -> None:
    from localghost.cli import _save_subcommand_hint

    assert _save_subcommand_hint("compose") == "`localghost save compose`"
    assert _save_subcommand_hint("dockerfile") == "`localghost save dockerfile`"
    assert _save_subcommand_hint("vite") == "`localghost save host --type vite`"
