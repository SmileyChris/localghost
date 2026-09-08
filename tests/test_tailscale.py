import json
import stat
from io import BytesIO
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import click
import pytest
from click.testing import CliRunner
from keyring.errors import KeyringError

import localghost.cli as cli_module
import localghost.tailscale as tailscale_module
from localghost.cli import cli
from localghost.tailscale import (
    API,
    TailscaleError,
    TailscaleState,
    detect_suffix,
    load_state,
    save_state,
    suffix_is_public,
    validate_suffix,
)
from localghost.trust import PublicCertificate, TrustError

CERTIFICATE_PEM = b"""-----BEGIN CERTIFICATE-----
MAA=
-----END CERTIFICATE-----
"""


def test_state_round_trip_is_private(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    state = TailscaleState("example.com", "tail1234", ("100.64.0.1",), {"dns": []})

    save_state(state)

    assert load_state() == state
    assert stat.S_IMODE((tmp_path / "tailscale.json").stat().st_mode) == 0o600
    assert "secret" not in (tmp_path / "tailscale.json").read_text()


def test_invalid_saved_state_is_actionable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    (tmp_path / "tailscale.json").write_text("{}")
    with pytest.raises(TailscaleError, match="invalid saved"):
        load_state()


def test_absent_state_and_remove_are_idempotent(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    assert load_state() is None
    tailscale_module.remove_state()


@pytest.mark.parametrize("value", ["two.labels", "-bad", "bad-"])
def test_suffix_rejects_unsafe_values(value) -> None:
    with pytest.raises(ValueError, match="one DNS label"):
        validate_suffix(value)


@pytest.mark.parametrize(
    "value", ["localhost", "dev", "com", "app", "internal", "io", "uk", "test", "corp"]
)
def test_public_and_reserved_names_are_accepted_but_flagged(value) -> None:
    assert validate_suffix(value) == value
    assert suffix_is_public(value)


@pytest.mark.parametrize("value", ["tail1234", "taildc3ac3", "chris-laptop"])
def test_private_labels_are_not_public(value) -> None:
    assert not suffix_is_public(value)


def test_public_suffix_state_loads_as_http_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    save_state(TailscaleState("-", "work", ("100.64.0.1",), {}))

    state = load_state()

    assert state is not None
    assert state.https is False


def test_private_suffix_state_serves_https(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    save_state(TailscaleState("-", "tail1234", ("100.64.0.1",), {}))

    state = load_state()

    assert state is not None
    assert state.https is True


def test_detect_suffix_uses_short_search_domain(monkeypatch) -> None:
    monkeypatch.setattr(
        tailscale_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args[0],
            0,
            json.dumps({"SearchDomains": ["tail.example.ts.net.", "tail1234."]}),
            "",
        ),
    )
    assert detect_suffix() == "tail1234"


def test_detect_suffix_survives_null_search_domains(monkeypatch) -> None:
    monkeypatch.setattr(
        tailscale_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args[0],
            0,
            json.dumps(
                {
                    "SearchDomains": None,
                    "CurrentTailnet": {"MagicDNSSuffix": "taildc3ac3.ts.net"},
                }
            ),
            "",
        ),
    )
    assert detect_suffix() == "taildc3ac3"


def test_detect_suffix_prefers_the_tailnet_label(monkeypatch) -> None:
    monkeypatch.setattr(
        tailscale_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args[0],
            0,
            json.dumps(
                {
                    "SearchDomains": ["taildc3ac3.ts.net."],
                    "CurrentTailnet": {
                        "MagicDNSSuffix": "taildc3ac3.ts.net",
                        "SelfDNSName": "chris-laptop.taildc3ac3.ts.net.",
                    },
                }
            ),
            "",
        ),
    )
    assert detect_suffix() == "taildc3ac3"


def test_detect_suffix_skips_an_unusable_tailnet_label(monkeypatch) -> None:
    monkeypatch.setattr(
        tailscale_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args[0],
            0,
            json.dumps(
                {
                    "CurrentTailnet": {
                        "MagicDNSSuffix": "local.ts.net",
                        "SelfDNSName": "chris-laptop.example.ts.net.",
                    },
                }
            ),
            "",
        ),
    )
    assert detect_suffix() == "chris-laptop"


def test_detect_suffix_falls_back_to_this_machine_name(monkeypatch) -> None:
    monkeypatch.setattr(
        tailscale_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args[0],
            0,
            json.dumps(
                {"CurrentTailnet": {"SelfDNSName": "chris-laptop.example.ts.net."}}
            ),
            "",
        ),
    )
    assert detect_suffix() == "chris-laptop"


def test_suffix_length_leaves_room_for_the_gateway_hostname() -> None:
    assert validate_suffix("a" * 52) == "a" * 52
    with pytest.raises(ValueError, match="52"):
        validate_suffix("a" * 53)


def test_detect_suffix_requests_explicit_value_without_cli(monkeypatch) -> None:
    monkeypatch.setattr(
        tailscale_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    with pytest.raises(TailscaleError, match="pass --suffix"):
        detect_suffix()


@pytest.mark.parametrize(
    "result",
    [
        CompletedProcess([], 1, "", "failed"),
        CompletedProcess([], 0, "not json", ""),
        CompletedProcess([], 0, '{"SearchDomains":["local."]}', ""),
    ],
)
def test_detect_suffix_rejects_unusable_status(monkeypatch, result) -> None:
    monkeypatch.setattr(tailscale_module.subprocess, "run", lambda *a, **kw: result)
    with pytest.raises(TailscaleError, match="could not detect"):
        detect_suffix()


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size: int | None = None):
        return self.payload if size is None else self.payload[:size]


def test_api_auth_key_and_dns_calls(monkeypatch) -> None:
    requests = []
    responses = iter(
        [
            {"access_token": "token"},
            {"key": "tskey-auth-test"},
            {"corp": ["100.1.1.1"]},
            {},
        ]
    )

    def urlopen(request, timeout):
        requests.append(request)
        return Response(json.dumps(next(responses)).encode())

    monkeypatch.setattr(tailscale_module.urllib.request, "urlopen", urlopen)
    api = API.authenticate("client", "secret")
    assert api.create_auth_key("example.com", "tag:localghost") == "tskey-auth-test"
    assert api.split_dns("example.com") == {"corp": ["100.1.1.1"]}
    api.update_split_dns("example.com", {"tail1234": ["100.64.0.1"]})

    key_body = json.loads(requests[1].data)
    create = key_body["capabilities"]["devices"]["create"]
    assert create == {
        "reusable": False,
        "ephemeral": False,
        "preauthorized": True,
        "tags": ["tag:localghost"],
    }
    assert requests[3].method == "PATCH"
    assert requests[3].full_url.endswith("/dns/split-dns")


def test_api_reports_http_and_network_errors(monkeypatch) -> None:
    error = HTTPError("url", 500, "Server Error", {}, BytesIO(b"broken"))
    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(TailscaleError, match="HTTP 500: broken"):
        API("token").split_dns("-")


def test_auth_key_tag_rejection_points_at_the_policy_file(monkeypatch) -> None:
    error = HTTPError(
        "url",
        400,
        "Bad Request",
        {},
        BytesIO(b"requested tags [tag:localghost] are invalid or not permitted"),
    )
    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(TailscaleError, match="tagOwners") as excinfo:
        API("token").create_auth_key("-", "tag:localghost")
    assert "tag:localghost" in str(excinfo.value)
    assert "admin/acls" in str(excinfo.value)


def test_auth_key_forbidden_names_the_missing_scope(monkeypatch) -> None:
    error = HTTPError("url", 403, "Forbidden", {}, BytesIO(b"denied"))
    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(TailscaleError, match="auth_keys"):
        API("token").create_auth_key("-", "tag:localghost")


def test_dns_forbidden_names_the_missing_scope(monkeypatch) -> None:
    error = HTTPError("url", 403, "Forbidden", {}, BytesIO(b"denied"))
    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(TailscaleError, match="dns:write"):
        API("token").split_dns("-")
    with pytest.raises(TailscaleError, match="dns:write"):
        API("token").update_split_dns("-", {"tail1234": ["100.64.0.1"]})


def _fake_keyring(stored: dict) -> SimpleNamespace:
    return SimpleNamespace(
        set_password=lambda service, name, value: stored.__setitem__(
            (service, name), value
        ),
        get_password=lambda service, name: stored.get((service, name)),
        delete_password=lambda service, name: stored.pop((service, name), None),
    )


def test_credential_round_trip_uses_the_system_keyring(monkeypatch) -> None:
    stored: dict = {}
    monkeypatch.setattr(tailscale_module, "keyring", _fake_keyring(stored))
    assert tailscale_module.store_credential("id", "secret") is True
    assert stored
    assert all(service == "localghost-tailscale" for service, _ in stored)
    credential = tailscale_module.load_credential()
    assert credential == tailscale_module.Credential("id", "secret")
    tailscale_module.delete_credential()
    assert not stored
    assert tailscale_module.load_credential() is None


def test_credential_helpers_survive_a_missing_keyring_backend(monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise KeyringError("no backend")

    monkeypatch.setattr(
        tailscale_module,
        "keyring",
        SimpleNamespace(
            set_password=broken, get_password=broken, delete_password=broken
        ),
    )
    assert tailscale_module.store_credential("id", "secret") is False
    assert tailscale_module.load_credential() is None
    tailscale_module.delete_credential()


def test_api_rejects_missing_fields_and_unexpected_dns(monkeypatch) -> None:
    monkeypatch.setattr(tailscale_module, "_request", lambda *args, **kwargs: {})
    with pytest.raises(TailscaleError, match="no access token"):
        API.authenticate("id", "secret")
    with pytest.raises(TailscaleError, match="auth key"):
        API("token").create_auth_key("-", "tag:localghost")
    monkeypatch.setattr(tailscale_module, "_request", lambda *args, **kwargs: [])
    with pytest.raises(TailscaleError, match="unexpected Tailscale DNS"):
        API("token").split_dns("-")
    monkeypatch.setattr(
        tailscale_module, "_request", lambda *args, **kwargs: {"bad": "value"}
    )
    with pytest.raises(TailscaleError, match="unexpected Tailscale DNS"):
        API("token").split_dns("-")


def test_fetch_public_root_success_and_failure(monkeypatch) -> None:
    requests = []

    def open_root(request, **kwargs):
        requests.append(request)
        return Response(CERTIFICATE_PEM)

    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        open_root,
    )
    assert tailscale_module.fetch_public_root(
        "tail1234", ("100.64.0.10",)
    ) == CERTIFICATE_PEM
    assert requests[0].full_url.startswith("http://100.64.0.10/")
    assert requests[0].get_header("Host") == "trust.tail1234"
    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    with pytest.raises(TailscaleError, match="could not download"):
        tailscale_module.fetch_public_root("tail1234")


def test_fetch_public_root_rejects_an_oversized_response(monkeypatch) -> None:
    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: Response(b"x" * (65536 + 1)),
    )
    with pytest.raises(TailscaleError, match="oversized"):
        tailscale_module.fetch_public_root("tail1234", ("100.64.0.10",))


def test_delete_credential_attempts_every_name(monkeypatch) -> None:
    attempted: list = []

    def delete(service, name):
        attempted.append(name)
        raise KeyringError("locked")

    monkeypatch.setattr(
        tailscale_module,
        "keyring",
        SimpleNamespace(delete_password=delete),
    )
    tailscale_module.delete_credential()
    assert attempted == ["client-id", "client-secret"]


@pytest.mark.parametrize("payload, message", [(b"", None), (b"bad", "invalid JSON")])
def test_request_handles_empty_and_invalid_responses(
    monkeypatch, payload, message
) -> None:
    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: Response(payload),
    )
    if message:
        with pytest.raises(TailscaleError, match=message):
            tailscale_module._request(
                "http://test", method="GET", data=None, headers={}
            )
    else:
        assert tailscale_module._request(
            "http://test", method="GET", data=None, headers={}
        ) == {}

    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    with pytest.raises(TailscaleError, match="offline"):
        API("token").split_dns("-")


def test_status_reports_enabled_state(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )
    monkeypatch.setattr(cli_module, "_unmirrored_router_names", lambda: [])
    result = CliRunner().invoke(cli, ["tailscale", "status"])
    assert result.exit_code == 0, result.output
    assert "Route suffix: .tail1234" in result.output
    assert "Localhost-only routers" not in result.output


def test_status_reports_unmirrored_routers(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )
    monkeypatch.setattr(cli_module, "_tailscale_gateway_health", lambda: "healthy")
    monkeypatch.setattr(
        cli_module, "_unmirrored_router_names", lambda: ["shop-web", "api"]
    )
    result = CliRunner().invoke(cli, ["tailscale", "status"])
    assert result.exit_code == 0, result.output
    assert "Localhost-only routers: shop-web, api" in result.output


def test_unmirrored_routers_are_read_from_traefik_logs(monkeypatch) -> None:
    def run(command, **kwargs):
        if command[:2] == ["docker", "ps"]:
            return CompletedProcess(command, 0, "abc123\n", "")
        assert command[:2] == ["docker", "logs"]
        stderr = (
            "localghostCA[x]: router shop-web cannot be mirrored without "
            "explicit service and entrypoints\n"
            "localghostCA[x]: router shop-web cannot be mirrored without "
            "explicit service and entrypoints\n"
            "localghostCA[x]: publishing complete snapshot (2 certificates, "
            "1 routers)\n"
        )
        return CompletedProcess(command, 0, "", stderr)

    monkeypatch.setattr(cli_module.subprocess, "run", run)
    assert cli_module._unmirrored_router_names() == ["shop-web"]


def test_unmirrored_routers_are_empty_without_docker(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert cli_module._unmirrored_router_names() == []


def test_status_reports_disabled_state(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: None)
    result = CliRunner().invoke(cli, ["tailscale", "status"])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output


def test_status_reports_invalid_state(monkeypatch) -> None:
    def broken():
        raise TailscaleError("invalid saved state")

    monkeypatch.setattr(cli_module, "load_tailscale_state", broken)
    result = CliRunner().invoke(cli, ["tailscale", "status"])
    assert result.exit_code != 0
    assert "invalid saved state" in result.output


def test_gateway_health_is_unknown_when_docker_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess([], 1, "", "denied"),
    )
    assert cli_module._tailscale_gateway_health() == "unknown"


def _patch_enable(monkeypatch, events, saved, state_dir):
    class FakeAPI:
        @classmethod
        def authenticate(cls, client_id, client_secret):
            events.append((client_id, client_secret))
            return cls()

        def create_auth_key(self, tailnet, tag):
            return "auth-key"

        def split_dns(self, tailnet):
            return {"corp": ["100.1.1.1"]}

        def update_split_dns(self, tailnet, value):
            events.append((tailnet, value))

    monkeypatch.delenv("TAILSCALE_CLIENT_ID", raising=False)
    monkeypatch.delenv("TAILSCALE_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(state_dir))
    monkeypatch.setattr(cli_module, "TailscaleAPI", FakeAPI)
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: None)
    monkeypatch.setattr(cli_module, "_https_configured", lambda: False)
    monkeypatch.setattr(cli_module, "_bootstrap_public_root", lambda: None)
    monkeypatch.setattr(
        cli_module,
        "_bootstrap_tailnet_root",
        lambda suffix: PublicCertificate.parse(CERTIFICATE_PEM),
    )
    monkeypatch.setattr(
        cli_module, "_bootstrap_tailscale_gateway", lambda suffix, key: ("100.64.0.1",)
    )
    monkeypatch.setattr(cli_module, "_ensure_gateway_image", lambda suffix: None)
    monkeypatch.setattr(cli_module, "save_tailscale_state", saved.append)
    monkeypatch.setattr(
        cli_module, "_run_proxy", lambda *args, **kwargs: events.append(kwargs)
    )
    monkeypatch.setattr(
        cli_module, "load_tailscale_credential", lambda: None
    )
    monkeypatch.setattr(
        cli_module, "store_tailscale_credential", lambda *args: True
    )


ENABLE_ARGS = [
    "tailscale", "enable", "--tailnet", "example.com", "--suffix", "tail1234"
]
CREDENTIAL_ARGS = ["--client-id", "id", "--client-secret", "secret"]


def test_enable_uses_ephemeral_credentials_and_saves_public_state(
    monkeypatch, tmp_path
) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert saved[0].suffix == "tail1234"
    assert not hasattr(saved[0], "client_secret")
    assert events[-1] == {"https_enabled": True, "force_recreate": True}


def test_enable_with_a_public_suffix_skips_the_tailnet_authority(
    monkeypatch, tmp_path
) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)
    monkeypatch.setattr(
        cli_module,
        "_bootstrap_tailnet_root",
        lambda suffix: pytest.fail("a public suffix must not get a root"),
    )
    monkeypatch.setattr(
        cli_module,
        "_install_tailnet_trust",
        lambda *args, **kwargs: pytest.fail("nothing to trust"),
    )

    args = ["tailscale", "enable", "--tailnet", "example.com", "--suffix", "work"]
    result = CliRunner().invoke(cli, args + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert saved[0].suffix == "work"
    assert saved[0].https is False
    assert events[-1] == {"https_enabled": False, "force_recreate": True}
    assert "http://<project>.work" in result.output
    assert "HTTP only" in result.output
    assert "tailscale trust" not in result.output
    assert not (tmp_path / "tailscale-work-rootCA.pem").exists()


def test_enable_stores_the_credential_in_the_keyring(monkeypatch, tmp_path) -> None:
    events: list = []
    saved: list = []
    stored: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)
    monkeypatch.setattr(
        cli_module,
        "store_tailscale_credential",
        lambda client_id, client_secret: stored.append((client_id, client_secret))
        or True,
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert stored == [("id", "secret")]


def test_enable_warns_when_keyring_storage_fails(monkeypatch, tmp_path) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)
    monkeypatch.setattr(
        cli_module, "store_tailscale_credential", lambda *args: False
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert "keyring" in result.output


def test_enable_uses_the_stored_credential(monkeypatch, tmp_path) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_credential",
        lambda: tailscale_module.Credential("kid", "ksecret"),
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS)
    assert result.exit_code == 0, result.output
    assert ("kid", "ksecret") in events
    assert "keyring" in result.output


def test_enable_without_credential_guides_setup_then_prompts(
    monkeypatch, tmp_path
) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)

    result = CliRunner().invoke(cli, ENABLE_ARGS, input="typed-id\ntyped-secret\n")
    assert result.exit_code == 0, result.output
    assert ("typed-id", "typed-secret") in events
    assert "tagOwners" in result.output
    assert "tag:localghost" in result.output
    assert "admin/acls" in result.output
    assert "admin/settings/oauth" in result.output
    assert "auth_keys" in result.output
    assert "dns:write" in result.output


def test_enable_refuses_when_already_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )
    result = CliRunner().invoke(cli, ["tailscale", "enable"])
    assert result.exit_code != 0
    assert "already enabled" in result.output


def test_enable_requires_a_tag_prefix(monkeypatch, tmp_path) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)

    result = CliRunner().invoke(
        cli, ENABLE_ARGS + ["--tag", "localghost"] + CREDENTIAL_ARGS
    )
    assert result.exit_code != 0
    assert "tag:" in result.output


def test_enable_installs_tailnet_trust_on_a_trusted_client(
    monkeypatch, tmp_path
) -> None:
    events: list = []
    saved: list = []
    installed: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)
    monkeypatch.setattr(cli_module, "_https_configured", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "_install_tailnet_trust",
        lambda suffix, **kwargs: installed.append(suffix),
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert installed == ["tail1234"]
    assert "trusted on this client" in result.output


def test_enable_warns_when_tailnet_trust_installation_fails(
    monkeypatch, tmp_path
) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)
    monkeypatch.setattr(cli_module, "_https_configured", lambda: True)

    def failing_install(suffix, **kwargs):
        raise click.ClickException("mkcert is unavailable")

    monkeypatch.setattr(cli_module, "_install_tailnet_trust", failing_install)

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert "Tailnet trust was not installed" in result.output
    assert "localghost trust" in result.output


def test_enable_creates_the_auth_key_after_the_image_builds(
    monkeypatch, tmp_path
) -> None:
    events: list = []
    saved: list = []
    order: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)
    monkeypatch.setattr(
        cli_module.TailscaleAPI,
        "create_auth_key",
        lambda self, tailnet, tag: order.append("auth-key") or "auth-key",
    )
    monkeypatch.setattr(
        cli_module,
        "_bootstrap_tailnet_root",
        lambda suffix: order.append("bootstrap")
        or PublicCertificate.parse(CERTIFICATE_PEM),
    )
    monkeypatch.setattr(
        cli_module, "_ensure_gateway_image", lambda suffix: order.append("build")
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert order == ["bootstrap", "build", "auth-key"]


def test_enable_prints_the_share_command(monkeypatch, tmp_path) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    fingerprint = PublicCertificate.parse(CERTIFICATE_PEM).fingerprint
    assert (
        f"localghost tailscale trust tail1234 --fingerprint {fingerprint}"
        in result.output
    )


def test_enable_saves_the_tailnet_root_for_later_status(monkeypatch, tmp_path) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert (
        tmp_path / "tailscale-tail1234-rootCA.pem"
    ).read_bytes() == PublicCertificate.parse(CERTIFICATE_PEM).pem


def test_enable_refuses_an_actively_mapped_suffix(monkeypatch, tmp_path) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)
    monkeypatch.setattr(
        cli_module.TailscaleAPI,
        "split_dns",
        lambda self, tailnet: {"tail1234": ["100.9.9.9"]},
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code != 0
    assert "100.9.9.9" in result.output
    assert "--takeover" in result.output
    assert saved == []


def test_enable_takeover_replaces_an_active_mapping(monkeypatch, tmp_path) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)
    monkeypatch.setattr(
        cli_module.TailscaleAPI,
        "split_dns",
        lambda self, tailnet: {"tail1234": ["100.9.9.9"]},
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS + ["--takeover"] + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert ("example.com", {"tail1234": ["100.64.0.1"]}) in events


def test_enable_rolls_back_when_the_hub_fails_to_start(monkeypatch, tmp_path) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)
    monkeypatch.setattr(
        cli_module,
        "_run_proxy",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            click.ClickException("compose failed")
        ),
    )
    monkeypatch.setattr(
        cli_module, "remove_tailscale_state", lambda: events.append("state-removed")
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code != 0
    restore = ("example.com", {"tail1234": None})
    assert restore in events
    assert "state-removed" in events
    assert events.index(restore) < events.index("state-removed")


def test_enable_rollback_warns_when_dns_restore_fails(monkeypatch, tmp_path) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved, tmp_path)

    calls = {"count": 0}

    def failing_update(self, tailnet, value):
        calls["count"] += 1
        raise TailscaleError("api offline")

    monkeypatch.setattr(cli_module.TailscaleAPI, "update_split_dns", failing_update)
    monkeypatch.setattr(
        cli_module, "remove_tailscale_state", lambda: events.append("state-removed")
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code != 0
    assert calls["count"] == 2
    assert "Tailnet DNS was not restored" in result.output
    assert "state-removed" in events


def test_status_prints_the_share_command(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    (tmp_path / "tailscale-tail1234-rootCA.pem").write_bytes(CERTIFICATE_PEM)
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )
    monkeypatch.setattr(cli_module, "_tailscale_gateway_health", lambda: "healthy")
    monkeypatch.setattr(cli_module, "_unmirrored_router_names", lambda: [])

    result = CliRunner().invoke(cli, ["tailscale", "status"])
    assert result.exit_code == 0, result.output
    fingerprint = PublicCertificate.parse(CERTIFICATE_PEM).fingerprint
    assert (
        f"localghost tailscale trust tail1234 --fingerprint {fingerprint}"
        in result.output
    )


def test_disable_uses_the_stored_credential_and_deletes_it(monkeypatch) -> None:
    events: list = []
    state = TailscaleState(
        "example.com", "tail1234", ("100.64.0.1",), {"corp": ["100.1.1.1"]}
    )

    class FakeAPI:
        @classmethod
        def authenticate(cls, client_id, client_secret):
            events.append((client_id, client_secret))
            return cls()

        def update_split_dns(self, tailnet, value):
            events.append((tailnet, value))

    monkeypatch.delenv("TAILSCALE_CLIENT_ID", raising=False)
    monkeypatch.delenv("TAILSCALE_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(cli_module, "TailscaleAPI", FakeAPI)
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    monkeypatch.setattr(
        cli_module, "remove_tailscale_state", lambda: events.append("removed")
    )
    monkeypatch.setattr(cli_module, "proxy_is_running", lambda: False)
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_credential",
        lambda: tailscale_module.Credential("kid", "ksecret"),
    )
    monkeypatch.setattr(
        cli_module, "delete_tailscale_credential", lambda: events.append("deleted")
    )

    result = CliRunner().invoke(cli, ["tailscale", "disable"])
    assert result.exit_code == 0, result.output
    assert events[0] == ("kid", "ksecret")
    assert "deleted" in events
    assert events.index("deleted") > events.index("removed")


def test_disable_notes_that_other_machines_still_trust_the_root(monkeypatch) -> None:
    state = TailscaleState("example.com", "tail1234", ("100.64.0.1",), {})

    class FakeAPI:
        @classmethod
        def authenticate(cls, client_id, client_secret):
            return cls()

        def update_split_dns(self, tailnet, value):
            return None

    monkeypatch.delenv("TAILSCALE_CLIENT_ID", raising=False)
    monkeypatch.delenv("TAILSCALE_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(cli_module, "TailscaleAPI", FakeAPI)
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    monkeypatch.setattr(cli_module, "remove_tailscale_state", lambda: None)
    monkeypatch.setattr(cli_module, "proxy_is_running", lambda: False)
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_credential",
        lambda: tailscale_module.Credential("kid", "ksecret"),
    )
    monkeypatch.setattr(cli_module, "delete_tailscale_credential", lambda: None)

    result = CliRunner().invoke(cli, ["tailscale", "disable"])
    assert result.exit_code == 0, result.output
    assert "still trust" in result.output
    assert "localghost trust remove" in result.output


def test_disable_requires_enabled_state(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: None)
    result = CliRunner().invoke(cli, ["tailscale", "disable"])
    assert result.exit_code != 0
    assert "not enabled" in result.output


def test_disable_reports_invalid_state(monkeypatch) -> None:
    def broken():
        raise TailscaleError("invalid saved state")

    monkeypatch.setattr(cli_module, "load_tailscale_state", broken)
    result = CliRunner().invoke(cli, ["tailscale", "disable"])
    assert result.exit_code != 0
    assert "invalid saved state" in result.output


def test_disable_reports_api_failure(monkeypatch) -> None:
    class FailingAPI:
        @classmethod
        def authenticate(cls, client_id, client_secret):
            raise TailscaleError("could not reach the Tailscale API")

    monkeypatch.delenv("TAILSCALE_CLIENT_ID", raising=False)
    monkeypatch.delenv("TAILSCALE_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(cli_module, "TailscaleAPI", FailingAPI)
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_credential",
        lambda: tailscale_module.Credential("kid", "ksecret"),
    )

    result = CliRunner().invoke(cli, ["tailscale", "disable"])
    assert result.exit_code != 0
    assert "could not reach the Tailscale API" in result.output


def test_trust_alias_defaults_to_the_active_suffix(monkeypatch) -> None:
    installed: list = []
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )
    monkeypatch.setattr(
        cli_module,
        "_install_tailnet_trust",
        lambda suffix, **kwargs: installed.append((suffix, kwargs)),
    )

    result = CliRunner().invoke(cli, ["tailscale", "trust"])
    assert result.exit_code == 0, result.output
    assert installed == [("tail1234", {"expected_fingerprint": None})]


def test_trust_alias_requires_enabled_state(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: None)
    result = CliRunner().invoke(cli, ["tailscale", "trust"])
    assert result.exit_code != 0
    assert "not enabled" in result.output


def test_run_proxy_repairs_authorities_after_a_failed_up(monkeypatch) -> None:
    state = TailscaleState("example.com", "tail1234", ("100.64.0.1",), {})
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    repaired: list = []
    monkeypatch.setattr(
        cli_module, "_bootstrap_public_root", lambda: repaired.append("localhost")
    )
    monkeypatch.setattr(
        cli_module,
        "_bootstrap_tailnet_root",
        lambda suffix: repaired.append(suffix),
    )
    attempts = {"count": 0}

    def run(command, **kwargs):
        if command[:2] != ["docker", "compose"]:
            return CompletedProcess(command, 0, "", "")
        attempts["count"] += 1
        code = 1 if attempts["count"] == 1 else 0
        return CompletedProcess(command, code, "", "bootstrap public root is missing")

    monkeypatch.setattr(cli_module.subprocess, "run", run)

    cli_module._run_proxy("up", https_enabled=True)
    assert repaired == ["localhost", "tail1234"]
    assert attempts["count"] == 2


def test_run_proxy_reports_the_original_failure_when_repair_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: None)

    def broken_bootstrap():
        raise click.ClickException("docker is required")

    monkeypatch.setattr(cli_module, "_bootstrap_public_root", broken_bootstrap)
    attempts = {"count": 0}

    def run(command, **kwargs):
        if command[:2] != ["docker", "compose"]:
            return CompletedProcess(command, 0, "", "")
        attempts["count"] += 1
        return CompletedProcess(command, 1, "", "original failure")

    monkeypatch.setattr(cli_module.subprocess, "run", run)

    with pytest.raises(click.exceptions.Exit):
        cli_module._run_proxy("up", https_enabled=True)
    assert attempts["count"] == 1


def test_down_survives_corrupt_tailnet_state(monkeypatch) -> None:
    def broken():
        raise TailscaleError("invalid saved Tailscale state")

    monkeypatch.setattr(cli_module, "load_tailscale_state", broken)
    commands: list = []
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command)
        or CompletedProcess(command, 0, "", ""),
    )

    cli_module._run_proxy("down")
    assert any("down" in command for command in commands)

    with pytest.raises(click.ClickException):
        cli_module._run_proxy("up")


def test_trust_continues_when_the_tailnet_root_is_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "_https_configured", lambda: False)
    monkeypatch.setattr(cli_module, "proxy_is_running", lambda: True)
    monkeypatch.setattr(cli_module, "_enable_https", lambda: None)
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )

    def failing_install(suffix, **kwargs):
        raise click.ClickException("could not download the localghost root")

    monkeypatch.setattr(cli_module, "_install_tailnet_trust", failing_install)
    ups: list = []
    monkeypatch.setattr(
        cli_module, "_run_proxy", lambda *args, **kwargs: ups.append(kwargs)
    )

    result = CliRunner().invoke(cli, ["trust", "install"])
    assert result.exit_code == 0, result.output
    assert "Tailnet trust was not installed" in result.output
    assert ups and ups[0]["https_enabled"] is True


def test_remove_trust_keeps_the_hub_on_https_for_the_tailnet(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "_https_configured", lambda: True)
    monkeypatch.setattr(cli_module, "proxy_is_running", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )
    monkeypatch.setattr(cli_module, "_trust_marker", lambda: Path("/nonexistent"))
    monkeypatch.setattr(cli_module, "_public_root_path", lambda: Path("/nonexistent"))
    monkeypatch.setattr(
        cli_module, "_state_directory", lambda: Path("/nonexistent-state")
    )
    downgrades: list = []
    monkeypatch.setattr(
        cli_module, "_run_proxy", lambda *args, **kwargs: downgrades.append(kwargs)
    )

    result = CliRunner().invoke(cli, ["trust", "remove"])
    assert result.exit_code == 0, result.output
    assert downgrades == []
    assert "tailnet" in result.output.lower()


def test_proxy_origin_reports_https_when_the_tailnet_forces_it(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli_module, "_https_configured", lambda: False)
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )
    assert cli_module._proxy_origin("shop") == "https://shop.localhost"


def test_tailnet_origin_report_can_target_stderr(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )
    calls: list = []
    monkeypatch.setattr(
        cli_module,
        "info",
        lambda message, **kwargs: calls.append(kwargs.get("err", False)),
    )
    cli_module._report_tailnet_origin("shop", err=True)
    assert calls == [True]


def test_dry_run_plan_reports_the_tailnet_origin_on_stderr(monkeypatch) -> None:
    reported: list = []
    monkeypatch.setattr(
        cli_module,
        "_report_tailnet_origin",
        lambda hostname, err=False: reported.append(err),
    )
    monkeypatch.setattr(cli_module, "_proxy_origin", lambda name: "http://x.localhost")
    plan = SimpleNamespace(
        name="x",
        type="vite",
        command=("npm", "run", "dev"),
        port=5173,
        project_root=None,
        working_directory=None,
        bridge_yaml="services: {}\n",
    )
    cli_module._print_run_plan(plan, dry_run=True)
    assert reported == [True]


def test_up_tolerates_an_unready_gateway(monkeypatch) -> None:
    state = TailscaleState("example.com", "tail1234", ("100.64.0.1",), {})
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    monkeypatch.setattr(cli_module, "_only_the_gateway_failed", lambda: True)

    def run(command, **kwargs):
        if command[:2] != ["docker", "compose"]:
            return CompletedProcess(command, 0, "", "")
        return CompletedProcess(command, 1, "", "gateway timed out")

    monkeypatch.setattr(cli_module.subprocess, "run", run)

    cli_module._run_proxy("up", https_enabled=True)


def test_only_the_gateway_failed_checks_both_services(monkeypatch) -> None:
    health = {"traefik": "healthy", "tailscale-gateway": "starting"}
    monkeypatch.setattr(
        cli_module, "_service_health", lambda service: health[service]
    )
    assert cli_module._only_the_gateway_failed() is True
    health["traefik"] = "unhealthy"
    assert cli_module._only_the_gateway_failed() is False
    health.update({"traefik": "healthy", "tailscale-gateway": "healthy"})
    assert cli_module._only_the_gateway_failed() is False


def test_run_proxy_adds_tailnet_overlay(monkeypatch) -> None:
    commands = []
    state = TailscaleState("example.com", "tail1234", ("100.64.0.1",), {})
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda command, **kwargs: commands.append((command, kwargs))
        or CompletedProcess(command, 0, "", ""),
    )

    cli_module._run_proxy("up")

    command, kwargs = next(
        entry for entry in commands if entry[0][:2] == ["docker", "compose"]
    )
    assert "proxy_compose_https.yaml" in " ".join(command)
    assert "proxy_compose_tailscale.yaml" in " ".join(command)
    assert "proxy_compose_tailscale_https.yaml" in " ".join(command)
    assert kwargs["env"]["LOCALGHOST_TAILSCALE_SUFFIX"] == "tail1234"
    assert kwargs["env"]["LOCALGHOST_LOCALHOST_CA_MODE"] == "tls"
    assert kwargs["env"]["LOCALGHOST_TAILNET_CA_MODE"] == "tls"


PUBLIC_STATE = TailscaleState("example.com", "work", ("100.64.0.1",), {})


def test_run_proxy_keeps_a_public_suffix_on_http(monkeypatch) -> None:
    commands = []
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: PUBLIC_STATE)
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda command, **kwargs: (
            commands.append((command, kwargs)) or CompletedProcess(command, 0, "", "")
        ),
    )

    cli_module._run_proxy("up")

    command, kwargs = next(
        entry for entry in commands if entry[0][:2] == ["docker", "compose"]
    )
    joined = " ".join(command)
    assert "proxy_compose_tailscale.yaml" in joined
    assert "proxy_compose_https.yaml" not in joined
    assert "proxy_compose_tailscale_https.yaml" not in joined
    assert kwargs["env"]["LOCALGHOST_TAILSCALE_SUFFIX"] == "work"
    assert kwargs["env"]["LOCALGHOST_LOCALHOST_CA_MODE"] == "http"
    assert kwargs["env"]["LOCALGHOST_TAILNET_CA_MODE"] == "http"


def test_run_proxy_keeps_localhost_https_beside_a_public_suffix(monkeypatch) -> None:
    commands = []
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: PUBLIC_STATE)
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda command, **kwargs: commands.append((command, kwargs))
        or CompletedProcess(command, 0, "", ""),
    )

    cli_module._run_proxy("up", https_enabled=True)

    command, kwargs = next(
        entry for entry in commands if entry[0][:2] == ["docker", "compose"]
    )
    joined = " ".join(command)
    assert "proxy_compose_https.yaml" in joined
    assert "proxy_compose_tailscale.yaml" in joined
    assert "proxy_compose_tailscale_https.yaml" not in joined
    assert kwargs["env"]["LOCALGHOST_LOCALHOST_CA_MODE"] == "tls"
    assert kwargs["env"]["LOCALGHOST_TAILNET_CA_MODE"] == "http"


def test_public_suffix_does_not_force_localhost_https(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "_https_configured", lambda: False)
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: PUBLIC_STATE)
    assert cli_module._tailnet_forces_https() is False
    assert cli_module._proxy_origin("shop") == "http://shop.localhost"
    assert cli_module._tailnet_origin("shop") == "http://shop.work"


def test_public_suffix_status_reports_http_only(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: PUBLIC_STATE)
    monkeypatch.setattr(cli_module, "_tailscale_gateway_health", lambda: "healthy")
    monkeypatch.setattr(cli_module, "_unmirrored_router_names", lambda: [])
    result = CliRunner().invoke(cli, ["tailscale", "status"])
    assert result.exit_code == 0, result.output
    assert "Route suffix: .work" in result.output
    assert "HTTP only" in result.output
    assert "public" in result.output
    assert "tailscale trust" not in result.output


def test_trust_alias_has_nothing_to_install_for_a_public_suffix(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: PUBLIC_STATE)
    monkeypatch.setattr(
        cli_module,
        "_install_tailnet_trust",
        lambda *args, **kwargs: pytest.fail("no root exists to install"),
    )
    result = CliRunner().invoke(cli, ["tailscale", "trust"])
    assert result.exit_code != 0
    assert "HTTP only" in result.output


def test_hub_up_notes_a_public_suffix(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: PUBLIC_STATE)
    monkeypatch.setattr(cli_module, "_https_configured", lambda: False)
    monkeypatch.setattr(cli_module, "_ensure_https_or_warn", lambda: False)
    monkeypatch.setattr(cli_module, "proxy_is_running", lambda: False)
    monkeypatch.setattr(cli_module, "_managed_image_is_available", lambda: True)
    events: list = []
    monkeypatch.setattr(
        cli_module, "_run_proxy", lambda *args, **kwargs: events.append(kwargs)
    )
    result = CliRunner().invoke(cli, ["hub", "up"])
    assert result.exit_code == 0, result.output
    assert events[0]["https_enabled"] is False
    assert "HTTP only" in result.output
    assert "http://traefik.work" in result.output


def test_bootstrap_tailnet_root_builds_the_expected_one_shot(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or CompletedProcess(command, 0, CERTIFICATE_PEM, b""),
    )

    certificate = cli_module._bootstrap_tailnet_root("tail1234")

    command, kwargs = calls[0]
    assert certificate.pem == CERTIFICATE_PEM
    assert "--suffix=tail1234" in command
    assert "--signer-path=/var/lib/localghost-tailnet-ca" in command
    assert kwargs["env"]["LOCALGHOST_TAILSCALE_SUFFIX"] == "tail1234"


def test_bootstrap_gateway_passes_key_only_on_stdin(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or CompletedProcess(
            command,
            0,
            b"IPv4=100.64.0.1\nIPv6=fd7a:115c:a1e0::1\n",
            b"",
        ),
    )

    addresses = cli_module._bootstrap_tailscale_gateway("tail1234", "secret-key")

    command, kwargs = calls[0]
    assert addresses == ("100.64.0.1", "fd7a:115c:a1e0::1")
    assert "secret-key" not in command
    assert kwargs["input"] == b"secret-key"


def test_bootstrap_gateway_parses_only_labelled_addresses(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(
            command,
            0,
            b"dialing 9.9.9.9 for control\n"
            b"IPv4=100.64.0.1\n"
            b"IPv6=invalid\n",
            b"",
        ),
    )
    addresses = cli_module._bootstrap_tailscale_gateway("tail1234", "key")
    assert addresses == ("100.64.0.1",)


def test_bootstrap_helpers_report_compose_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 2, b"", b"failed"),
    )
    with pytest.raises(TailscaleError, match="failed"):
        cli_module._bootstrap_tailnet_root("tail1234")
    with pytest.raises(TailscaleError, match="failed"):
        cli_module._bootstrap_tailscale_gateway("tail1234", "key")


def test_disable_restores_exact_dns_then_reconciles(monkeypatch) -> None:
    events = []
    state = TailscaleState(
        "example.com", "tail1234", ("100.64.0.1",), {"corp": ["100.1.1.1"]}
    )

    class FakeAPI:
        @classmethod
        def authenticate(cls, client_id, client_secret):
            return cls()

        def update_split_dns(self, tailnet, value):
            events.append((tailnet, value))

    monkeypatch.setattr(cli_module, "TailscaleAPI", FakeAPI)
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    monkeypatch.setattr(
        cli_module, "remove_tailscale_state", lambda: events.append("removed")
    )
    monkeypatch.setattr(cli_module, "proxy_is_running", lambda: True)
    monkeypatch.setattr(cli_module, "_https_configured", lambda: True)
    monkeypatch.setattr(
        cli_module, "_run_proxy", lambda *args, **kwargs: events.append((args, kwargs))
    )
    monkeypatch.setattr(cli_module, "delete_tailscale_credential", lambda: None)
    result = CliRunner().invoke(
        cli,
        [
            "tailscale",
            "disable",
            "--client-id",
            "id",
            "--client-secret",
            "secret",
        ],
    )
    assert result.exit_code == 0, result.output
    assert events[0] == (
        "example.com",
        {"tail1234": None},
    )
    assert events[1] == "removed"


def test_tailnet_trust_downloads_and_installs_both_stores(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    installed = []

    class Installer:
        def __init__(self, path, **kwargs):
            self.path = path

        def install(self, **kwargs):
            installed.append(self.path)

    monkeypatch.setattr(
        cli_module, "fetch_tailscale_root", lambda suffix, gateway_ips: CERTIFICATE_PEM
    )
    monkeypatch.setattr(cli_module, "MkcertInstaller", Installer)
    monkeypatch.setattr(cli_module, "ZenNssInstaller", Installer)
    result = CliRunner().invoke(cli, ["tailscale", "trust", "tail1234"])
    assert result.exit_code == 0, result.output
    assert len(installed) == 2
    assert (tmp_path / "tailscale-tail1234-rootCA.pem").read_bytes() == CERTIFICATE_PEM


def test_tailnet_trust_accepts_a_matching_pinned_fingerprint(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    installed = []
    fingerprint = PublicCertificate.parse(CERTIFICATE_PEM).fingerprint

    class Installer:
        def __init__(self, path, **kwargs):
            self.path = path

        def install(self, **kwargs):
            installed.append(self.path)

    monkeypatch.setattr(
        cli_module, "fetch_tailscale_root", lambda suffix, gateway_ips: CERTIFICATE_PEM
    )
    monkeypatch.setattr(cli_module, "MkcertInstaller", Installer)
    monkeypatch.setattr(cli_module, "ZenNssInstaller", Installer)

    # The hub prints SHA256:ABCD…; a pasted fingerprint may be lower case or
    # colon-separated.
    spelled = ":".join(
        fingerprint.removeprefix("SHA256:").lower()[index : index + 2]
        for index in range(0, 64, 2)
    )
    result = CliRunner().invoke(
        cli, ["tailscale", "trust", "tail1234", "--fingerprint", spelled]
    )

    assert result.exit_code == 0, result.output
    assert len(installed) == 2


def test_tailnet_trust_refuses_a_root_with_another_fingerprint(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))

    class Installer:
        def __init__(self, path, **kwargs):
            self.path = path

        def install(self, **kwargs):
            pytest.fail("an unverified root was installed")

    monkeypatch.setattr(
        cli_module, "fetch_tailscale_root", lambda suffix, gateway_ips: CERTIFICATE_PEM
    )
    monkeypatch.setattr(cli_module, "MkcertInstaller", Installer)
    monkeypatch.setattr(cli_module, "ZenNssInstaller", Installer)

    expected = "SHA256:" + "B" * 64
    result = CliRunner().invoke(
        cli, ["tailscale", "trust", "tail1234", "--fingerprint", expected]
    )

    assert result.exit_code != 0
    assert "does not match" in result.output
    assert PublicCertificate.parse(CERTIFICATE_PEM).fingerprint in result.output
    assert not (tmp_path / "tailscale-tail1234-rootCA.pem").exists()


def test_tailnet_trust_rejects_an_unreadable_fingerprint(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli_module,
        "fetch_tailscale_root",
        lambda suffix, gateway_ips: pytest.fail("downloaded before validation"),
    )

    result = CliRunner().invoke(
        cli, ["tailscale", "trust", "tail1234", "--fingerprint", "nonsense"]
    )

    assert result.exit_code != 0
    assert "64 hexadecimal characters" in result.output


def test_tailnet_trust_removes_a_superseded_root_before_installing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    previous = CERTIFICATE_PEM.replace(b"MAA=", b"MAE=")
    path = tmp_path / "tailscale-tail1234-rootCA.pem"
    path.write_bytes(previous)
    events = []

    class Installer:
        def __init__(self, path, **kwargs):
            self.path = path

        def install(self, **kwargs):
            events.append(("install", self.path.read_bytes()))

        def uninstall(self):
            events.append(("uninstall", self.path.read_bytes()))

    monkeypatch.setattr(
        cli_module, "fetch_tailscale_root", lambda suffix, gateway_ips: CERTIFICATE_PEM
    )
    monkeypatch.setattr(cli_module, "MkcertInstaller", Installer)
    monkeypatch.setattr(cli_module, "ZenNssInstaller", Installer)

    result = CliRunner().invoke(cli, ["tailscale", "trust", "tail1234"])

    assert result.exit_code == 0, result.output
    # The superseded root must leave the trust stores while its own bytes are
    # still on disk; afterwards the new root is written and installed.
    assert events == [
        ("uninstall", previous),
        ("uninstall", previous),
        ("install", CERTIFICATE_PEM),
        ("install", CERTIFICATE_PEM),
    ]
    assert path.read_bytes() == CERTIFICATE_PEM
    assert "replaced" in result.output.lower()


def test_tailnet_trust_keeps_an_unchanged_root_installed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    (tmp_path / "tailscale-tail1234-rootCA.pem").write_bytes(CERTIFICATE_PEM)

    class Installer:
        def __init__(self, path, **kwargs):
            self.path = path

        def install(self, **kwargs):
            return None

        def uninstall(self):
            pytest.fail("an unchanged root was removed")

    monkeypatch.setattr(
        cli_module, "fetch_tailscale_root", lambda suffix, gateway_ips: CERTIFICATE_PEM
    )
    monkeypatch.setattr(cli_module, "MkcertInstaller", Installer)
    monkeypatch.setattr(cli_module, "ZenNssInstaller", Installer)

    result = CliRunner().invoke(cli, ["tailscale", "trust", "tail1234"])

    assert result.exit_code == 0, result.output


def test_tailnet_trust_warns_when_a_superseded_root_cannot_be_removed(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    path = tmp_path / "tailscale-tail1234-rootCA.pem"
    path.write_bytes(CERTIFICATE_PEM.replace(b"MAA=", b"MAE="))
    installed = []

    class Installer:
        def __init__(self, path, **kwargs):
            self.path = path

        def install(self, **kwargs):
            installed.append(self.path)

        def uninstall(self):
            raise TrustError("store is locked")

    monkeypatch.setattr(
        cli_module, "fetch_tailscale_root", lambda suffix, gateway_ips: CERTIFICATE_PEM
    )
    monkeypatch.setattr(cli_module, "MkcertInstaller", Installer)
    monkeypatch.setattr(cli_module, "ZenNssInstaller", Installer)

    result = CliRunner().invoke(cli, ["tailscale", "trust", "tail1234"])

    assert result.exit_code == 0, result.output
    assert "store is locked" in result.output
    assert len(installed) == 2
    assert path.read_bytes() == CERTIFICATE_PEM


def test_tailnet_origin_mirrors_the_hostname(monkeypatch) -> None:
    state = TailscaleState("example.com", "tail1234", ("100.64.0.1",), {})
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    assert cli_module._tailnet_origin("shop") == "https://shop.tail1234"

    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: None)
    assert cli_module._tailnet_origin("shop") is None

    def broken():
        raise TailscaleError("bad state")

    monkeypatch.setattr(cli_module, "load_tailscale_state", broken)
    assert cli_module._tailnet_origin("shop") is None


def test_compose_run_pins_the_tailnet_origin_too(monkeypatch, tmp_path) -> None:
    from contextlib import contextmanager

    pins: dict = {}

    @contextmanager
    def fake_pinned(url, *, secondary_url=None, **kwargs):
        pins["url"] = url
        pins["secondary_url"] = secondary_url
        yield SimpleNamespace(status=lambda message: None)

    state = TailscaleState("example.com", "tail1234", ("100.64.0.1",), {})
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    monkeypatch.setattr(cli_module, "title", lambda **kwargs: None)
    monkeypatch.setattr(
        cli_module, "_proxy_origin", lambda name: f"https://{name}.localhost"
    )
    monkeypatch.setattr(cli_module.statusbar, "pinned", fake_pinned)
    monkeypatch.setattr(cli_module, "_run_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "_https_configured", lambda: True)
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    cli_module._run_compose(tmp_path, "demo", False)
    assert pins["url"] == "https://demo.localhost"
    assert pins["secondary_url"] == "https://demo.tail1234"


def test_host_run_passes_the_tailnet_origin_to_execute(monkeypatch) -> None:
    from localghost.runner import RunPlan

    executed: dict = {}
    state = TailscaleState("example.com", "tail1234", ("100.64.0.1",), {})
    plan = RunPlan("demo", "custom", ("echo",), 3000, "session", "services: {}\n")
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: state)
    monkeypatch.setattr("localghost.cli.build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr("localghost.cli.find_route_collision", lambda name: None)

    def fake_execute(*args, **kwargs):
        executed.update(kwargs)
        return 0

    monkeypatch.setattr("localghost.cli.execute", fake_execute)
    result = CliRunner().invoke(cli, ["run", "--port", "3000", "--", "echo"])
    assert result.exit_code == 0, result.output
    assert executed["secondary_origin"] == "https://demo.tail1234"


def test_status_reports_gateway_health(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_state",
        lambda: TailscaleState("example.com", "tail1234", ("100.64.0.1",), {}),
    )
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args[0], 0, "Up 5 minutes (healthy)\n", ""
        ),
    )
    result = CliRunner().invoke(cli, ["tailscale", "status"])
    assert result.exit_code == 0, result.output
    assert "Gateway health: healthy" in result.output


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ("", "not running"),
        ("Up 2 seconds (health: starting)\n", "starting"),
        ("Up 10 minutes (unhealthy)\n", "unhealthy"),
        ("Up 10 minutes\n", "running"),
    ],
)
def test_gateway_health_reads_container_state(monkeypatch, stdout, expected) -> None:
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, stdout, ""),
    )
    assert cli_module._tailscale_gateway_health() == expected


def test_gateway_health_survives_a_missing_docker(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert cli_module._tailscale_gateway_health() == "unknown"


def test_fetch_public_root_falls_back_to_the_trust_hostname(monkeypatch) -> None:
    requests = []

    def open_root(request, **kwargs):
        requests.append(request)
        return Response(CERTIFICATE_PEM)

    monkeypatch.setattr(tailscale_module.urllib.request, "urlopen", open_root)

    assert tailscale_module.fetch_public_root("tail1234") == CERTIFICATE_PEM
    assert requests[0].full_url.startswith("http://trust.tail1234/")


def test_create_auth_key_reraises_unexpected_api_errors(monkeypatch) -> None:
    from localghost.tailscale import APIError

    def rejected(*args, **kwargs):
        raise APIError(500, "boom")

    monkeypatch.setattr(tailscale_module, "_request", rejected)
    with pytest.raises(APIError, match="boom"):
        tailscale_module.API("token").create_auth_key("example.com", "tag:localghost")
