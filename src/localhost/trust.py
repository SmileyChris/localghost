"""Host-trust support for the optional local HTTPS proxy."""

from __future__ import annotations

import hashlib
import os
import shutil
import ssl
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class TrustError(RuntimeError):
    """The public root could not be installed or removed safely."""


@dataclass(frozen=True)
class PublicCertificate:
    pem: bytes
    fingerprint: str

    @classmethod
    def parse(cls, value: bytes) -> PublicCertificate:
        try:
            text = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise TrustError("certificate PEM must be ASCII") from exc
        if "PRIVATE KEY" in text:
            raise TrustError("private-key material is forbidden")
        begin = "-----BEGIN CERTIFICATE-----"
        end = "-----END CERTIFICATE-----"
        if text.count(begin) != 1 or text.count(end) != 1:
            raise TrustError("expected exactly one public CERTIFICATE PEM block")
        prefix, body = text.split(begin, 1)
        certificate_body, suffix = body.split(end, 1)
        if prefix.strip() or suffix.strip() or not certificate_body.strip():
            raise TrustError("expected exactly one public CERTIFICATE PEM block")
        canonical = f"{begin}{certificate_body}{end}\n"
        try:
            der = ssl.PEM_cert_to_DER_cert(canonical)
        except ValueError as exc:
            raise TrustError("invalid X.509 certificate PEM") from exc
        return cls(
            pem=canonical.encode("ascii"),
            fingerprint="SHA256:" + hashlib.sha256(der).hexdigest().upper(),
        )


class MkcertInstaller:
    """Ask mkcert to manage only the exported public root."""

    def __init__(
        self,
        certificate_path: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.certificate_path = certificate_path
        self.runner = runner
        self.which = which

    def _run(self, action: str) -> None:
        executable = self.which("mkcert")
        if executable is None:
            raise TrustError(
                "mkcert is unavailable; HTTPS remains disabled. Install mkcert, "
                "then run `localhost trust`."
            )
        result = self.runner(
            [executable, action],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "CAROOT": str(self.certificate_path.parent),
                "TRUST_STORES": "system,nss",
            },
        )
        if result.returncode:
            detail = (
                result.stderr or result.stdout or "mkcert returned a non-zero status"
            ).strip()
            raise TrustError(f"mkcert {action} failed: {detail}")

    def install(self) -> None:
        PublicCertificate.parse(self.certificate_path.read_bytes())
        self._run("-install")

    def uninstall(self) -> None:
        PublicCertificate.parse(self.certificate_path.read_bytes())
        self._run("-uninstall")


class ZenNssInstaller:
    """Install into Zen profiles, which mkcert does not discover reliably."""

    prefix = "localhost-proxy-"

    def __init__(
        self,
        certificate_path: Path,
        *,
        home: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.certificate_path = certificate_path
        self.home = home or Path.home()
        self.runner = runner
        self.which = which

    def _profiles(self) -> list[Path]:
        database_files = (self.home / ".config" / "zen").glob("*/cert9.db")
        return sorted(path.parent for path in database_files)

    def _nickname(self, certificate: PublicCertificate) -> str:
        return self.prefix + certificate.fingerprint.removeprefix("SHA256:")[:16]

    def _certutil(self) -> str | None:
        return self.which("certutil")

    def _inspect(self, executable: str, profile: Path, nickname: str) -> str | None:
        result = self.runner(
            [executable, "-L", "-d", f"sql:{profile}", "-n", nickname, "-a"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            return None
        return PublicCertificate.parse(result.stdout.encode("ascii")).fingerprint

    def install(self) -> None:
        profiles = self._profiles()
        if not profiles:
            return
        executable = self._certutil()
        if executable is None:
            raise TrustError(
                "Zen profile found but certutil is unavailable; install nss tools, "
                "then run `localhost trust` again."
            )
        certificate = PublicCertificate.parse(self.certificate_path.read_bytes())
        nickname = self._nickname(certificate)
        for profile in profiles:
            found = self._inspect(executable, profile, nickname)
            if found == certificate.fingerprint:
                continue
            if found is not None:
                raise TrustError(f"Zen NSS nickname collision in {profile}; unchanged")
            result = self.runner(
                [
                    executable,
                    "-A",
                    "-d",
                    f"sql:{profile}",
                    "-n",
                    nickname,
                    "-t",
                    "C,,",
                    "-i",
                    str(self.certificate_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if (
                result.returncode
                or self._inspect(executable, profile, nickname)
                != certificate.fingerprint
            ):
                raise TrustError(f"Zen NSS installation failed for {profile}")

    def uninstall(self) -> None:
        if not self.certificate_path.exists():
            return
        executable = self._certutil()
        if executable is None:
            return
        certificate = PublicCertificate.parse(self.certificate_path.read_bytes())
        nickname = self._nickname(certificate)
        for profile in self._profiles():
            if self._inspect(executable, profile, nickname) is None:
                continue
            result = self.runner(
                [executable, "-D", "-d", f"sql:{profile}", "-n", nickname],
                check=False,
                capture_output=True,
                text=True,
            )
            if (
                result.returncode
                or self._inspect(executable, profile, nickname) is not None
            ):
                raise TrustError(f"Zen NSS removal failed for {profile}")
