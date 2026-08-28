"""Persistent configuration and scoped Tailscale API operations."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .paths import state_directory

API_ROOT = "https://api.tailscale.com/api/v2"
TOKEN_URL = f"{API_ROOT}/oauth/token"
_SUFFIX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class TailscaleError(RuntimeError):
    """An actionable Tailscale configuration failure."""


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


def validate_suffix(value: str) -> str:
    value = value.removesuffix(".").lower()
    if not _SUFFIX.fullmatch(value) or value in {"localhost", "local"}:
        raise ValueError("suffix must be one DNS label (for example, tail1234)")
    return value


def detect_suffix() -> str:
    """Use a one-label search domain advertised by the local Tailscale client."""
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
        candidates = payload.get("SearchDomains", [])
        for candidate in candidates:
            candidate = str(candidate).removesuffix(".")
            if "." not in candidate:
                try:
                    return validate_suffix(candidate)
                except ValueError:
                    pass
        self_dns_name = (payload.get("CurrentTailnet") or {}).get(
            "SelfDNSName", ""
        )
        if self_dns_name:
            try:
                return validate_suffix(str(self_dns_name).split(".", 1)[0])
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
        try:
            return value["key"]
        except (KeyError, TypeError) as exc:
            raise TailscaleError("Tailscale did not return an auth key") from exc

    def split_dns(self, tailnet: str) -> dict[str, list[str]]:
        tailnet = urllib.parse.quote(tailnet, safe="")
        value = self._call(f"/tailnet/{tailnet}/dns/split-dns")
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
        self._call(
            f"/tailnet/{tailnet}/dns/split-dns", method="PATCH", body=value
        )


def fetch_public_root(suffix: str) -> bytes:
    suffix = validate_suffix(suffix)
    url = f"http://trust.{suffix}/.well-known/localghost/root.pem"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return response.read()
    except urllib.error.URLError as exc:
        raise TailscaleError(
            f"could not download the localghost root from {url}"
        ) from exc


def _request(
    url: str, *, method: str, data: bytes | None, headers: dict[str, str]
) -> Any:
    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise TailscaleError(
            f"Tailscale API returned HTTP {exc.code}: {detail or exc.reason}"
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
