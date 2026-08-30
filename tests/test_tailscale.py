import json
import stat
from io import BytesIO
from subprocess import CompletedProcess
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

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


@pytest.mark.parametrize("value", ["localhost", "two.labels", "-bad", "bad-"])
def test_suffix_rejects_unsafe_values(value) -> None:
    with pytest.raises(ValueError, match="one DNS label"):
        validate_suffix(value)


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


def test_detect_suffix_falls_back_to_this_machine_name(monkeypatch) -> None:
    monkeypatch.setattr(
        tailscale_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args[0],
            0,
            json.dumps(
                {"CurrentTailnet": {"SelfDNSName": "work.example.ts.net."}}
            ),
            "",
        ),
    )
    assert detect_suffix() == "work"


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

    def read(self):
        return self.payload


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
    result = CliRunner().invoke(cli, ["tailscale", "status"])
    assert result.exit_code == 0, result.output
    assert "Route suffix: .tail1234" in result.output


def _patch_enable(monkeypatch, events, saved):
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
    monkeypatch.setattr(cli_module, "TailscaleAPI", FakeAPI)
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: None)
    monkeypatch.setattr(cli_module, "_https_configured", lambda: False)
    monkeypatch.setattr(cli_module, "_bootstrap_public_root", lambda: None)
    monkeypatch.setattr(cli_module, "_bootstrap_tailnet_root", lambda suffix: None)
    monkeypatch.setattr(
        cli_module, "_bootstrap_tailscale_gateway", lambda suffix, key: ("100.64.0.1",)
    )
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


def test_enable_uses_ephemeral_credentials_and_saves_public_state(monkeypatch) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved)

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert saved[0].suffix == "tail1234"
    assert not hasattr(saved[0], "client_secret")
    assert events[-1] == {"https_enabled": True, "force_recreate": True}


def test_enable_stores_the_credential_in_the_keyring(monkeypatch) -> None:
    events: list = []
    saved: list = []
    stored: list = []
    _patch_enable(monkeypatch, events, saved)
    monkeypatch.setattr(
        cli_module,
        "store_tailscale_credential",
        lambda client_id, client_secret: stored.append((client_id, client_secret))
        or True,
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert stored == [("id", "secret")]


def test_enable_warns_when_keyring_storage_fails(monkeypatch) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved)
    monkeypatch.setattr(
        cli_module, "store_tailscale_credential", lambda *args: False
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS + CREDENTIAL_ARGS)
    assert result.exit_code == 0, result.output
    assert "keyring" in result.output


def test_enable_uses_the_stored_credential(monkeypatch) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved)
    monkeypatch.setattr(
        cli_module,
        "load_tailscale_credential",
        lambda: tailscale_module.Credential("kid", "ksecret"),
    )

    result = CliRunner().invoke(cli, ENABLE_ARGS)
    assert result.exit_code == 0, result.output
    assert ("kid", "ksecret") in events
    assert "keyring" in result.output


def test_enable_without_credential_guides_setup_then_prompts(monkeypatch) -> None:
    events: list = []
    saved: list = []
    _patch_enable(monkeypatch, events, saved)

    result = CliRunner().invoke(cli, ENABLE_ARGS, input="typed-id\ntyped-secret\n")
    assert result.exit_code == 0, result.output
    assert ("typed-id", "typed-secret") in events
    assert "tagOwners" in result.output
    assert "tag:localghost" in result.output
    assert "admin/acls" in result.output
    assert "admin/settings/oauth" in result.output
    assert "auth_keys" in result.output
    assert "dns:write" in result.output


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
    assert kwargs["env"]["LOCALGHOST_TAILSCALE_SUFFIX"] == "tail1234"


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
