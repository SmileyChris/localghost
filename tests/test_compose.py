import json
from subprocess import CompletedProcess

import click
import pytest

from localhost.compose import declared_ports, resolve_compose


def test_declared_ports_combines_compose_and_traefik_sources() -> None:
    service = {
        "ports": [{"target": 8000}, "9000/tcp", "invalid"],
        "expose": [8080, "7000/udp"],
        "labels": {
            "traefik.http.services.app.loadbalancer.server.port": "5000",
            "unrelated": "value",
        },
    }

    assert declared_ports(service) == [5000, 7000, 8000, 8080, 9000]


def test_resolve_compose_builds_repeated_file_arguments(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return CompletedProcess(command, 0, json.dumps({"name": "project"}), "")

    monkeypatch.setattr("localhost.compose.subprocess.run", run)

    assert resolve_compose(("base.yaml", "local.yaml")) == {"name": "project"}
    assert calls[0][0] == [
        "docker",
        "compose",
        "--file",
        "base.yaml",
        "--file",
        "local.yaml",
        "config",
        "--format",
        "json",
    ]


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (CompletedProcess([], 1, "", "bad compose"), "bad compose"),
        (CompletedProcess([], 0, "not-json", ""), "invalid JSON"),
    ],
)
def test_resolve_compose_reports_command_and_json_errors(
    monkeypatch, result, message
) -> None:
    monkeypatch.setattr(
        "localhost.compose.subprocess.run", lambda *a, **k: result
    )

    with pytest.raises(click.ClickException, match=message):
        resolve_compose(())


def test_resolve_compose_reports_missing_docker(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("localhost.compose.subprocess.run", missing)

    with pytest.raises(click.ClickException, match="docker is required"):
        resolve_compose(())


def test_resolve_compose_requires_a_json_object(monkeypatch) -> None:
    monkeypatch.setattr(
        "localhost.compose.subprocess.run",
        lambda *a, **k: CompletedProcess([], 0, "[]", ""),
    )

    with pytest.raises(click.ClickException, match="non-object JSON model"):
        resolve_compose(())
