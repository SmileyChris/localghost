from __future__ import annotations

import subprocess

import pytest

from localghost import forwards, ports, sessions


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


# --- Watcher ---------------------------------------------------------------

V4 = ports.PortListener("127.0.0.1", 5173, "100")
V6 = ports.PortListener("::1", 5173, "200")
WILD = ports.PortListener("0.0.0.0", 5173, "300")
GULCH = "/home/me/dev/lab/gulch"
OTHER = "/home/me/dev/other"
NOTHING = (
    "nothing is listening on 5173 yet; api.localhost will serve it once "
    "something does"
)
CANNOT_TELL = (
    "cannot tell which process owns 5173; api.localhost will serve whatever "
    "listens there"
)


def _seen(*listeners, directory=GULCH, pid=41, owner=None, probe_ok=False):
    return forwards.Observation(
        listeners=tuple(listeners),
        probe_ok=probe_ok,
        holder_directory=directory,
        holder_pid=pid if directory else None,
        owner=owner,
    )


def _empty():
    return _seen(directory=None)


def _watcher(needed=False, possible=True, calls=None):
    def relay_needed():
        if calls is not None:
            calls.append("needed")
        return needed

    def relay_possible():
        if calls is not None:
            calls.append("possible")
        return possible

    return forwards.Watcher(
        "api", 5173, relay_needed=relay_needed, relay_possible=relay_possible
    )


def test_nothing_listening_waits_without_bridge():
    watcher = _watcher()
    decision = watcher.step(0.0, _empty())
    assert decision == forwards.Decision(
        bridge=False,
        relay_to=None,
        route="waiting: nothing listening on 5173",
        events=(NOTHING,),
    )
    assert watcher.step(0.5, _empty()).events == ()


def test_first_holder_is_pinned_and_announced():
    watcher = _watcher()
    decision = watcher.step(0.0, _seen(WILD))
    assert watcher.pinned_directory == GULCH
    assert decision.bridge is True
    assert decision.route == f"up: {GULCH}"
    assert decision.events == (f"forwarding to 5173, held by {GULCH}",)
    assert watcher.step(0.5, _seen(WILD)).events == ()


def test_same_directory_keeps_route_after_new_inode():
    watcher = _watcher()
    watcher.step(0.0, _seen(WILD))
    restarted = ports.PortListener("0.0.0.0", 5173, "301")
    decision = watcher.step(0.5, _seen(restarted, pid=42))
    assert decision.bridge is True
    assert decision.route == f"up: {GULCH}"
    assert decision.events == ()
    assert watcher.pinned_directory == GULCH


def test_other_directory_lowers_route_and_keeps_waiting():
    watcher = _watcher()
    watcher.step(0.0, _seen(WILD))
    decision = watcher.step(0.5, _seen(WILD, directory=OTHER, pid=99))
    assert decision.bridge is False
    assert decision.relay_to is None
    assert decision.route == f"waiting: 5173 held by {OTHER}"
    assert decision.events == (
        f"port 5173 is held by {OTHER} (pid 99); not routing api",
    )
    assert watcher.pinned_directory == GULCH
    again = watcher.step(1.0, _seen(WILD, directory=OTHER, pid=99))
    assert again.events == ()
    assert again.route == f"waiting: 5173 held by {OTHER}"


def test_matching_server_after_other_directory_raises_route():
    watcher = _watcher()
    watcher.step(0.0, _seen(WILD))
    watcher.step(0.5, _seen(WILD, directory=OTHER, pid=99))
    back = ports.PortListener("0.0.0.0", 5173, "302")
    decision = watcher.step(1.0, _seen(back, pid=43))
    assert decision.bridge is True
    assert decision.route == f"up: {GULCH}"
    assert decision.events == (f"forwarding to 5173, held by {GULCH}",)


def test_unknown_holder_follows_the_port_and_says_so_once():
    watcher = _watcher()
    decision = watcher.step(0.0, _seen(WILD, directory=None))
    assert decision.bridge is True
    assert decision.route == "up"
    assert decision.events == (CANNOT_TELL,)
    assert watcher.pinned_directory is None
    assert watcher.step(0.5, _seen(WILD, directory=None)).events == ()
    # Gone long enough to lower the bridge, then back: still said only once.
    watcher.step(1.0, _empty())
    assert watcher.step(7.0, _empty()).bridge is False
    other = ports.PortListener("0.0.0.0", 5173, "303")
    decision = watcher.step(8.0, _seen(other, directory=None))
    assert decision.route == "up"
    assert CANNOT_TELL not in decision.events
    # A known holder later is pinned; an unknown one afterwards still follows.
    decision = watcher.step(8.5, _seen(WILD, directory=GULCH))
    assert watcher.pinned_directory == GULCH
    assert decision.route == f"up: {GULCH}"
    decision = watcher.step(9.0, _seen(other, directory=None))
    assert decision.route == "up"
    assert decision.bridge is True
    assert CANNOT_TELL not in decision.events


def test_owner_lowers_route_with_its_reason():
    watcher = _watcher()
    watcher.step(0.0, _seen(WILD))
    owner = "5173 is blog's port; it is already at blog.localhost"
    decision = watcher.step(0.5, _seen(WILD, owner=owner))
    assert decision.bridge is False
    assert decision.relay_to is None
    assert decision.route == f"waiting: {owner}"
    assert decision.events == (owner,)
    assert watcher.step(1.0, _seen(WILD, owner=owner)).events == ()
    # The owner goes away and our server is back.
    assert watcher.step(1.5, _seen(WILD)).route == f"up: {GULCH}"


def test_owner_wins_even_before_anything_is_pinned():
    watcher = _watcher()
    owner = "5173 is already forwarded as web.localhost"
    decision = watcher.step(0.0, _seen(WILD, owner=owner))
    assert decision.bridge is False
    assert decision.route == f"waiting: {owner}"
    assert watcher.pinned_directory is None


def test_relay_drops_immediately_bridge_after_grace():
    watcher = _watcher(needed=True)
    up = watcher.step(0.0, _seen(V4))
    assert up.bridge is True
    assert up.relay_to == V4
    gone = watcher.step(1.0, _empty())
    assert gone.bridge is True
    assert gone.relay_to is None
    still = watcher.step(5.9, _empty())
    assert still.bridge is True
    assert still.relay_to is None
    down = watcher.step(6.0, _empty())
    assert down.bridge is False
    assert down.relay_to is None
    assert down.route == "waiting: nothing listening on 5173"
    assert down.events == (NOTHING,)
    assert watcher.step(6.5, _empty()).events == ()


def test_short_gap_never_lowers_bridge():
    watcher = _watcher(needed=True)
    watcher.step(0.0, _seen(V4))
    decisions = [watcher.step(t, _empty()) for t in (0.5, 1.0, 2.0, 4.0)]
    restarted = ports.PortListener("127.0.0.1", 5173, "101")
    decisions.append(watcher.step(4.5, _seen(restarted, pid=42)))
    assert all(d.bridge for d in decisions)
    assert all(d.route == f"up: {GULCH}" for d in decisions)
    assert all(d.events == () for d in decisions)
    assert decisions[-1].relay_to == restarted
    # A later gap is measured from its own start, not the first one.
    watcher.step(5.0, _empty())
    assert watcher.step(9.9, _empty()).bridge is True
    assert watcher.step(10.0, _empty()).bridge is False


def test_wildcard_listener_needs_no_relay():
    calls = []
    watcher = _watcher(needed=True, calls=calls)
    decision = watcher.step(0.0, _seen(WILD, V4))
    assert decision.bridge is True
    assert decision.relay_to is None
    assert calls == []


def test_relay_retargets_from_ipv4_to_ipv6_loopback():
    watcher = _watcher(needed=True)
    assert watcher.step(0.0, _seen(V4)).relay_to == V4
    decision = watcher.step(0.5, _seen(V6, pid=42))
    assert decision.bridge is True
    assert decision.relay_to == V6
    assert decision.route == f"up: {GULCH}"


def test_relay_prefers_ipv4_when_both_loopbacks_listen():
    watcher = _watcher(needed=True)
    assert watcher.step(0.0, _seen(V6, V4)).relay_to == V4


def test_relay_not_needed_leaves_relay_down():
    watcher = _watcher(needed=False)
    decision = watcher.step(0.0, _seen(V4))
    assert decision.bridge is True
    assert decision.relay_to is None


def test_relay_impossible_reports_failing():
    watcher = _watcher(needed=True, possible=False)
    decision = watcher.step(0.0, _seen(V4))
    failing = "no relay possible: the server must listen on a wider address"
    assert decision.bridge is False
    assert decision.relay_to is None
    assert decision.route == f"failing: {failing}"
    assert decision.events == (failing,)
    assert watcher.step(0.5, _seen(V4)).events == ()
    # The server rebinds to a wildcard address: no relay is needed any more.
    decision = watcher.step(1.0, _seen(WILD, pid=42))
    assert decision.bridge is True
    assert decision.route == f"up: {GULCH}"


def test_probe_only_port_counts_as_listening():
    watcher = _watcher(needed=True)
    decision = watcher.step(0.0, _seen(probe_ok=True, directory=None))
    assert decision.bridge is True
    assert decision.relay_to is None
    assert decision.route == "up"
    assert decision.events == (CANNOT_TELL,)


def test_relay_needed_called_only_on_transitions():
    calls = []
    watcher = _watcher(needed=True, calls=calls)
    watcher.step(0.0, _seen(V4))
    assert calls == ["needed", "possible"]
    for t in (0.5, 1.0, 1.5):
        watcher.step(t, _seen(V4))
    assert calls == ["needed", "possible"]
    # A new inode is a transition.
    watcher.step(2.0, _seen(V6, pid=42))
    assert calls == ["needed", "possible"] * 2
    watcher.step(2.5, _seen(V6, pid=42))
    # Server gone past the grace, then back: the bridge going up is one.
    watcher.step(3.0, _empty())
    watcher.step(9.0, _empty())
    assert calls == ["needed", "possible"] * 2
    watcher.step(9.5, _seen(V6, pid=42))
    assert calls == ["needed", "possible"] * 3


def test_relay_possible_not_asked_while_failing():
    calls = []
    watcher = _watcher(needed=True, possible=False, calls=calls)
    for t in (0.0, 0.5, 1.0):
        watcher.step(t, _seen(V4))
    assert calls == ["needed", "possible"]
