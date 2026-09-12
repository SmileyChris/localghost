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


def test_gateway_picks_the_ipv4_address_when_the_bridge_is_dual_stack(monkeypatch):
    monkeypatch.setattr(
        forwarder.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "172.17.0.1\nfd00:dead:beef::1\n", ""
        ),
    )

    # A bridge with IPv6 enabled reports two gateways. The hub's route to the
    # host is the IPv4 one, and two addresses run together are not an address.
    assert forwarder.gateway() == "172.17.0.1"


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


def test_available_needs_both_a_gateway_and_bind_detection(monkeypatch):
    monkeypatch.setattr(forwarder, "gateway", lambda: "127.0.0.1")
    monkeypatch.setattr(forwarder.sys, "platform", "linux")
    assert forwarder.available()

    # Without /proc nothing can tell that a bind needs relaying, so promising
    # a relay would strand the application with no wider bind to fall back on.
    monkeypatch.setattr(forwarder.sys, "platform", "darwin")
    assert not forwarder.available()


def test_available_is_false_without_a_gateway(monkeypatch):
    monkeypatch.setattr(forwarder, "gateway", lambda: None)
    monkeypatch.setattr(forwarder.sys, "platform", "linux")

    assert not forwarder.available()


def test_available_is_false_when_the_gateway_is_not_an_address_of_this_host(
    monkeypatch,
):
    monkeypatch.setattr(forwarder, "gateway", lambda: "192.0.2.1")
    monkeypatch.setattr(forwarder.sys, "platform", "linux")

    # Docker Desktop and rootless Docker report a gateway that lives inside a
    # VM or a user namespace. Promising a relay on it drops the wider bind
    # the application needs, and the relay then cannot be raised.
    assert not forwarder.available()


def test_local_tells_an_address_of_this_host_from_one_that_is_not():
    assert forwarder.local("127.0.0.1")
    assert not forwarder.local("192.0.2.1")
    assert not forwarder.local("not an address")


def test_the_hub_cannot_reach_loopback_on_linux(monkeypatch):
    monkeypatch.setattr(forwarder.sys, "platform", "linux")
    monkeypatch.setattr(forwarder, "gateway", lambda: "127.0.0.1")

    # The bridge connects over the host gateway, which no loopback socket
    # accepts, so a loopback bind genuinely cannot be served.
    assert not forwarder.hub_reaches_loopback()


def test_the_hub_reaches_loopback_where_docker_is_not_native_to_this_host(
    monkeypatch,
):
    monkeypatch.setattr(forwarder.sys, "platform", "linux")
    monkeypatch.setattr(forwarder, "gateway", lambda: "192.0.2.1")

    # A gateway this host cannot bind belongs to a VM or a user namespace:
    # Docker Desktop for Linux, or rootless Docker. Both proxy the container's
    # route to the host through a process that reaches loopback, so a
    # loopback bind is served as it is on macOS.
    assert forwarder.hub_reaches_loopback()


def test_a_loopback_bind_is_left_alone_where_docker_runs_in_a_vm(monkeypatch):
    monkeypatch.setattr(forwarder.sys, "platform", "darwin")

    # There the connection is proxied by a process on the host itself, which
    # reaches loopback. Condemning such a run would break what works today.
    assert forwarder.hub_reaches_loopback()


def test_a_long_lived_connection_survives_others_coming_and_going(echo):
    """The HMR pattern: one socket held open while page requests churn."""
    port = _free_port()

    with (
        forwarder.forwarding(echo, bind="127.0.0.1", port=port),
        socket.create_connection(("127.0.0.1", port), timeout=5) as hmr,
    ):
        hmr.sendall(b"subscribe")
        assert hmr.recv(4096) == b"SUBSCRIBE"

        for index in range(10):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as page:
                page.sendall(f"get {index}".encode())
                assert page.recv(4096) == f"GET {index}".encode()

        # Still the same socket, still relaying, after all of that.
        hmr.sendall(b"update")
        assert hmr.recv(4096) == b"UPDATE"
