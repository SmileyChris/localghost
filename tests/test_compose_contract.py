import json
import os
import subprocess
from pathlib import Path

from localghost.cli import LOCALGHOST_VERSION

ROOT = Path(__file__).resolve().parents[1]


def compose_model(
    *paths: Path, profiles: tuple[str, ...] = (), **environment: str
) -> dict:
    command = ["docker", "compose"]
    for path in paths:
        command.extend(["--file", str(path)])
    for profile in profiles:
        command.extend(["--profile", profile])
    command.extend(["config", "--format", "json"])
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **environment},
    )
    return json.loads(result.stdout)


def test_proxy_compose_matches_the_public_contract() -> None:
    model = compose_model(
        ROOT / "compose.yaml",
        LOCALGHOST_HTTP_PORT="18081",
        LOCALGHOST_IMAGE_TAG=f"v{LOCALGHOST_VERSION}",
    )

    assert model["name"] == "localghost"
    assert set(model["services"]) == {"traefik"}
    assert set(model["networks"]) == {"localghost"}
    assert model["networks"]["localghost"]["name"] == "localghost"

    traefik = model["services"]["traefik"]
    assert traefik["image"] == f"localghost-traefik:v{LOCALGHOST_VERSION}"
    assert traefik["pull_policy"] == "build"
    assert traefik["build"] == {
        "context": str(ROOT / "src" / "localghost"),
        "dockerfile": "Dockerfile",
    }
    assert traefik["restart"] == "unless-stopped"
    assert set(traefik["networks"]) == {"localghost"}

    assert set(traefik["command"]) == {
        "--api.dashboard=true",
        "--api.insecure=false",
        "--entrypoints.web.address=:80",
        "--global.checknewversion=false",
        "--global.sendanonymoususage=false",
        "--ping=true",
        "--providers.docker=true",
        "--providers.docker.exposedbydefault=false",
        "--providers.docker.network=localghost",
    }
    assert traefik["healthcheck"]["test"] == [
        "CMD",
        "traefik",
        "healthcheck",
        "--ping",
    ]

    assert traefik["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 80,
            "published": "18081",
            "protocol": "tcp",
        }
    ]
    assert not any(port["target"] == 8080 for port in traefik["ports"])

    socket_mount = next(
        volume
        for volume in traefik["volumes"]
        if volume["target"] == "/var/run/docker.sock"
    )
    assert socket_mount["source"] == "/var/run/docker.sock"
    assert socket_mount["type"] == "bind"
    assert socket_mount["read_only"] is True

    labels = traefik["labels"]
    assert labels["traefik.enable"] == "true"
    assert labels["traefik.docker.network"] == "localghost"
    assert labels[
        "traefik.http.routers.localghost-dashboard.service"
    ] == "api@internal"
    assert labels[
        "traefik.http.routers.localghost-dashboard.rule"
    ] == "Host(`traefik.localhost`)"
    assert labels[
        "traefik.http.middlewares.localghost-dashboard-redirect.redirectregex.replacement"
    ] == "http://$${1}/dashboard/"


def test_https_proxy_adds_loopback_dashboard_with_secure_redirect() -> None:
    model = compose_model(
        ROOT / "src" / "localghost" / "proxy_compose.yaml",
        ROOT / "src" / "localghost" / "proxy_compose_https.yaml",
        LOCALGHOST_HTTP_PORT="18081",
        LOCALGHOST_HTTPS_PORT="18443",
    )

    traefik = model["services"]["traefik"]
    assert "bootstrap" not in model["services"]
    assert traefik["pull_policy"] == "build"
    https_port = next(port for port in traefik["ports"] if port["target"] == 443)
    assert https_port["host_ip"] == "127.0.0.1"
    assert https_port["published"] == "18443"

    labels = traefik["labels"]
    assert labels[
        "traefik.http.routers.localghost-dashboard-secure.middlewares"
    ] == "localghost-dashboard-secure-redirect"
    assert labels[
        "traefik.http.middlewares.localghost-dashboard-secure-redirect.redirectregex.replacement"
    ] == "https://$${1}/dashboard/"

    profiled_model = compose_model(
        ROOT / "src" / "localghost" / "proxy_compose.yaml",
        ROOT / "src" / "localghost" / "proxy_compose_https.yaml",
        profiles=("bootstrap",),
        LOCALGHOST_HTTP_PORT="18081",
        LOCALGHOST_HTTPS_PORT="18443",
    )
    assert profiled_model["services"]["bootstrap"]["profiles"] == ["bootstrap"]


def test_bootstrap_runs_a_prebuilt_binary_from_the_hub_image() -> None:
    model = compose_model(
        ROOT / "src" / "localghost" / "proxy_compose.yaml",
        ROOT / "src" / "localghost" / "proxy_compose_https.yaml",
        profiles=("bootstrap",),
        LOCALGHOST_HTTP_PORT="18081",
        LOCALGHOST_HTTPS_PORT="18443",
        LOCALGHOST_IMAGE_TAG=f"v{LOCALGHOST_VERSION}",
    )

    bootstrap = model["services"]["bootstrap"]
    # The authority is created by a binary compiled into the hub image, so no
    # Go toolchain is pulled or run to trust a hub.
    assert bootstrap["image"] == f"localghost-traefik:v{LOCALGHOST_VERSION}"
    assert bootstrap["pull_policy"] == "build"
    assert bootstrap["build"] == {
        "context": str(ROOT / "src" / "localghost"),
        "dockerfile": "Dockerfile",
    }
    assert bootstrap["entrypoint"] == ["/usr/local/bin/localghost-bootstrap"]
    assert bootstrap["network_mode"] == "none"
    assert {mount["target"] for mount in bootstrap["volumes"]} == {
        "/var/lib/localghost-root",
        "/var/lib/localghost-ca",
    }


def test_bootstrap_prints_only_the_public_root(monkeypatch, tmp_path) -> None:
    """The real invocation must not mix build output into the certificate."""
    import localghost.cli as cli_module
    from localghost.trust import PublicCertificate

    project = "localghost-contract-test"
    monkeypatch.setattr(cli_module, "PROJECT_NAME", project)
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    try:
        certificate = cli_module._bootstrap_public_root()
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                project,
                "--file",
                str(ROOT / "src" / "localghost" / "proxy_compose.yaml"),
                "--file",
                str(ROOT / "src" / "localghost" / "proxy_compose_https.yaml"),
                "--profile",
                "bootstrap",
                "down",
                "--volumes",
            ],
            check=False,
            capture_output=True,
            env={**os.environ, "LOCALGHOST_IMAGE_TAG": f"v{LOCALGHOST_VERSION}"},
        )

    assert PublicCertificate.parse(certificate.pem).pem == certificate.pem
    assert (tmp_path / "rootCA.pem").read_bytes() == certificate.pem


def test_hub_image_ships_a_working_bootstrap_binary(tmp_path) -> None:
    context = ROOT / "src" / "localghost"
    tag = "localghost-traefik:contract-test"
    subprocess.run(
        ["docker", "build", "--tag", tag, "--file", str(context / "Dockerfile"), "."],
        check=True,
        capture_output=True,
        cwd=context,
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/usr/local/bin/localghost-bootstrap",
            tag,
            "--root-path=/tmp/root",
            "--signer-path=/tmp/signer",
            "--print-root",
        ],
        check=True,
        capture_output=True,
    )

    assert result.stdout.startswith(b"-----BEGIN CERTIFICATE-----")
    assert b"PRIVATE KEY" not in result.stdout


def test_tailscale_overlay_adds_unpublished_gateway_and_suffix_provider() -> None:
    model = compose_model(
        ROOT / "src" / "localghost" / "proxy_compose.yaml",
        ROOT / "src" / "localghost" / "proxy_compose_https.yaml",
        ROOT / "src" / "localghost" / "proxy_compose_tailscale.yaml",
        LOCALGHOST_IMAGE_TAG="test",
        LOCALGHOST_TAILSCALE_SUFFIX="tail1234",
    )

    gateway = model["services"]["tailscale-gateway"]
    assert "ports" not in gateway
    assert gateway["pull_policy"] == "build"
    assert gateway["read_only"] is True
    assert gateway["cap_drop"] == ["ALL"]
    assert gateway["security_opt"] == ["no-new-privileges:true"]
    assert set(gateway["networks"]) == {"localghost"}
    assert gateway["command"] == [
        "--suffix=tail1234",
        "--hostname=localghost-tail1234",
        "--state-dir=/var/lib/localghost-tailscale",
        "--root-ca=/var/lib/localghost-root/rootCA.pem",
        "--http-target=traefik:80",
        "--https-target=traefik:443",
    ]
    root_mount = next(
        mount
        for mount in gateway["volumes"]
        if mount["target"] == "/var/lib/localghost-root"
    )
    assert root_mount == {
        "type": "volume",
        "source": "localghost-tailnet-ca-signer",
        "target": "/var/lib/localghost-root",
        "read_only": True,
        "volume": {},
    }
    assert model["volumes"]["localghost-tailscale-state"]["name"] == (
        "localghost-tailscale-state-tail1234"
    )
    assert model["volumes"]["localghost-tailnet-ca-signer"]["name"] == (
        "localghost-tailnet-ca-signer-v2-tail1234"
    )

    command = set(model["services"]["traefik"]["command"])
    assert "--providers.plugin.localghostCA.domainsuffix=localhost" in command
    assert "--providers.plugin.localghostTailnetCA.domainsuffix=tail1234" in command
    assert (
        "--experimental.localplugins.localghostTailnetCA.modulename="
        "github.com/SmileyChris/traefik-localghost-tailnet-ca"
    ) in command


def test_example_compose_exercises_consumer_contract() -> None:
    model = compose_model(
        ROOT / "examples" / "compose.yaml",
        COMPOSE_PROJECT_NAME="contract-fixture",
    )

    assert set(model["services"]) == {"web", "mailpit", "unlabelled"}
    assert model["networks"]["localghost"]["external"] is True

    web = model["services"]["web"]
    assert set(web["networks"]) == {"default", "localghost"}
    assert web["expose"] == ["8080"]
    assert web["labels"]["traefik.enable"] == "true"
    assert web["labels"][
        "traefik.http.services.contract-fixture-web.loadbalancer.server.port"
    ] == "80"
    assert web["labels"][
        "traefik.http.routers.contract-fixture-web.rule"
    ] == "Host(`contract-fixture.localhost`)"

    mailpit = model["services"]["mailpit"]
    assert mailpit["labels"][
        "traefik.http.routers.contract-fixture-mailpit.rule"
    ] == "Host(`mailpit.contract-fixture.localhost`)"
    assert "labels" not in model["services"]["unlabelled"]
