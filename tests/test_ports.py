"""Host port availability, selection, and diagnosing whoever holds one."""

import os
import socket
import sys

import click
import pytest

from localghost import ports


@pytest.fixture
def ipv6_listener():
    """A listener bound to the IPv6 loopback only, as Node dev servers are."""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    sock.bind(("::1", 0))
    sock.listen(5)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def test_a_port_held_only_over_ipv6_is_not_available(ipv6_listener):
    # Binding the IPv4 wildcard succeeds while a v6 listener holds the port,
    # so an IPv4-only check reports it free. localghost then plans that port,
    # points the bridge at it, and the application quietly lands elsewhere.
    assert not ports.port_available(ipv6_listener)


LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="identifying a port's holder reads /proc",
)


@LINUX_ONLY
def test_holder_names_the_process_listening_on_a_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        found = ports.holder(sock.getsockname()[1])

    assert found is not None
    # This test process is the listener, so its own identity is the answer.
    assert found.pid == os.getpid()
    assert found.directory == os.getcwd()


@LINUX_ONLY
def test_holder_finds_a_listener_bound_only_over_ipv6(ipv6_listener):
    found = ports.holder(ipv6_listener)

    assert found is not None and found.pid == os.getpid()


def test_holder_is_none_when_nothing_is_listening():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]

    # Closed above, so the port is free and there is nobody to name.
    assert ports.holder(free) is None


@LINUX_ONLY
def test_a_taken_port_names_whoever_is_holding_it(ipv6_listener):
    with pytest.raises(click.ClickException) as caught:
        ports.select_port(ipv6_listener, strict=True)

    # A bare "port is in use" is a dead end; the holder's identity is what
    # turns it into something the user can act on.
    message = str(caught.value)
    assert str(ipv6_listener) in message
    assert str(os.getpid()) in message
    assert os.getcwd() in message


@LINUX_ONLY
def test_a_taken_port_says_what_to_do_about_it(ipv6_listener):
    with pytest.raises(click.ClickException) as caught:
        ports.select_port(ipv6_listener, strict=True)

    # Naming the holder diagnoses the dead end; it does not get the user out
    # of it. PRODUCT.md asks for the next useful action to be obvious.
    assert "--port" in str(caught.value)


def test_an_unidentifiable_holder_still_says_what_to_do(monkeypatch):
    monkeypatch.setattr(ports, "holder", lambda port: None)

    message = ports.in_use_message(5173)

    assert "5173" in message and "--port" in message
    # Nothing to list, so the message stays a single sentence rather than
    # trailing a colon into an empty block.
    assert ":\n" not in message
