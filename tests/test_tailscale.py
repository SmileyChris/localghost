import json
import stat
from io import BytesIO
from subprocess import CompletedProcess
from urllib.error import HTTPError, URLError

import pytest
from click.testing import CliRunner

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
    error = HTTPError("url", 403, "Forbidden", {}, BytesIO(b"denied"))
    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(TailscaleError, match="HTTP 403: denied"):
        API("token").split_dns("-")


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
    monkeypatch.setattr(
        tailscale_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: Response(CERTIFICATE_PEM),
    )
    assert tailscale_module.fetch_public_root("tail1234") == CERTIFICATE_PEM
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


def test_enable_uses_ephemeral_credentials_and_saves_public_state(monkeypatch) -> None:
    events = []

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

    saved = []
    monkeypatch.setattr(cli_module, "TailscaleAPI", FakeAPI)
    monkeypatch.setattr(cli_module, "load_tailscale_state", lambda: None)
    monkeypatch.setattr(cli_module, "_bootstrap_public_root", lambda: None)
    monkeypatch.setattr(cli_module, "_bootstrap_tailnet_root", lambda suffix: None)
    monkeypatch.setattr(
        cli_module, "_bootstrap_tailscale_gateway", lambda suffix, key: ("100.64.0.1",)
    )
    monkeypatch.setattr(cli_module, "save_tailscale_state", saved.append)
    monkeypatch.setattr(
        cli_module, "_run_proxy", lambda *args, **kwargs: events.append(kwargs)
    )

    result = CliRunner().invoke(
        cli,
        [
            "tailscale",
            "enable",
            "--tailnet",
            "example.com",
            "--suffix",
            "tail1234",
            "--client-id",
            "id",
            "--client-secret",
            "secret",
        ],
    )
    assert result.exit_code == 0, result.output
    assert saved[0].suffix == "tail1234"
    assert not hasattr(saved[0], "client_secret")
    assert events[-1] == {"https_enabled": True, "force_recreate": True}


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

    command, kwargs = commands[0]
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
        def __init__(self, path):
            self.path = path

        def install(self, **kwargs):
            installed.append(self.path)

    monkeypatch.setattr(
        cli_module, "fetch_tailscale_root", lambda suffix: CERTIFICATE_PEM
    )
    monkeypatch.setattr(cli_module, "MkcertInstaller", Installer)
    monkeypatch.setattr(cli_module, "ZenNssInstaller", Installer)
    result = CliRunner().invoke(cli, ["tailscale", "trust", "tail1234"])
    assert result.exit_code == 0, result.output
    assert len(installed) == 2
    assert (tmp_path / "tailscale-tail1234-rootCA.pem").read_bytes() == CERTIFICATE_PEM
