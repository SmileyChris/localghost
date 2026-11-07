from __future__ import annotations

import subprocess

import pytest

from localghost import forwards, sessions


def _session(mode="host", name="blog", port=8000, isolated=False, id="s1"):
    return sessions.Session(
        id=id,
        mode=mode,
        name=name,
        port=port,
        cwd=None if mode == "forward" else "/tmp/x",
        pid=1,
        log="",
        isolated=isolated,
    )


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    monkeypatch.delenv("LOCALGHOST_HTTP_PORT", raising=False)
    monkeypatch.delenv("LOCALGHOST_HTTPS_PORT", raising=False)
    monkeypatch.setattr(forwards.sessions, "sessions", lambda: [])
    monkeypatch.setattr(forwards.sessions, "alive", lambda session: True)
    monkeypatch.setattr(
        forwards.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )


@pytest.mark.parametrize(
    "env,port",
    [({}, 80), ({}, 443), ({"LOCALGHOST_HTTP_PORT": "8081"}, 8081)],
)
def test_port_owner_refuses_hub_ports(monkeypatch, env, port):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert forwards.port_owner(port) == f"{port} is the localghost hub's port"


def test_port_owner_names_a_live_host_run(monkeypatch):
    monkeypatch.setattr(forwards.sessions, "sessions", lambda: [_session()])
    assert (
        forwards.port_owner(8000)
        == "8000 is blog's port; it is already at blog.localhost"
    )
    assert forwards.port_owner(8001) is None


def test_port_owner_ignores_a_dead_host_run(monkeypatch):
    monkeypatch.setattr(forwards.sessions, "sessions", lambda: [_session()])
    monkeypatch.setattr(forwards.sessions, "alive", lambda session: False)
    assert forwards.port_owner(8000) is None


def test_port_owner_counts_an_isolated_runs_host_side(monkeypatch):
    monkeypatch.setattr(
        forwards.sessions, "sessions", lambda: [_session(isolated=True, port=41234)]
    )
    assert forwards.port_owner(41234) is not None


def test_port_owner_names_another_forward_but_not_itself(monkeypatch):
    monkeypatch.setattr(
        forwards.sessions,
        "sessions",
        lambda: [_session(mode="forward", name="api", port=5000, id="f1")],
    )
    assert forwards.port_owner(5000) == "5000 is already forwarded as api.localhost"
    assert forwards.port_owner(5000, exclude_session="f1") is None


def test_port_owner_parses_docker_published_ports(monkeypatch):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="web\t127.0.0.1:8080->80/tcp, [::1]:8080->80/tcp\nother\t80/tcp\n",
            stderr="",
        )

    monkeypatch.setattr(forwards.subprocess, "run", run)
    assert (
        forwards.port_owner(8080)
        == "8080 is published by web, already routed by localghost"
    )
    assert forwards.port_owner(8081) is None
    assert forwards.port_owner(80) is not None  # hub port, not the container's


@pytest.mark.parametrize("failure", ["nonzero", "missing"])
def test_port_owner_ignores_docker_failure(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("docker")
        return subprocess.CompletedProcess(
            args, 1, stdout="web\t0.0.0.0:8080->80/tcp", stderr=""
        )

    monkeypatch.setattr(forwards.subprocess, "run", run)
    assert forwards.port_owner(8080) is None
