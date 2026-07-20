import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compose_model(*paths: Path, **environment: str) -> dict:
    command = ["docker", "compose"]
    for path in paths:
        command.extend(["--file", str(path)])
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
        ROOT / "compose.yaml", LOCALGHOST_HTTP_PORT="18081"
    )

    assert model["name"] == "localghost"
    assert set(model["services"]) == {"traefik"}
    assert set(model["networks"]) == {"localghost"}
    assert model["networks"]["localghost"]["name"] == "localghost"

    traefik = model["services"]["traefik"]
    assert traefik["image"] == "localghost-traefik:v3.7.7"
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
