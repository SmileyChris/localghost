"""The host-side relay that lets the hub reach a loopback-bound application."""

import socket
import socketserver
import subprocess
import threading

import pytest

from localghost import forwarder, ports


class _Echo(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            self.request.sendall(data.upper())


@pytest.fixture
def echo():
    """An application listening on loopback, as Node dev servers do."""
    server = _Echo(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield ports.Listener("127.0.0.1", server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_forwarding_relays_bytes_in_both_directions(echo):
    port = _free_port()

    with (
        forwarder.forwarding(echo, bind="127.0.0.1", port=port),
        socket.create_connection(("127.0.0.1", port), timeout=5) as client,
    ):
        client.sendall(b"hello")
        assert client.recv(4096) == b"HELLO"
        # A second exchange proves the relay is not one-shot.
        client.sendall(b"again")
        assert client.recv(4096) == b"AGAIN"


def test_forwarding_binds_only_the_address_it_was_given(echo):
    port = _free_port()

    # 127.0.0.2 is this machine too, but it is not the address we bound.
    # The hub must be the only thing that can reach the application.
    with (
        forwarder.forwarding(echo, bind="127.0.0.1", port=port),
        pytest.raises(OSError),
    ):
        socket.create_connection(("127.0.0.2", port), timeout=2).close()


def test_forwarding_stops_listening_once_the_block_ends(echo):
    port = _free_port()

    with forwarder.forwarding(echo, bind="127.0.0.1", port=port):
        socket.create_connection(("127.0.0.1", port), timeout=5).close()

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_gateway_reads_the_default_bridge_address(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="172.17.0.1\n", stderr="")

    monkeypatch.setattr(forwarder.subprocess, "run", fake_run)

    # The default bridge gateway is what Docker's host-gateway alias resolves
    # to, even for a container attached to another network.
    assert forwarder.gateway() == "172.17.0.1"
    assert "bridge" in seen["command"]


def test_gateway_is_none_when_docker_cannot_answer(monkeypatch):
    monkeypatch.setattr(
        forwarder.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "boom"),
    )

    # Guessing an address here would bind something arbitrary; saying nothing
    # lets the caller fall back to explaining the problem instead.
    assert forwarder.gateway() is None


def test_gateway_is_none_when_docker_is_missing(monkeypatch):
    def explode(command, **kwargs):
        raise OSError("no docker")

    monkeypatch.setattr(forwarder.subprocess, "run", explode)

    assert forwarder.gateway() is None


def test_gateway_is_none_when_the_output_is_not_an_address(monkeypatch):
    monkeypatch.setattr(
        forwarder.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "<no value>\n", ""
        ),
    )

    assert forwarder.gateway() is None
