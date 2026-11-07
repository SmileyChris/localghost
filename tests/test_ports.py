"""Host port availability, selection, and diagnosing whoever holds one."""

import contextlib
import os
import shutil
import signal
import socket
import subprocess
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


def _reap(child):
    """Stop a test child's whole group, and collect it so nothing leaks.

    Killing the group is not enough on its own: an unreaped child stays a
    zombie, and its stdout pipe an open descriptor, until the Popen object
    is collected -- which Python reports as a ResourceWarning.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    child.wait()
    if child.stdout is not None:
        child.stdout.close()


def _listening_child(host):
    """A child in its own session, as runner spawns applications."""
    code = (
        "import socket, time;"
        f"family = socket.AF_INET6 if {host!r}.count(':') else socket.AF_INET;"
        "s = socket.socket(family);"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1);"
        f"s.bind(({host!r}, 0)); s.listen(5); print(s.getsockname()[1], flush=True);"
        "time.sleep(30)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True
    )
    port = int(child.stdout.readline())  # blocks until it is actually listening
    return child, port


@LINUX_ONLY
def test_loopback_only_names_an_application_the_hub_cannot_reach():
    child, port = _listening_child("127.0.0.1")
    try:
        found = ports.loopback_only(child.pid, port)
    finally:
        _reap(child)

    # A container reaches the host over its gateway address, which no
    # loopback socket accepts, so the public URL can never resolve here.
    assert found is not None and found.host == "127.0.0.1"


@LINUX_ONLY
def test_loopback_only_is_none_when_the_application_binds_every_interface():
    child, port = _listening_child("0.0.0.0")
    try:
        found = ports.loopback_only(child.pid, port)
    finally:
        _reap(child)

    assert found is None


@LINUX_ONLY
def test_loopback_only_is_none_before_anything_is_listening():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        # Still booting is not a diagnosis; the bar must keep waiting.
        assert ports.loopback_only(child.pid, 5173) is None
    finally:
        _reap(child)


@LINUX_ONLY
def test_loopback_only_ignores_a_sibling_bound_elsewhere():
    """A database on loopback must not condemn the application still booting."""
    child, port = _listening_child("127.0.0.1")
    try:
        # The application's own port is not open yet; only a sibling's is.
        assert ports.loopback_only(child.pid, port + 1) is None
    finally:
        _reap(child)


@pytest.fixture
def without_proc(monkeypatch):
    """Force the portable backend, as a host without /proc would."""
    monkeypatch.setattr(ports, "_proc_available", lambda: False)


@pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof")
@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "::1"])
def test_listeners_without_proc_reads_every_address_family(without_proc, host):
    child, port = _listening_child(host)
    try:
        found = ports.listeners(child.pid)
    finally:
        _reap(child)

    # lsof writes a wildcard as "*", which has to mean the same thing the
    # /proc reader means by 0.0.0.0 or the loopback verdict flips.
    assert ports.Listener(host, port) in found


@pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof")
def test_loopback_only_agrees_across_both_backends(without_proc):
    child, port = _listening_child("0.0.0.0")
    try:
        assert ports.loopback_only(child.pid, port) is None
    finally:
        _reap(child)


def test_listeners_without_proc_is_empty_when_lsof_is_missing(
    without_proc, monkeypatch
):
    def explode(command, **kwargs):
        raise OSError("no lsof")

    monkeypatch.setattr(ports.subprocess, "run", explode)

    assert ports.listeners(1) == []


def test_ephemeral_port_is_free_and_outside_the_dev_server_defaults():
    port = ports.ephemeral_port()

    assert port >= 1024 and ports.port_available(port)


def _bound(family, host, port=0, v6only=False):
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if v6only:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
        sock.listen(5)
    except OSError:
        sock.close()
        raise
    return sock


def _bound_v6_or_skip(host, port=0, v6only=True):
    try:
        return _bound(socket.AF_INET6, host, port, v6only)
    except OSError:
        pytest.skip("this host has no usable IPv6")


def _child_binding(family_name, host, port, v6only=False):
    code = (
        "import socket, time;"
        f"s = socket.socket(socket.{family_name});"
        + (
            "s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1);"
            if v6only
            else ""
        )
        + f"s.bind(({host!r}, {port})); s.listen(5); print('up', flush=True);"
        "time.sleep(30)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True
    )
    if not child.stdout.readline():
        _reap(child)
        pytest.skip("the child could not bind; this host has no usable IPv6")
    return child


@LINUX_ONLY
def test_listeners_on_finds_ipv4_and_ipv6_loopback():
    with _bound(socket.AF_INET, "127.0.0.1") as v4:
        port = v4.getsockname()[1]
        with _bound_v6_or_skip("::1", port):
            found = ports.listeners_on(port)

    assert {item.host for item in found} == {"127.0.0.1", "::1"}
    assert all(item.port == port and item.inode for item in found)


@LINUX_ONLY
def test_listeners_on_excludes_own_pid():
    with _bound(socket.AF_INET, "127.0.0.1") as sock:
        port = sock.getsockname()[1]
        assert ports.listeners_on(port, exclude_pids=frozenset({os.getpid()})) == []
        assert ports.listeners_on(port)


@LINUX_ONLY
def test_listeners_on_excludes_hosts():
    with _bound(socket.AF_INET, "127.0.0.1") as sock:
        port = sock.getsockname()[1]
        assert ports.listeners_on(port, exclude_hosts=frozenset({"127.0.0.1"})) == []


@LINUX_ONLY
def test_holder_restricted_to_given_inodes():
    port = ports.ephemeral_port()
    child = _child_binding("AF_INET", "127.0.0.1", port)
    try:
        with _bound_v6_or_skip("::1", port):
            theirs = ports.listeners_on(port, exclude_pids=frozenset({os.getpid()}))
            assert len(theirs) == 1
            found = ports.holder(port, inodes={theirs[0].inode})
    finally:
        _reap(child)

    assert found is not None and found.pid == child.pid


@LINUX_ONLY
@pytest.mark.parametrize("child_is_loopback", [False, True])
def test_holder_prefers_the_loopback_owner(child_is_loopback):
    # Both arrangements, so the answer cannot come from pid or scan order.
    port = ports.ephemeral_port()
    if child_is_loopback:
        child = _child_binding("AF_INET", "127.0.0.1", port)
        other = _bound_v6_or_skip("::", port)
    else:
        child = _child_binding("AF_INET6", "::", port, v6only=True)
        other = _bound(socket.AF_INET, "127.0.0.1", port)
    try:
        found = ports.holder(port)
    finally:
        other.close()
        _reap(child)

    assert found is not None
    assert found.pid == (child.pid if child_is_loopback else os.getpid())
