"""Persistent configuration and scoped Tailscale API operations."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import KeyringError

from .paths import state_directory

API_ROOT = "https://api.tailscale.com/api/v2"
TOKEN_URL = f"{API_ROOT}/oauth/token"
OAUTH_CONSOLE_URL = "https://login.tailscale.com/admin/settings/oauth"
POLICY_CONSOLE_URL = "https://login.tailscale.com/admin/acls/file"
KEYRING_SERVICE = "localghost-tailscale"
_SUFFIX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class TailscaleError(RuntimeError):
    """An actionable Tailscale configuration failure."""


class APIError(TailscaleError):
    """A Tailscale API rejection carrying its HTTP status code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TailscaleState:
    tailnet: str
    suffix: str
    gateway_ips: tuple[str, ...]
    previous_split_dns: dict[str, list[str]]
    tag: str = "tag:localghost"


def state_path() -> Path:
    return state_directory() / "tailscale.json"


def load_state() -> TailscaleState | None:
    path = state_path()
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return TailscaleState(
            tailnet=value["tailnet"],
            suffix=validate_suffix(value["suffix"]),
            gateway_ips=tuple(value["gateway_ips"]),
            previous_split_dns=value["previous_split_dns"],
            tag=value.get("tag", "tag:localghost"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TailscaleError(f"invalid saved Tailscale state in {path}: {exc}") from exc


def save_state(state: TailscaleState) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".tailscale-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(asdict(state), stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def remove_state() -> None:
    state_path().unlink(missing_ok=True)


@dataclass(frozen=True)
class Credential:
    client_id: str
    client_secret: str


def store_credential(client_id: str, client_secret: str) -> bool:
    """Save the OAuth credential in the system keyring; report success."""
    try:
        keyring.set_password(KEYRING_SERVICE, "client-id", client_id)
        keyring.set_password(KEYRING_SERVICE, "client-secret", client_secret)
    except KeyringError:
        return False
    return True


def load_credential() -> Credential | None:
    try:
        client_id = keyring.get_password(KEYRING_SERVICE, "client-id")
        client_secret = keyring.get_password(KEYRING_SERVICE, "client-secret")
    except KeyringError:
        return None
    if not client_id or not client_secret:
        return None
    return Credential(client_id, client_secret)


def delete_credential() -> None:
    for name in ("client-id", "client-secret"):
        with suppress(KeyringError):
            keyring.delete_password(KEYRING_SERVICE, name)


# "localghost-" plus the suffix must still fit in one 63-character DNS label
# when it becomes the gateway hostname.
_SUFFIX_LIMIT = 63 - len("localghost-")

# The tailnet root is name-constrained to exactly the suffix, so the one
# remaining way to mint a root for real websites is choosing a real TLD as
# the suffix. Every ccTLD is exactly two letters, and this best-effort list
# covers the reserved names and common gTLDs; MagicDNS labels like tail1234
# are unaffected.
_PUBLIC_OR_RESERVED = frozenset(
    "localhost local internal home corp lan mail arpa onion test example "  # noqa: SIM905
    "invalid alt zip mov "
    "com net org edu gov mil int info biz name pro mobi asia tel dev app xyz "
    "online site tech store shop blog cloud page link live art bank club "
    "design digital email fun games group host life media network news one "
    "ooo party plus press pub red rocks run social software solutions space "
    "studio systems team today tools top video vip web website wiki work "
    "works world zone new day city eco law men ninja".split()
)


def validate_suffix(value: str) -> str:
    value = value.removesuffix(".").lower()
    if not _SUFFIX.fullmatch(value):
        raise ValueError("suffix must be one DNS label (for example, tail1234)")
    if len(value) == 2 or value in _PUBLIC_OR_RESERVED:
        raise ValueError(
            f"suffix .{value} is a public or reserved DNS name; a root "
            "scoped to it could impersonate real websites — choose a "
            "private label such as tail1234"
        )
    if len(value) > _SUFFIX_LIMIT:
        raise ValueError(
            f"suffix must be at most {_SUFFIX_LIMIT} characters so the "
            f"localghost-{{suffix}} gateway hostname stays one DNS label"
        )
    return value


def detect_suffix() -> str:
    """Choose a suffix from the local Tailscale client's DNS view.

    An explicit one-label search domain wins, then the tailnet's own MagicDNS
    label (``taildc3ac3`` for ``taildc3ac3.ts.net``), which is stable and
    collides with no device's short name. This machine's own hostname is the
    last resort.
    """
    try:
        result = subprocess.run(
            ["tailscale", "dns", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TailscaleError(
            "pass --suffix because the tailscale CLI is unavailable"
        ) from exc
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {}
        candidates = payload.get("SearchDomains") or []
        for candidate in candidates:
            candidate = str(candidate).removesuffix(".")
            if "." not in candidate:
                try:
                    return validate_suffix(candidate)
                except ValueError:
                    pass
        tailnet = payload.get("CurrentTailnet") or {}
        for name in (tailnet.get("MagicDNSSuffix"), tailnet.get("SelfDNSName")):
            if not name:
                continue
            try:
                return validate_suffix(str(name).split(".", 1)[0])
            except ValueError:
                pass
    raise TailscaleError("could not detect a short tailnet suffix; pass --suffix")


class API:
    def __init__(self, token: str):
        self.token = token

    @classmethod
    def authenticate(cls, client_id: str, client_secret: str) -> API:
        credentials = base64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode()
        payload = urllib.parse.urlencode(
            {"grant_type": "client_credentials"}
        ).encode()
        value = _request(
            TOKEN_URL,
            method="POST",
            data=payload,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            return cls(value["access_token"])
        except (KeyError, TypeError) as exc:
            raise TailscaleError(
                "Tailscale OAuth response contained no access token"
            ) from exc

    def _call(self, path: str, *, method: str = "GET", body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        return _request(
            f"{API_ROOT}{path}",
            method=method,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )

    def create_auth_key(self, tailnet: str, tag: str) -> str:
        tailnet = urllib.parse.quote(tailnet, safe="")
        try:
            value = self._call(
                f"/tailnet/{tailnet}/keys",
                method="POST",
                body={
                    "capabilities": {
                        "devices": {
                            "create": {
                                "reusable": False,
                                "ephemeral": False,
                                "preauthorized": True,
                                "tags": [tag],
                            }
                        }
                    },
                    "expirySeconds": 600,
                },
            )
        except APIError as exc:
            if exc.code == 400 and "tag" in str(exc):
                raise TailscaleError(
                    f"Tailscale rejected the device tag: add {tag} to tagOwners "
                    f"in the policy file ({POLICY_CONSOLE_URL}), for example: "
                    f'"tagOwners": {{"{tag}": ["autogroup:admin"]}}'
                ) from exc
            if exc.code == 403:
                raise TailscaleError(
                    f"the OAuth client may not create auth keys: it needs the "
                    f"auth_keys scope with {tag} allowed ({OAUTH_CONSOLE_URL})"
                ) from exc
            raise
        try:
            return value["key"]
        except (KeyError, TypeError) as exc:
            raise TailscaleError("Tailscale did not return an auth key") from exc

    @staticmethod
    def _dns_scope_guidance(exc: APIError) -> TailscaleError:
        if exc.code == 403:
            return TailscaleError(
                "the OAuth client lacks the dns:write scope; recreate it at "
                f"{OAUTH_CONSOLE_URL}"
            )
        return exc

    def split_dns(self, tailnet: str) -> dict[str, list[str]]:
        tailnet = urllib.parse.quote(tailnet, safe="")
        try:
            value = self._call(f"/tailnet/{tailnet}/dns/split-dns")
        except APIError as exc:
            raise self._dns_scope_guidance(exc) from exc
        if not isinstance(value, dict) or any(
            not isinstance(domain, str)
            or not isinstance(addresses, list)
            or any(not isinstance(address, str) for address in addresses)
            for domain, addresses in value.items()
        ):
            raise TailscaleError("unexpected Tailscale DNS response")
        return value

    def update_split_dns(
        self, tailnet: str, value: dict[str, list[str] | None]
    ) -> None:
        tailnet = urllib.parse.quote(tailnet, safe="")
        try:
            self._call(
                f"/tailnet/{tailnet}/dns/split-dns", method="PATCH", body=value
            )
        except APIError as exc:
            raise self._dns_scope_guidance(exc) from exc


# A root certificate PEM is around a kilobyte; anything near this cap is not
# one.
_MAX_ROOT_BYTES = 65536


def fetch_public_root(suffix: str, gateway_ips: tuple[str, ...] = ()) -> bytes:
    suffix = validate_suffix(suffix)
    host = f"trust.{suffix}"
    candidates = gateway_ips or (host,)
    last_error: urllib.error.URLError | None = None
    for candidate in candidates:
        try:
            address = str(ipaddress.ip_address(candidate))
            if ":" in address:
                address = f"[{address}]"
        except ValueError:
            address = candidate
        url = f"http://{address}/.well-known/localghost/root.pem"
        request = urllib.request.Request(url, headers={"Host": host})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                value = response.read(_MAX_ROOT_BYTES + 1)
        except urllib.error.URLError as exc:
            last_error = exc
            continue
        if len(value) > _MAX_ROOT_BYTES:
            raise TailscaleError(
                f"the .{suffix} gateway returned an oversized root certificate"
            )
        return value
    raise TailscaleError(
        f"could not download the localghost root from the .{suffix} gateway"
    ) from last_error


def _request(
    url: str, *, method: str, data: bytes | None, headers: dict[str, str]
) -> Any:
    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise APIError(
            exc.code, f"Tailscale API returned HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TailscaleError(
            f"could not reach the Tailscale API: {exc.reason}"
        ) from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TailscaleError("Tailscale API returned invalid JSON") from exc
