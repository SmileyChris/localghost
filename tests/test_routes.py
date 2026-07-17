import json
from subprocess import CompletedProcess

import click
import pytest

from localhost import routes


def test_routes_show_host_path_or_compose_location(monkeypatch):
    containers = [
        {
            "Name": "/host-bridge",
            "Config": {
                "Labels": {
                    "traefik.enable": "true",
                    "traefik.http.routers.demo.rule": "Host(`demo.localhost`)",
                    "io.localhost.source-path": "/work/demo",
                }
            },
        },
        {
            "Name": "/compose-web",
            "Config": {
                "Labels": {
                    "traefik.enable": "true",
                    "traefik.http.routers.web.rule": "Host(`web.localhost`)",
                    "com.docker.compose.project": "web",
                    "com.docker.compose.service": "app",
                }
            },
        },
    ]

    def run(command, **kwargs):
        if command[1:3] == ["ps", "--quiet"]:
            return CompletedProcess(command, 0, "one\ntwo\n", "")
        return CompletedProcess(command, 0, json.dumps(containers), "")

    monkeypatch.setattr(routes.subprocess, "run", run)
    assert routes.active_routes() == [
        routes.Route("demo.localhost", "/work/demo"),
        routes.Route("web.localhost", "web / app"),
    ]


def test_proxy_running_uses_fixed_project_labels(monkeypatch):
    recorded = []

    def run(command, **kwargs):
        recorded.append(command)
        return CompletedProcess(command, 0, "container\n", "")

    monkeypatch.setattr(routes.subprocess, "run", run)
    assert routes.proxy_is_running()
    assert "label=com.docker.compose.project=localhost" in recorded[0]


def test_routes_handle_empty_and_unusual_container_metadata(monkeypatch):
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 0, "", ""),
    )
    assert routes.active_routes() == []

    container = {
        "Name": "/manual",
        "Config": {"Labels": {"traefik.http.routers.x.rule": "Host(`x.localhost`)"}},
    }
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(
            command, 0, "id\n" if command[1] == "ps" else json.dumps([container]), ""
        ),
    )
    assert routes.active_routes() == [routes.Route("x.localhost", "manual")]


def test_routes_report_docker_failures(monkeypatch):
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 4, "", "no Docker"),
    )
    with pytest.raises(click.ClickException, match="no Docker"):
        routes.proxy_is_running()

    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    with pytest.raises(click.ClickException, match="docker is required"):
        routes.proxy_is_running()
