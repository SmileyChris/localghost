from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from subprocess import CompletedProcess

import click
import pytest
from click.testing import CliRunner

import localghost.cli as cli_module
from localghost.cli import cli
from localghost.tailscale import TailscaleState
from localghost.trust import PublicCertificate, TrustError

CERTIFICATE_PEM = b"""-----BEGIN CERTIFICATE-----
MAA=
-----END CERTIFICATE-----
"""


def test_trust_status_reports_an_absent_root_without_starting_proxy(tmp_path) -> None:
    result = CliRunner().invoke(
        cli, ["trust", "status"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert "HTTPS: disabled" in result.output


def test_trust_mode_flags_are_gone() -> None:
    for flag in ("--status", "--remove"):
        result = CliRunner().invoke(cli, ["trust", flag])

        assert result.exit_code != 0, flag
        assert "no such option" in result.output.lower()


def test_bare_trust_reports_without_touching_trust_stores(monkeypatch) -> None:
    enabled = []
    monkeypatch.setattr("localghost.cli._enable_https", lambda: enabled.append(True))
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)

    result = CliRunner().invoke(cli, ["trust"])

    assert result.exit_code == 0, result.output
    assert enabled == []
    assert "HTTPS: disabled" in result.output


def test_trust_status_reports_valid_enabled_root(tmp_path) -> None:
    (tmp_path / "rootCA.pem").write_bytes(CERTIFICATE_PEM)
    (tmp_path / "https-enabled").touch()

    result = CliRunner().invoke(
        cli, ["trust", "status"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert "HTTPS: enabled" in result.output
    assert PublicCertificate.parse(CERTIFICATE_PEM).fingerprint in result.output
    assert "Managed stores: system,nss" in result.output


def test_trust_status_rejects_an_invalid_root(tmp_path) -> None:
    (tmp_path / "rootCA.pem").write_text("not a certificate", encoding="utf-8")

    result = CliRunner().invoke(
        cli, ["trust", "status"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
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
        ["hub", "up"],
        env={
            "LOCALGHOST_STATE_DIR": str(tmp_path),
            "LOCALGHOST_HTTPS_PORT": "8443",
        },
    )

    assert result.exit_code == 0, result.output
    assert "https://traefik.localhost:8443" in result.output
    compose = compose_command(commands)
    assert any("proxy_compose_https.yaml" in item for item in compose)
    assert "--force-recreate" not in compose


def compose_command(commands: list[list[str]]) -> list[str]:
    return next(command for command in commands if command[:2] == ["docker", "compose"])


def start_recorder(monkeypatch, tmp_path, *, images_present: bool):
    """Record the commands a `localghost hub up` run issues."""
    (tmp_path / "rootCA.pem").write_bytes(CERTIFICATE_PEM)
    (tmp_path / "https-enabled").touch()
    commands: list[list[str]] = []
    monkeypatch.setattr(cli_module, "proxy_is_running", lambda: True)
    monkeypatch.setattr(cli_module, "active_routes", lambda: [])

    def run(command, **kwargs):
        commands.append(command)
        code = 0 if images_present or command[:2] != ["docker", "image"] else 1
        return CompletedProcess(command, code, "", "")

    monkeypatch.setattr(cli_module.subprocess, "run", run)
    return commands


def test_start_does_not_rebuild_images_that_already_exist(
    monkeypatch, tmp_path
) -> None:
    commands = start_recorder(monkeypatch, tmp_path, images_present=True)

    result = CliRunner().invoke(
        cli, ["hub", "up"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    # Hub images are tagged with the release version, so re-checking the build
    # of an image that already exists is pure latency.
    assert "--no-build" in compose_command(commands)


def test_start_builds_when_a_hub_image_is_missing(monkeypatch, tmp_path) -> None:
    commands = start_recorder(monkeypatch, tmp_path, images_present=False)

    result = CliRunner().invoke(
        cli, ["hub", "up"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert "--no-build" not in compose_command(commands)


def test_rebuild_forces_a_build_of_existing_images(monkeypatch, tmp_path) -> None:
    commands = start_recorder(monkeypatch, tmp_path, images_present=True)

    result = CliRunner().invoke(
        cli, ["hub", "up", "--rebuild"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert "--no-build" not in compose_command(commands)


def test_start_checks_the_gateway_image_while_tailnet_hosting_is_on(
    monkeypatch, tmp_path
) -> None:
    state = TailscaleState(
        tailnet="example.com",
        suffix="tailwork",
        gateway_ips=("100.64.0.10",),
        previous_split_dns={},
    )
    commands = start_recorder(monkeypatch, tmp_path, images_present=True)
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)

    result = CliRunner().invoke(
        cli, ["hub", "up"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    inspected = [
        command for command in commands if command[:3] == ["docker", "image", "inspect"]
    ]
    assert any(
        f"localghost-tailscale-gateway:v{cli_module.LOCALGHOST_VERSION}" in command
        for command in inspected
    )


def test_interactive_start_can_enable_https(monkeypatch) -> None:
    enabled = []
    monkeypatch.setattr("localghost.cli._https_configured", lambda: False)
    monkeypatch.setattr("localghost.cli._is_interactive", lambda no_input: True)
    monkeypatch.setattr("localghost.cli.shutil.which", lambda name: "mkcert")
    monkeypatch.setattr("localghost.cli._enable_https", lambda: enabled.append(True))
    monkeypatch.setattr("localghost.cli._managed_image_is_available", lambda: False)
    monkeypatch.setattr("localghost.cli._run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli.active_routes", lambda: [])

    result = CliRunner().invoke(cli, ["hub", "up"], input="y\n")

    assert result.exit_code == 0, result.output
    assert enabled == [True]
    assert "Hub is ready at https://" in result.output


def test_proxy_status_reports_running_route_failure(monkeypatch) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: True)
    monkeypatch.setattr("localghost.cli._https_configured", lambda: True)
    monkeypatch.setattr(
        "localghost.cli.active_routes",
        lambda: (_ for _ in ()).throw(click.ClickException("inspect failed")),
    )

    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "Hub: running" in result.output
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
        cli, ["trust", "install"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert "already configured for HTTPS" in result.output


def test_trust_installs_active_tailnet_root(monkeypatch, tmp_path) -> None:
    state = TailscaleState(
        tailnet="example.com",
        suffix="tailwork",
        gateway_ips=("100.64.0.10",),
        previous_split_dns={},
    )
    installed = []

    class Installer:
        def __init__(self, path, **kwargs):
            self.path = path

        def install(self, **kwargs):
            installed.append(self.path)

    monkeypatch.setattr(cli_module, "_enable_https", lambda: None)
    monkeypatch.setattr(cli_module, "_https_configured", lambda: True)
    monkeypatch.setattr(cli_module, "proxy_is_running", lambda: False)
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    monkeypatch.setattr(
        cli_module, "fetch_tailscale_root", lambda suffix, gateway_ips: CERTIFICATE_PEM
    )
    monkeypatch.setattr(cli_module, "MkcertInstaller", Installer)
    monkeypatch.setattr(cli_module, "ZenNssInstaller", Installer)

    result = CliRunner().invoke(
        cli, ["trust", "install"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    tailnet_root = tmp_path / "tailscale-tailwork-rootCA.pem"
    assert installed == [tailnet_root, tailnet_root]
    assert tailnet_root.read_bytes() == CERTIFICATE_PEM


def test_trust_scopes_each_authority_for_zen(monkeypatch, tmp_path) -> None:
    state = TailscaleState(
        tailnet="example.com",
        suffix="tailwork",
        gateway_ips=("100.64.0.10",),
        previous_split_dns={},
    )
    scopes = []
    certificate = PublicCertificate.parse(CERTIFICATE_PEM)

    class Zen:
        def __init__(self, path, *, scope):
            scopes.append((path.name, scope))

        def install(self, **kwargs):
            return None

    class Mkcert:
        def __init__(self, path):
            self.path = path

        def install(self, **kwargs):
            return None

    monkeypatch.setattr(cli_module, "proxy_is_running", lambda: False)
    monkeypatch.setattr(cli_module, "_bootstrap_public_root", lambda: certificate)
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    monkeypatch.setattr(
        cli_module, "fetch_tailscale_root", lambda suffix, gateway_ips: CERTIFICATE_PEM
    )
    monkeypatch.setattr(cli_module, "MkcertInstaller", Mkcert)
    monkeypatch.setattr(cli_module, "ZenNssInstaller", Zen)

    result = CliRunner().invoke(
        cli, ["trust", "install"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert scopes == [
        ("rootCA.pem", "localhost"),
        ("tailscale-tailwork-rootCA.pem", "tailwork"),
    ]


def test_trust_remove_scopes_each_authority_for_zen(monkeypatch, tmp_path) -> None:
    (tmp_path / "rootCA.pem").write_bytes(CERTIFICATE_PEM)
    (tmp_path / "tailscale-tailwork-rootCA.pem").write_bytes(CERTIFICATE_PEM)
    scopes = []

    class Zen:
        def __init__(self, path, *, scope):
            scopes.append((path.name, scope))

        def uninstall(self):
            return None

    class Mkcert:
        def __init__(self, path):
            self.path = path

        def uninstall(self):
            return None

    monkeypatch.setattr(cli_module, "proxy_is_running", lambda: False)
    monkeypatch.setattr(cli_module, "MkcertInstaller", Mkcert)
    monkeypatch.setattr(cli_module, "ZenNssInstaller", Zen)

    result = CliRunner().invoke(
        cli, ["trust", "remove"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert scopes == [
        ("rootCA.pem", "localhost"),
        ("tailscale-tailwork-rootCA.pem", "tailwork"),
    ]


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
        "localghost.cli.ZenNssInstaller", lambda path, **kwargs: Installer("zen")
    )
    monkeypatch.setattr(
        "localghost.cli.MkcertInstaller", lambda path: Installer("mkcert")
    )

    result = CliRunner().invoke(
        cli,
        ["trust", "remove"],
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

    monkeypatch.setattr(
        "localghost.cli.ZenNssInstaller", lambda path, **kwargs: Installer()
    )

    result = CliRunner().invoke(
        cli,
        ["trust", "remove"],
        env={"LOCALGHOST_STATE_DIR": str(tmp_path)},
    )

    assert result.exit_code != 0
    assert "store failed" in result.output


def test_trust_remove_keeps_marker_when_proxy_reconciliation_fails(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "rootCA.pem").write_bytes(CERTIFICATE_PEM)
    marker = tmp_path / "https-enabled"
    marker.touch()
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: True)
    monkeypatch.setattr(
        "localghost.cli._run_proxy",
        lambda *args, **kwargs: (_ for _ in ()).throw(click.exceptions.Exit(9)),
    )

    result = CliRunner().invoke(
        cli,
        ["trust", "remove"],
        env={"LOCALGHOST_STATE_DIR": str(tmp_path)},
    )

    assert result.exit_code == 9
    assert marker.exists()


def test_enable_https_clears_marker_when_installation_fails(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    marker = tmp_path / "https-enabled"
    marker.touch()
    certificate = PublicCertificate.parse(CERTIFICATE_PEM)
    monkeypatch.setattr("localghost.cli._bootstrap_public_root", lambda: certificate)

    class Installer:
        def install(self, **kwargs):
            raise TrustError("authorization denied")

        def uninstall(self):
            return None

    monkeypatch.setattr("localghost.cli.MkcertInstaller", lambda path: Installer())

    result = CliRunner().invoke(
        cli, ["trust", "install"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code != 0
    assert "HTTPS remains disabled" in result.output
    assert not marker.exists()


def test_enable_https_rolls_back_partial_trust_installation(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    certificate = PublicCertificate.parse(CERTIFICATE_PEM)
    monkeypatch.setattr("localghost.cli._bootstrap_public_root", lambda: certificate)
    events = []

    class Mkcert:
        def install(self, **kwargs):
            events.append("mkcert install")

        def uninstall(self):
            events.append("mkcert uninstall")

    class Zen:
        def install(self, **kwargs):
            events.append("zen install")
            raise TrustError("Zen installation failed")

        def uninstall(self):
            events.append("zen uninstall")

    monkeypatch.setattr("localghost.cli.MkcertInstaller", lambda path: Mkcert())
    monkeypatch.setattr("localghost.cli.ZenNssInstaller", lambda path, **kwargs: Zen())

    result = CliRunner().invoke(
        cli, ["trust", "install"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code != 0
    assert events == [
        "mkcert install",
        "zen install",
        "zen uninstall",
        "mkcert uninstall",
    ]
    assert "HTTPS remains disabled" in result.output
    assert not (tmp_path / "https-enabled").exists()


def test_enable_https_retains_an_existing_configuration_on_refresh_failure(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "rootCA.pem").write_bytes(CERTIFICATE_PEM)
    marker = tmp_path / "https-enabled"
    marker.touch()
    certificate = PublicCertificate.parse(CERTIFICATE_PEM)
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli._bootstrap_public_root", lambda: certificate)

    class Installer:
        def install(self, **kwargs):
            raise TrustError("refresh failed")

        def uninstall(self):
            pytest.fail("existing trust was rolled back")

    monkeypatch.setattr("localghost.cli.MkcertInstaller", lambda path: Installer())

    result = CliRunner().invoke(
        cli, ["trust", "install"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code != 0
    assert "existing HTTPS configuration was retained" in result.output
    assert marker.exists()


def test_enable_https_reports_incomplete_automatic_rollback(
    monkeypatch, tmp_path
) -> None:
    certificate = PublicCertificate.parse(CERTIFICATE_PEM)
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli._bootstrap_public_root", lambda: certificate)

    class Mkcert:
        def install(self, **kwargs):
            raise TrustError("installation failed")

        def uninstall(self):
            raise TrustError("mkcert cleanup failed")

    class Zen:
        def install(self, **kwargs):
            pytest.fail("Zen install should not run")

        def uninstall(self):
            raise TrustError("Zen cleanup failed")

    monkeypatch.setattr("localghost.cli.MkcertInstaller", lambda path: Mkcert())
    monkeypatch.setattr("localghost.cli.ZenNssInstaller", lambda path, **kwargs: Zen())

    result = CliRunner().invoke(
        cli, ["trust", "install"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code != 0
    assert "automatic trust rollback also failed" in result.output
    assert "Zen NSS: Zen cleanup failed" in result.output
    assert "mkcert: mkcert cleanup failed" in result.output


def test_enable_https_forces_mkcert_reinstall_on_root_rotation(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "rootCA.pem").write_bytes(CERTIFICATE_PEM)
    (tmp_path / "https-enabled").touch()
    old_fingerprint = "SHA256:" + "B" * 64
    (tmp_path / "root-fingerprint").write_text(old_fingerprint)
    certificate = PublicCertificate.parse(CERTIFICATE_PEM)
    monkeypatch.setattr("localghost.cli.proxy_is_running", lambda: False)
    monkeypatch.setattr("localghost.cli._bootstrap_public_root", lambda: certificate)
    calls = []

    class Mkcert:
        def install(self, force=False):
            calls.append(("mkcert install", force))

        def uninstall(self):
            calls.append("mkcert uninstall")

    class Zen:
        def install(self, **kwargs):
            calls.append("zen install")

    monkeypatch.setattr("localghost.cli.MkcertInstaller", lambda path: Mkcert())
    monkeypatch.setattr("localghost.cli.ZenNssInstaller", lambda path, **kwargs: Zen())

    result = CliRunner().invoke(
        cli, ["trust", "install"], env={"LOCALGHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("mkcert install", True),
        "zen install",
    ]
    assert (tmp_path / "root-fingerprint").read_text() == certificate.fingerprint


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


def test_bootstrap_names_the_versioned_hub_image(monkeypatch, tmp_path) -> None:
    captured = {}

    def run(arguments, **kwargs):
        captured["env"] = kwargs.get("env")
        return CompletedProcess(arguments, 0, CERTIFICATE_PEM, b"")

    monkeypatch.setattr("localghost.cli.subprocess.run", run)
    monkeypatch.setattr(
        "localghost.cli._proxy_resource_directory", lambda: nullcontext(tmp_path)
    )
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path / "state"))

    cli_module._bootstrap_public_root()

    # The bootstrap job runs the hub image, so its tag has to be resolvable.
    assert captured["env"]["LOCALGHOST_IMAGE_TAG"] == (
        f"v{cli_module.LOCALGHOST_VERSION}"
    )


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
