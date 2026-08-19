import json
import os
from pathlib import Path
from subprocess import CompletedProcess

import click
import pytest
from click.testing import CliRunner

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

    result = runner.invoke(cli)

    assert result.exit_code == 0, result.output
    assert commands[0] == (
        ["docker", "image", "inspect", f"localghost-traefik:v{LOCALGHOST_VERSION}"],
        {"check": False, "capture_output": True},
    )
    command, kwargs = commands[1]
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
    assert command[6:] == ["up", "--detach", "--wait", "--wait-timeout", "60"]
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["env"]["LOCALGHOST_IMAGE_TAG"] == f"v{LOCALGHOST_VERSION}"
    assert "Shared proxy is ready at http://traefik.localhost" in result.output
    assert "Stop the proxy: uvx localghost down" in result.output
    assert "Add a route: uvx localghost generate for Docker Compose" in result.output
    assert "uvx localghost run for a local app." in " ".join(result.output.split())


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

    result = CliRunner().invoke(cli)

    assert result.exit_code == 0, result.output
    assert "Shared proxy is already ready" in result.output
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

    result = CliRunner().invoke(cli)

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

    result = CliRunner().invoke(cli)

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

    result = CliRunner().invoke(cli)

    assert result.exit_code == 0, result.output
    assert "Shared proxy is ready at http://" in result.output
    assert (
        "Enable HTTPS: uvx localghost trust after installing mkcert."
        in result.output
    )


def test_status_reports_proxy_state_without_reconciling(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr(
        "localghost.cli._https_configured", lambda: False
    )
    monkeypatch.setattr(
        "localghost.cli._run_proxy", lambda *args, **kwargs: pytest.fail("reconciled")
    )

    result = CliRunner().invoke(cli, ["--status"])

    assert result.exit_code == 0, result.output
    assert "Proxy: stopped" in result.output
    assert "HTTPS configuration: HTTP only" in result.output
    assert "localghost trust --status" in result.output


def test_status_cannot_be_combined_with_a_subcommand() -> None:
    result = CliRunner().invoke(cli, ["--status", "down"])

    assert result.exit_code != 0
    assert "cannot be combined" in result.output


def test_down_stops_the_bundled_proxy(monkeypatch) -> None:
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    runner = CliRunner()

    result = runner.invoke(cli, ["down"])

    assert result.exit_code == 0, result.output
    assert commands[0][0][6:] == ["down"]
    assert "Proxy stopped and removed." in result.output


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
    monkeypatch.setattr("localghost.cli.ZenNssInstaller", lambda path: Installer())
    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)

    result = CliRunner().invoke(
        cli, ["trust"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "https-enabled").is_file()
    assert "public-root fingerprint: SHA256:" in result.output
    assert commands == []
    assert "Start the proxy: localghost" in result.output


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
    monkeypatch.setattr("localghost.cli.ZenNssInstaller", lambda path: Installer())

    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    result = CliRunner().invoke(
        cli, ["trust"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert any("proxy_compose_https.yaml" in item for item in commands[0])
    assert "--force-recreate" not in commands[0]


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
    monkeypatch.setattr("localghost.cli.ZenNssInstaller", lambda path: Installer())
    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", run)

    result = CliRunner().invoke(
        cli, ["trust", "--remove"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "https-enabled").exists()
    assert commands == []


def test_proxy_command_preserves_docker_compose_failure_status(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: CompletedProcess(
            command, 17, "", "compose failed"
        ),
    )
    runner = CliRunner()

    result = runner.invoke(cli)

    assert result.exit_code == 17
    assert "compose failed" in result.output
    assert "Proxy is running" not in result.output


def test_proxy_commands_report_missing_docker(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)

    def run(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    runner = CliRunner()

    result = runner.invoke(cli)

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

    result = runner.invoke(cli, env={"LOCALGHOST_HTTP_PORT": ""})

    assert result.exit_code == 0, result.output
    assert "http://traefik.localhost\n" in result.output


def test_run_dry_run_prints_plan_without_starting(monkeypatch) -> None:
    plan = RunPlan("demo", "custom", ("echo", "ok"), 3000, "session", "services: {}\n")
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args: plan)
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
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args: plan)
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
    payload = json.dumps([{
        "Config": {
            "Labels": {
                "io.localghost.managed": "true",
                "io.localghost.kind": "host-run-bridge",
            }
        }
    }])
    calls = _mock_docker_inspect(monkeypatch, container_id, 0, payload)
    cli_module._reclaim_route(container_id, "demo")
    assert len(calls) == 2
    assert calls[0] == ["docker", "inspect", "abc123"]
    assert calls[1] == ["docker", "rm", "-f", "abc123"]


def test_reclaim_route_raises_for_non_managed_container(monkeypatch, cli_module):
    container_id = "abc123"
    payload = json.dumps([{
        "Config": {
            "Labels": {"something": "else"}
        }
    }])
    _mock_docker_inspect(monkeypatch, container_id, 0, payload)
    with pytest.raises(click.ClickException, match="docker rm -f"):
        cli_module._reclaim_route(container_id, "demo")


def test_reclaim_route_raises_if_rm_fails(monkeypatch, cli_module):
    container_id = "abc123"
    payload = json.dumps([{
        "Config": {
            "Labels": {
                "io.localghost.managed": "true",
                "io.localghost.kind": "host-run-bridge",
            }
        }
    }])
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
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args: plan)
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: True)

    def settings_warnings(plan, cwd, *, public_origin):
        recorded["origin"] = public_origin
        return ["Django origin needs updating"]

    monkeypatch.setattr(
        "localghost.cli.django_settings_warnings", settings_warnings
    )
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

    def build(cwd, *args):
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

    result = runner.invoke(cli, env={"LOCALGHOST_HTTP_PORT": value})

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
    monkeypatch.setattr("localghost.cli.resolve_compose", lambda files: model)


def test_interactive_user_can_choose_a_non_default_service(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    monkeypatch.setattr("localghost.cli._is_interactive", lambda _: True)
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
    monkeypatch.setattr("localghost.cli._is_interactive", lambda _: True)
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
        result = runner.invoke(cli, ["generate", "--no-input", "--service", "missing"])

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
        assert "localghost:" in result.output
        assert not Path("compose.override.yaml").exists()


def test_generate_rejects_options_for_the_wrong_project_mode(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    runner = CliRunner()

    with runner.isolated_filesystem():
        no_compose = runner.invoke(cli, ["generate", "--extend"])
        assert no_compose.exit_code != 0
        assert "--extend requires an existing Compose project" in no_compose.output

        Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")
        compose = runner.invoke(cli, ["generate", "--mode", "host"])
        assert compose.exit_code != 0
        assert "--mode can only be used" in compose.output


def test_compose_file_environment_selects_compose_mode(monkeypatch) -> None:
    install_compose(monkeypatch, compose_model())
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["generate", "--no-input", "--dry-run"],
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
        result = runner.invoke(cli, ["generate", "--no-input", "--service", "web"])

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
        declined = runner.invoke(cli, ["generate", "--service", "web"], input="n\n")

        assert declined.exit_code != 0
        assert "refusing to overwrite" in declined.output
        assert Path("compose.override.yaml").read_text(encoding="utf-8") == original

        accepted = runner.invoke(cli, ["generate", "--service", "web"], input="y\n")

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
        complete_model["networks"]["localghost"] = {"external": True}
        complete_model["services"]["web"]["labels"] = {
            "traefik.enable": "true",
            "traefik.docker.network": "localghost",
            "traefik.http.routers.sample-project-web.entrypoints": "web",
            "traefik.http.routers.sample-project-web.rule": (
                "Host(`sample-project.localhost`)"
            ),
                "traefik.http.routers.sample-project-web.service": "sample-project-web",
                (
                    "traefik.http.routers.sample-project-web-secure.entrypoints"
                ): "websecure",
                "traefik.http.routers.sample-project-web-secure.rule": (
                    "Host(`sample-project.localhost`)"
                ),
                (
                    "traefik.http.routers.sample-project-web-secure.service"
                ): "sample-project-web",
                "traefik.http.routers.sample-project-web-secure.tls": "true",
                (
                    "traefik.http.services.sample-project-web."
                    "loadbalancer.server.port"
                ): "8000",
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
    monkeypatch.setattr("localghost.cli._is_interactive", lambda _: True)
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


def test_compose_run_detached_records_a_session(monkeypatch, tmp_path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    commands = []
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)

    def _run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", _run)

    result = CliRunner().invoke(
        cli,
        ["run", "-C", str(tmp_path), "--mode", "compose", "--name", "demo", "--detach"],
    )

    assert result.exit_code == 0, result.output
    assert commands[0][-1] == "--detach"
    assert "Started detached Compose session for demo." in result.output
    recorded = sessions_list()
    assert [(item.mode, item.name) for item in recorded] == [("compose", "demo")]


def test_compose_run_propagates_a_failure(monkeypatch, tmp_path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 2),
    )

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--mode", "compose", "--name", "demo"]
    )

    assert result.exit_code == 2
    assert sessions_list() == []


def test_compose_run_rejects_host_only_settings(tmp_path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--mode", "compose", "--port", "3000"]
    )

    assert result.exit_code != 0
    assert "Compose mode does not accept" in result.output


def test_generate_command_requires_a_port() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["generate", "--no-input", "--", "./server"])

    assert result.exit_code != 0
    assert "a custom command requires --port" in result.output


def test_generate_command_rejects_dockerfile_mode() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            [
                "generate",
                "--no-input",
                "--mode",
                "dockerfile",
                "--port",
                "3000",
                "--",
                "./server",
            ],
        )

    assert result.exit_code != 0
    assert "cannot be combined with --mode dockerfile" in result.output


def test_generate_command_writes_and_then_extends_the_config() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        created = runner.invoke(
            cli, ["generate", "--no-input", "--port", "3000", "--", "./server"]
        )
        assert created.exit_code == 0, created.output
        assert "Created .localghost.toml." in created.output

        updated = runner.invoke(
            cli,
            [
                "generate",
                "--no-input",
                "--extend",
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


def test_generate_command_refuses_to_overwrite_without_extend() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".localghost.toml").write_text("[run]\nport = 1\n")

        result = runner.invoke(
            cli, ["generate", "--no-input", "--port", "3000", "--", "./server"]
        )

        assert result.exit_code != 0
        assert "--extend" in result.output


def test_generate_command_dry_run_prints_without_writing() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["generate", "--no-input", "--dry-run", "--port", "3000", "--", "./server"],
        )

        assert result.exit_code == 0, result.output
        assert not Path(".localghost.toml").exists()
        assert "[run]" in result.output
        assert "port = 3000" in result.output


def test_compose_run_starts_the_proxy_and_reports_the_public_url(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    calls: list[object] = []
    monkeypatch.setattr(
        "localghost.cli._run_proxy", lambda *args, **kwargs: calls.append("proxy")
    )

    def _run(command, **kwargs):
        calls.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setattr("localghost.cli.subprocess.run", _run)

    result = CliRunner().invoke(
        cli, ["run", "-C", str(tmp_path), "--mode", "compose", "--name", "demo"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0] == "proxy", "the proxy must be up before the application starts"
    assert calls[1] == ["docker", "compose", "--project-name", "demo", "up"]
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
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args: plan)
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
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args: plan)
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
    plan = RunPlan(
        "demo", "custom", ("missing",), 3000, "session", "services: {}\n"
    )
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
