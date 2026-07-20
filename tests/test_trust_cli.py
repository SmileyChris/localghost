from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from subprocess import CompletedProcess

import click
import pytest
from click.testing import CliRunner

import localghost.cli as cli_module
from localghost.cli import cli
from localghost.trust import PublicCertificate, TrustError

CERTIFICATE_PEM = b"""-----BEGIN CERTIFICATE-----
MAA=
-----END CERTIFICATE-----
"""


def test_trust_status_reports_an_absent_root_without_starting_proxy(tmp_path) -> None:
    result = CliRunner().invoke(
        cli, ["trust", "--status"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert "HTTPS: disabled" in result.output


def test_trust_rejects_combined_remove_and_status() -> None:
    result = CliRunner().invoke(cli, ["trust", "--remove", "--status"])

    assert result.exit_code != 0
    assert "cannot be used together" in result.output


def test_trust_status_reports_valid_enabled_root(tmp_path) -> None:
    (tmp_path / "rootCA.pem").write_bytes(CERTIFICATE_PEM)
    (tmp_path / "https-enabled").touch()

    result = CliRunner().invoke(
        cli, ["trust", "--status"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert "HTTPS: enabled" in result.output
    assert PublicCertificate.parse(CERTIFICATE_PEM).fingerprint in result.output
    assert "Managed stores: system,nss" in result.output


def test_trust_status_rejects_an_invalid_root(tmp_path) -> None:
    (tmp_path / "rootCA.pem").write_text("not a certificate", encoding="utf-8")

    result = CliRunner().invoke(
        cli, ["trust", "--status"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code != 0
    assert "invalid local public root" in result.output


def test_default_command_uses_configured_https_and_custom_port(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "rootCA.pem").write_bytes(CERTIFICATE_PEM)
    (tmp_path / "https-enabled").touch()
    commands = []
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli.active_routes", lambda: [])

    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    result = CliRunner().invoke(
        cli,
        env={
            "LOCALGHOST_STATE_DIR": str(tmp_path),
            "LOCALGHOST_HTTPS_PORT": "8443",
        },
    )

    assert result.exit_code == 0, result.output
    assert "https://traefik.localhost:8443" in result.output
    assert any("proxy_compose_https.yaml" in item for item in commands[0])
    assert "--force-recreate" in commands[0]


def test_interactive_start_can_enable_https(monkeypatch) -> None:
    enabled = []
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)
    monkeypatch.setattr("localghost.cli._is_interactive", lambda no_input: True)
    monkeypatch.setattr("localghost.cli._enable_https", lambda: enabled.append(True))
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli.active_routes", lambda: [])

    result = CliRunner().invoke(cli, input="y\n")

    assert result.exit_code == 0, result.output
    assert enabled == [True]
    assert "Started shared proxy at https://" in result.output


def test_proxy_status_reports_running_route_failure(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: True)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: True)
    monkeypatch.setattr(
        "localghost.cli.active_routes",
        lambda: (_ for _ in ()).throw(click.ClickException("inspect failed")),
    )

    result = CliRunner().invoke(cli, ["--status"])

    assert result.exit_code == 0, result.output
    assert "Proxy: running" in result.output
    assert "HTTPS configuration: enabled" in result.output
    assert "inspect failed" in result.output


def test_trust_reports_existing_https_on_a_running_proxy(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("localghost.cli._https_configured", lambda: True)
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: True)
    monkeypatch.setattr("localghost.cli._enable_https", lambda: None)
    monkeypatch.setattr(
        "localghost.cli._run_proxy", lambda *args, **kwargs: pytest.fail("reconciled")
    )

    result = CliRunner().invoke(
        cli, ["trust"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert "already configured for HTTPS" in result.output


def test_trust_remove_reconciles_running_proxy_before_uninstall(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "rootCA.pem").write_bytes(CERTIFICATE_PEM)
    (tmp_path / "https-enabled").touch()
    events = []
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: True)
    monkeypatch.setattr(
        "localghost.cli._run_proxy",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    class Installer:
        def __init__(self, name):
            self.name = name

        def uninstall(self):
            events.append(self.name)

    monkeypatch.setattr(
        "localghost.cli.ZenNssInstaller", lambda path: Installer("zen")
    )
    monkeypatch.setattr(
        "localghost.cli.MkcertInstaller", lambda path: Installer("mkcert")
    )

    result = CliRunner().invoke(
        cli,
        ["trust", "--remove"],
        env={"LOCALGHOST_STATE_DIR": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert events[0] == (
        ("up",),
        {"already_running": True, "https_enabled": False, "force_recreate": True},
    )
    assert events[1:] == ["zen", "mkcert"]
    assert "HTTPS is disabled" in result.output


def test_trust_remove_reports_store_failure(monkeypatch, tmp_path) -> None:
    (tmp_path / "rootCA.pem").write_bytes(CERTIFICATE_PEM)
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)

    class Installer:
        def uninstall(self):
            raise TrustError("store failed")

    monkeypatch.setattr("localghost.cli.ZenNssInstaller", lambda path: Installer())

    result = CliRunner().invoke(
        cli,
        ["trust", "--remove"],
        env={"LOCALGHOST_STATE_DIR": str(tmp_path)},
    )

    assert result.exit_code != 0
    assert "store failed" in result.output


def test_enable_https_clears_marker_when_installation_fails(
    monkeypatch, tmp_path
) -> None:
    marker = tmp_path / "https-enabled"
    marker.touch()
    certificate = PublicCertificate.parse(CERTIFICATE_PEM)
    monkeypatch.setattr("localghost.cli._bootstrap_public_root", lambda: certificate)

    class Installer:
        def install(self):
            raise TrustError("authorization denied")

    monkeypatch.setattr("localghost.cli.MkcertInstaller", lambda path: Installer())

    result = CliRunner().invoke(
        cli, ["trust"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code != 0
    assert "HTTPS remains disabled" in result.output
    assert not marker.exists()


def test_bootstrap_writes_public_root_atomically(monkeypatch, tmp_path) -> None:
    command = []

    def run(arguments, **kwargs):
        command.extend(arguments)
        return CompletedProcess(arguments, 0, CERTIFICATE_PEM, b"")

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    monkeypatch.setattr(
        "localghost.cli._proxy_resource_directory", lambda: nullcontext(tmp_path)
    )
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path / "state"))

    certificate = cli_module._bootstrap_public_root()

    assert certificate.pem == CERTIFICATE_PEM
    assert (tmp_path / "state" / "rootCA.pem").read_bytes() == CERTIFICATE_PEM
    assert command[-4:] == ["run", "--rm", "bootstrap", "--print-root"]


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (CompletedProcess([], 2, b"", b"compose failed"), "compose failed"),
        (CompletedProcess([], 0, b"invalid", b""), "invalid public root"),
    ],
)
def test_bootstrap_reports_command_and_certificate_failures(
    monkeypatch, tmp_path, result, message
) -> None:
    monkeypatch.setattr(
        "localghost.cli._proxy_resource_directory", lambda: nullcontext(tmp_path)
    )
    monkeypatch.setattr("localghost.cli.subprocess.run", lambda *args, **kwargs: result)

    with pytest.raises(click.ClickException, match=message):
        cli_module._bootstrap_public_root()


def test_bootstrap_reports_missing_docker(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "localghost.cli._proxy_resource_directory", lambda: nullcontext(tmp_path)
    )
    monkeypatch.setattr(
        "localghost.cli.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )

    with pytest.raises(click.ClickException, match="docker is required"):
        cli_module._bootstrap_public_root()


def test_state_directory_honors_xdg_state_home(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("LOCALGHOST_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert cli_module._state_directory() == tmp_path / "localghost"


def test_copy_resource_tree_materializes_a_file(tmp_path) -> None:
    source = Path(cli_module.__file__).with_name("Dockerfile")
    destination = tmp_path / "nested" / "Dockerfile"

    cli_module._copy_resource_tree(source, destination)

    assert destination.read_bytes() == source.read_bytes()


def test_proxy_resource_directory_materializes_non_filesystem_resources(
    monkeypatch,
) -> None:
    class Resource:
        def __init__(self, name, *, value=None, children=()):
            self.name = name
            self.value = value
            self.children = children

        def is_dir(self):
            return self.value is None

        def iterdir(self):
            return iter(self.children)

        def open(self, mode):
            assert mode == "rb"
            return BytesIO(self.value)

    root = Resource(
        "localghost",
        children=(Resource("proxy_compose.yaml", value=b"services: {}\n"),),
    )
    monkeypatch.setattr("localghost.cli.resources.files", lambda package: root)

    with cli_module._proxy_resource_directory() as directory:
        assert (directory / "proxy_compose.yaml").read_text(encoding="utf-8") == (
            "services: {}\n"
        )
