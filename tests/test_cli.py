from pathlib import Path
from subprocess import CompletedProcess

import pytest
from click.testing import CliRunner

from localghost.cli import cli
from localghost.runner import RunPlan
from localghost.trust import PublicCertificate


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
        ["docker", "image", "inspect", "localghost-traefik:v3.7.7"],
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
    assert "image: localghost-traefik:v3.7.7" in bundled
    assert command[6:] == ["up", "--detach", "--wait", "--wait-timeout", "60"]
    assert kwargs == {"check": False, "capture_output": True, "text": True}
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


def test_first_launch_introduces_localghost_before_the_https_prompt(
    monkeypatch,
) -> None:
    events = []
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli.active_routes", lambda: [])
    monkeypatch.setattr("localghost.cli._managed_image_is_available", lambda: False)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)
    monkeypatch.setattr("localghost.cli._is_interactive", lambda no_input: True)
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
        def install(self):
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
        def install(self):
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
    assert "--force-recreate" in commands[0]


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
        lambda command, **kwargs: CompletedProcess(command, 17),
    )
    runner = CliRunner()

    result = runner.invoke(cli)

    assert result.exit_code == 17
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
    result = CliRunner().invoke(cli, ["run", "--port", "3000", "--", "echo"])
    assert result.exit_code != 0
    assert "docker rm -f old" in result.output


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
