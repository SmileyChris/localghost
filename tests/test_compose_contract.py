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
        "--experimental.localplugins.localghostFallback.modulename=github.com/SmileyChris/traefik-localghost-fallback",
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
    assert labels["traefik.http.routers.localghost-fallback.rule"] == "HostRegexp(`.+`)"
    assert labels["traefik.http.routers.localghost-fallback.priority"] == "1"
    assert labels["traefik.http.routers.localghost-fallback.entrypoints"] == "web"
    assert labels["traefik.http.routers.localghost-fallback.service"] == "noop@internal"
    assert (
        labels["traefik.http.routers.localghost-fallback.middlewares"]
        == "localghost-fallback"
    )
    assert (
        labels[
            "traefik.http.middlewares.localghost-fallback.plugin.localghostFallback.registryPath"
        ]
        == "/var/lib/localghost-registry"
    )
    # The self-contained hub carries no user state.
    assert not any(
        "localghost-registry" in str(volume) for volume in traefik["volumes"]
    )


def test_packaged_proxy_mounts_registry_and_serves_fallback(tmp_path) -> None:
    model = compose_model(
        ROOT / "src" / "localghost" / "proxy_compose.yaml",
        LOCALGHOST_HTTP_PORT="18081",
        LOCALGHOST_IMAGE_TAG=f"v{LOCALGHOST_VERSION}",
        LOCALGHOST_REGISTRY_DIR=str(tmp_path),
    )
    traefik = model["services"]["traefik"]
    assert (
        "--experimental.localplugins.localghostFallback.modulename="
        "github.com/SmileyChris/traefik-localghost-fallback" in traefik["command"]
    )
    labels = traefik["labels"]
    assert labels["traefik.http.routers.localghost-fallback.priority"] == "1"
    mounts = {
        (volume["source"], volume["target"], not volume.get("read_only", False))
        for volume in traefik["volumes"]
        if volume["target"] == "/var/lib/localghost-registry"
    }
    assert mounts == {(str(tmp_path), "/var/lib/localghost-registry", False)}


def test_https_proxy_adds_secure_fallback_router(tmp_path) -> None:
    model = compose_model(
        ROOT / "src" / "localghost" / "proxy_compose.yaml",
        ROOT / "src" / "localghost" / "proxy_compose_https.yaml",
        LOCALGHOST_HTTP_PORT="18081",
        LOCALGHOST_IMAGE_TAG=f"v{LOCALGHOST_VERSION}",
        LOCALGHOST_REGISTRY_DIR=str(tmp_path),
    )
    labels = model["services"]["traefik"]["labels"]
    assert (
        labels["traefik.http.routers.localghost-fallback-secure.entrypoints"]
        == "websecure"
    )
    assert (
        labels["traefik.http.routers.localghost-fallback-secure.rule"]
        == "HostRegexp(`.+`)"
    )
    assert labels["traefik.http.routers.localghost-fallback-secure.priority"] == "1"
    assert labels["traefik.http.routers.localghost-fallback-secure.tls"] == "true"
    assert (
        labels["traefik.http.routers.localghost-fallback-secure.service"]
        == "noop@internal"
    )
    assert (
        labels["traefik.http.routers.localghost-fallback-secure.middlewares"]
        == "localghost-fallback"
    )
    command = model["services"]["traefik"]["command"]
    assert (
        "--experimental.localplugins.localghostFallback.modulename="
        "github.com/SmileyChris/traefik-localghost-fallback" in command
    )
    # The CA provider reads the same registry so remembered hostnames keep
    # certificates after their containers stop.
    assert (
        "--providers.plugin.localghostCA.registrypath=/var/lib/localghost-registry"
        in command
    )


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
