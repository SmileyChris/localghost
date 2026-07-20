from pathlib import Path
from subprocess import CompletedProcess

import pytest

from localghost.trust import (
    MkcertInstaller,
    PublicCertificate,
    TrustError,
    ZenNssInstaller,
)

CERTIFICATE_PEM = b"""-----BEGIN CERTIFICATE-----
MAA=
-----END CERTIFICATE-----
"""
FINGERPRINT = PublicCertificate.parse(CERTIFICATE_PEM).fingerprint


def write_certificate(path: Path) -> Path:
    path.write_bytes(CERTIFICATE_PEM)
    return path


def test_public_certificate_parses_and_canonicalizes() -> None:
    certificate = PublicCertificate.parse(CERTIFICATE_PEM.rstrip())

    assert certificate.pem == CERTIFICATE_PEM
    assert certificate.fingerprint == FINGERPRINT


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (b"\xff", "ASCII"),
        (b"PRIVATE KEY", "private-key"),
        (b"", "exactly one"),
        (
            CERTIFICATE_PEM + CERTIFICATE_PEM,
            "exactly one",
        ),
        (b"prefix\n" + CERTIFICATE_PEM, "exactly one"),
        (
            b"-----BEGIN CERTIFICATE-----\n\n-----END CERTIFICATE-----\n",
            "exactly one",
        ),
        (
            b"-----BEGIN CERTIFICATE-----\nnot-base64\n"
            b"-----END CERTIFICATE-----\n",
            "invalid X.509",
        ),
    ],
)
def test_public_certificate_rejects_invalid_input(value, message) -> None:
    with pytest.raises(TrustError, match=message):
        PublicCertificate.parse(value)


def test_mkcert_installs_and_uninstalls_only_the_selected_stores(tmp_path) -> None:
    certificate_path = write_certificate(tmp_path / "rootCA.pem")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return CompletedProcess(command, 0, "", "")

    installer = MkcertInstaller(
        certificate_path, runner=run, which=lambda name: "/usr/bin/mkcert"
    )
    installer.install()
    installer.uninstall()

    assert [call[0] for call in calls] == [
        ["/usr/bin/mkcert", "-install"],
        ["/usr/bin/mkcert", "-uninstall"],
    ]
    assert calls[0][1]["env"]["CAROOT"] == str(tmp_path)
    assert calls[0][1]["env"]["TRUST_STORES"] == "system,nss"
    assert calls[0][1]["check"] is False


def test_mkcert_reports_missing_executable_and_command_failure(tmp_path) -> None:
    certificate_path = write_certificate(tmp_path / "rootCA.pem")
    missing = MkcertInstaller(certificate_path, which=lambda name: None)

    with pytest.raises(TrustError, match="mkcert is unavailable"):
        missing.install()

    failing = MkcertInstaller(
        certificate_path,
        which=lambda name: "mkcert",
        runner=lambda command, **kwargs: CompletedProcess(command, 2, "", "denied"),
    )
    with pytest.raises(TrustError, match="mkcert -uninstall failed: denied"):
        failing.uninstall()


def zen_profile(home: Path) -> Path:
    profile = home / ".config" / "zen" / "profile"
    profile.mkdir(parents=True)
    (profile / "cert9.db").touch()
    return profile


def test_zen_install_is_a_noop_without_profiles(tmp_path) -> None:
    certificate_path = write_certificate(tmp_path / "rootCA.pem")
    ZenNssInstaller(
        certificate_path,
        home=tmp_path / "home",
        which=lambda name: pytest.fail("looked up certutil"),
    ).install()


def test_zen_install_requires_certutil_when_a_profile_exists(tmp_path) -> None:
    certificate_path = write_certificate(tmp_path / "rootCA.pem")
    zen_profile(tmp_path / "home")

    with pytest.raises(TrustError, match="certutil is unavailable"):
        ZenNssInstaller(
            certificate_path, home=tmp_path / "home", which=lambda name: None
        ).install()


@pytest.mark.parametrize("existing", [False, True])
def test_zen_install_adds_or_preserves_the_exact_certificate(
    tmp_path, existing
) -> None:
    certificate_path = write_certificate(tmp_path / "rootCA.pem")
    profile = zen_profile(tmp_path / "home")
    installed = existing
    calls = []

    def run(command, **kwargs):
        nonlocal installed
        calls.append(command)
        if command[1] == "-L":
            if installed:
                return CompletedProcess(command, 0, CERTIFICATE_PEM.decode(), "")
            return CompletedProcess(command, 1, "", "missing")
        assert command[1] == "-A"
        installed = True
        return CompletedProcess(command, 0, "", "")

    ZenNssInstaller(
        certificate_path,
        home=tmp_path / "home",
        runner=run,
        which=lambda name: "certutil",
    ).install()

    assert any(f"sql:{profile}" in argument for argument in calls[0])
    assert [command[1] for command in calls].count("-A") == (0 if existing else 1)


def test_zen_install_rejects_a_nickname_collision(tmp_path) -> None:
    certificate_path = write_certificate(tmp_path / "rootCA.pem")
    zen_profile(tmp_path / "home")
    other = CERTIFICATE_PEM.replace(b"MAA=", b"MAE=")

    with pytest.raises(TrustError, match="nickname collision"):
        ZenNssInstaller(
            certificate_path,
            home=tmp_path / "home",
            runner=lambda command, **kwargs: CompletedProcess(
                command, 0, other.decode(), ""
            ),
            which=lambda name: "certutil",
        ).install()


def test_zen_install_reports_failed_verification(tmp_path) -> None:
    certificate_path = write_certificate(tmp_path / "rootCA.pem")
    zen_profile(tmp_path / "home")

    def run(command, **kwargs):
        return CompletedProcess(command, 1, "", "failed")

    with pytest.raises(TrustError, match="installation failed"):
        ZenNssInstaller(
            certificate_path,
            home=tmp_path / "home",
            runner=run,
            which=lambda name: "certutil",
        ).install()


def test_zen_uninstall_removes_only_the_matching_nickname(tmp_path) -> None:
    certificate_path = write_certificate(tmp_path / "rootCA.pem")
    zen_profile(tmp_path / "home")
    installed = True

    def run(command, **kwargs):
        nonlocal installed
        if command[1] == "-L":
            status = 0 if installed else 1
            output = CERTIFICATE_PEM.decode() if installed else ""
            return CompletedProcess(command, status, output, "")
        assert command[1] == "-D"
        installed = False
        return CompletedProcess(command, 0, "", "")

    ZenNssInstaller(
        certificate_path,
        home=tmp_path / "home",
        runner=run,
        which=lambda name: "certutil",
    ).uninstall()

    assert installed is False


def test_zen_uninstall_handles_absent_inputs_and_failed_removal(tmp_path) -> None:
    missing_path = tmp_path / "missing.pem"
    ZenNssInstaller(missing_path, home=tmp_path).uninstall()

    certificate_path = write_certificate(tmp_path / "rootCA.pem")
    zen_profile(tmp_path / "home")
    ZenNssInstaller(
        certificate_path, home=tmp_path / "home", which=lambda name: None
    ).uninstall()

    def run(command, **kwargs):
        if command[1] == "-L":
            return CompletedProcess(command, 0, CERTIFICATE_PEM.decode(), "")
        return CompletedProcess(command, 1, "", "failed")

    with pytest.raises(TrustError, match="removal failed"):
        ZenNssInstaller(
            certificate_path,
            home=tmp_path / "home",
            runner=run,
            which=lambda name: "certutil",
        ).uninstall()
