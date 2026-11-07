from __future__ import annotations

import contextlib
import socket
import sys

import pytest

from localghost import forward_leader, forwarder, forwards, ports, registry, sessions

NAME = "api"
PORT = 5173
SERVER = ports.PortListener("127.0.0.1", PORT, "100")
NOTHING = forwards.Observation((), False, None, None, None)
GULCH = forwards.Observation((SERVER,), False, "~/dev/lab/gulch", 42, None)


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    # Nothing here may reach Docker: no relay is needed unless a test says so.
    monkeypatch.setattr(forwarder, "hub_reaches_loopback", lambda: True)
    monkeypatch.setattr(forwarder, "available", lambda: True)
    monkeypatch.setattr(forwarder, "gateway", lambda: "192.0.2.1")
    monkeypatch.setattr(forwards, "port_owner", lambda *a, **k: None)


def _forward(tmp_path) -> sessions.Session:
    registry.record_forward(NAME, PORT, detached=True)
    return sessions.create(
        mode="forward",
        name=NAME,
        port=PORT,
        cwd=None,
        command=(),
        log=tmp_path / "forward.log",
        pid=None,
    )


class Script:
    """A fake clock whose every sleep moves on to the next observation."""

    def __init__(self, observations, *, on_poll=None):
        self.now = 0.0
        self.observations = list(observations)
        self.polls = 0
        self.on_poll = on_poll
        self.bridge: list[str] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_poll is not None:
            self.on_poll(self)
        if self.polls >= len(self.observations) - 1:
            raise forward_leader.Stopped
        self.polls += 1

    def observe(self) -> forwards.Observation:
        return self.observations[self.polls]

    def up(self) -> None:
        self.bridge.append("up")

    def down(self) -> None:
        self.bridge.append("down")


def _run(session, script, **overrides) -> int:
    options = dict(
        clock=script.clock,
        sleep=script.sleep,
        observe=script.observe,
        bridge_up=script.up,
        bridge_down=script.down,
    )
    options.update(overrides)
    return forward_leader.main([session.id], **options)


def _record(session) -> sessions.Session:
    return sessions.read(session.id)


def _state_of(name=NAME):
    return next(item for item in registry.entries() if item.name == name).forward_state


def test_leader_raises_bridge_when_server_appears(tmp_path, capsys):
    session = _forward(tmp_path)
    seen = []

    def check(script):
        seen.append((_record(session).route, _record(session).bridge_project))

    script = Script([NOTHING, GULCH, GULCH], on_poll=check)
    assert _run(session, script) == 0
    assert script.bridge[0] == "up"
    assert seen[0] == (f"waiting: nothing listening on {PORT}", None)
    assert seen[1] == ("up: ~/dev/lab/gulch", f"localghost-forward-{NAME}")
    out = capsys.readouterr().out
    assert (
        f"nothing is listening on {PORT} yet; "
        f"{NAME}.localhost will serve it once something does"
    ) in out
    assert f"forwarding to {PORT}, held by ~/dev/lab/gulch" in out
    assert out.count("forwarding to") == 1


def test_leader_writes_the_bridge_yaml_while_up(tmp_path):
    session = _forward(tmp_path)
    seen = []
    script = Script(
        [GULCH, GULCH], on_poll=lambda _: seen.append(_record(session).bridge_yaml)
    )
    _run(session, script)
    assert f"Host(`{NAME}.localhost`)" in seen[0]
    assert f"host.docker.internal:{PORT}" in seen[0]


def test_leader_lowers_bridge_after_grace_and_marks_waiting(tmp_path):
    session = _forward(tmp_path)
    states = []
    script = Script(
        [GULCH] + [NOTHING] * 14,
        on_poll=lambda s: states.append(
            (s.now, list(s.bridge), _state_of(), _record(session).bridge_project)
        ),
    )
    routes = []
    polled = script.on_poll
    script.on_poll = lambda s: (polled(s), routes.append(_record(session).route))
    _run(session, script)
    assert states[0][1:] == (["up"], None, f"localghost-forward-{NAME}")
    # Inside the grace period the bridge stays up.
    within = [item for item in states if item[0] <= 5.0]
    assert all(item[1] == ["up"] for item in within)
    after = states[-1]
    assert after[1] == ["up", "down"]
    assert after[2] == "waiting"
    assert after[3] is None
    assert routes[-1] == f"waiting: nothing listening on {PORT}"


def test_leader_survives_bridge_failure_and_reports_failing(tmp_path, capsys):
    session = _forward(tmp_path)
    attempts = []

    def failing_up():
        attempts.append("up")
        if len(attempts) == 1:
            raise forward_leader.BridgeError("no such image: caddy")

    routes = []
    script = Script(
        [GULCH, GULCH, NOTHING] + [NOTHING] * 11 + [GULCH, GULCH],
        on_poll=lambda _: routes.append(_record(session).route),
    )
    assert _run(session, script, bridge_up=failing_up) == 0
    assert routes[0] == "failing: could not start the bridge: no such image: caddy"
    assert routes[1] == routes[0]  # not retried every poll
    assert len(attempts) == 2  # retried on the next transition
    assert routes[-1] == "up: ~/dev/lab/gulch"
    assert "could not start the bridge: no such image: caddy" in capsys.readouterr().out


def test_leader_bridge_failure_reads_the_first_stderr_line(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return forward_leader.subprocess.CompletedProcess(
            command, 1, stdout="", stderr="pull access denied\nmore\n"
        )

    monkeypatch.setattr(forward_leader.subprocess, "run", run)
    with pytest.raises(forward_leader.BridgeError, match="^pull access denied$"):
        forward_leader._compose(NAME, "yaml: 1\n", ["up", "--detach"])
    command, kwargs = calls[0]
    assert command == [
        "docker",
        "compose",
        "--project-name",
        f"localghost-forward-{NAME}",
        "--file",
        "-",
        "up",
        "--detach",
    ]
    assert kwargs["input"] == "yaml: 1\n"


def test_leader_cleans_up_and_marks_stopped_on_sigterm(tmp_path, monkeypatch):
    session = _forward(tmp_path)
    monkeypatch.setattr(forwarder, "hub_reaches_loopback", lambda: False)
    closed = []

    @contextlib.contextmanager
    def relay(target, *, bind, port):
        yield
        closed.append(target)

    monkeypatch.setattr(forwarder, "forwarding", relay)
    script = Script([GULCH, GULCH])
    assert _run(session, script) == 0
    assert closed == [ports.Listener("127.0.0.1", PORT)]
    assert script.bridge == ["up", "down"]
    assert _state_of() == "stopped"
    # sessions.stop removes the record, not the leader.
    assert _record(session) is not None
    # Asked to stop, so a following terminal reports a stop, not a crash.
    assert _record(session).route == sessions.STOPPED_ROUTE
    # The lowered bridge is off record, so `sessions stop` has none to lower.
    assert _record(session).bridge_project is None
    assert _record(session).bridge_yaml == ""


def test_leader_keeps_a_bridge_on_record_when_its_last_down_fails(tmp_path):
    session = _forward(tmp_path)

    def down():
        raise forward_leader.BridgeError("daemon busy")

    assert _run(session, Script([GULCH, GULCH]), bridge_down=down) == 0
    assert _record(session).bridge_project == f"localghost-forward-{NAME}"
    assert _record(session).bridge_yaml
    assert _state_of() == "stopped"


def test_leader_that_crashes_does_not_record_a_stop(tmp_path):
    session = _forward(tmp_path)

    class Crash(Exception):
        pass

    def observe():
        raise Crash

    with pytest.raises(Crash):
        _run(session, Script([GULCH]), observe=observe)
    assert _record(session).route != sessions.STOPPED_ROUTE
    assert _state_of() == "stopped"


def test_leader_signal_handler_raises_stopped():
    with pytest.raises(forward_leader.Stopped):
        forward_leader._stop(15, None)


def test_leader_keeps_routing_when_registry_unwritable(tmp_path, monkeypatch, capsys):
    session = _forward(tmp_path)

    def unwritable(entry):
        raise PermissionError("read-only registry")

    monkeypatch.setattr(registry, "_write", unwritable)
    routes = []
    script = Script(
        [NOTHING, GULCH] + [NOTHING] * 12 + [GULCH],
        on_poll=lambda s: routes.append((_record(session).route, list(s.bridge))),
    )
    assert _run(session, script) == 0
    assert routes[1] == ("up: ~/dev/lab/gulch", ["up"])
    assert routes[-1] == ("up: ~/dev/lab/gulch", ["up", "down", "up"])
    captured = capsys.readouterr()
    log = captured.out + captured.err
    assert log.count("read-only registry") == 1


def test_leader_relays_to_ipv6_loopback(tmp_path, monkeypatch, capsys):
    session = _forward(tmp_path)
    monkeypatch.setattr(forwarder, "hub_reaches_loopback", lambda: False)
    opened = []

    @contextlib.contextmanager
    def relay(target, *, bind, port):
        opened.append((target, bind, port))
        yield

    monkeypatch.setattr(forwarder, "forwarding", relay)
    v6 = ports.PortListener("::1", PORT, "200")
    seen = forwards.Observation((v6,), False, "~/dev/lab/gulch", 42, None)
    script = Script([seen, seen, seen])
    _run(session, script)
    assert opened == [(ports.Listener("::1", PORT), "192.0.2.1", PORT)]
    assert f"192.0.2.1:{PORT}" in capsys.readouterr().out


def test_leader_moves_the_relay_to_a_new_target(tmp_path, monkeypatch):
    session = _forward(tmp_path)
    monkeypatch.setattr(forwarder, "hub_reaches_loopback", lambda: False)
    events = []

    @contextlib.contextmanager
    def relay(target, *, bind, port):
        events.append(("open", target.host))
        yield
        events.append(("close", target.host))

    monkeypatch.setattr(forwarder, "forwarding", relay)
    v6 = forwards.Observation(
        (ports.PortListener("::1", PORT, "200"),), False, "~/dev/lab/gulch", 42, None
    )
    script = Script([GULCH, v6])
    _run(session, script)
    assert events == [
        ("open", "127.0.0.1"),
        ("close", "127.0.0.1"),
        ("open", "::1"),
        ("close", "::1"),
    ]


def test_leader_waits_for_its_session_record(tmp_path):
    session = sessions.Session(
        id="late", mode="forward", name=NAME, port=PORT, cwd=None, pid=None, log=""
    )
    registry.record_forward(NAME, PORT, detached=True)
    script = Script([GULCH, GULCH])
    waited = []

    def sleep(seconds):
        waited.append(seconds)
        script.now += seconds
        if script.now >= 1.0 and _record(session) is None:
            sessions.save(session)

    assert (
        forward_leader.main(
            ["late"],
            clock=script.clock,
            sleep=lambda s: sleep(s) if _record(session) is None else script.sleep(s),
            observe=script.observe,
            bridge_up=script.up,
            bridge_down=script.down,
        )
        == 0
    )
    assert waited
    assert script.bridge == ["up", "down"]


def test_leader_gives_up_without_a_session_record(tmp_path, capsys):
    script = Script([GULCH])
    assert (
        forward_leader.main(
            ["missing"],
            clock=script.clock,
            sleep=lambda s: setattr(script, "now", script.now + s),
            observe=script.observe,
            bridge_up=script.up,
            bridge_down=script.down,
        )
        == 1
    )
    assert 5.0 <= script.now < 6.0
    assert script.bridge == []


def _bindable(host: str) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind((host, 0))
        except OSError:
            return False
    return True


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not ports._proc_available(),
    reason="needs /proc/net/tcp",
)
def test_leader_never_counts_its_own_relay_socket(tmp_path, monkeypatch):
    # 127.0.0.2 stands in for the gateway: bindable, and not what the
    # probe's "localhost" dials.
    if not _bindable("127.0.0.2"):
        pytest.skip("127.0.0.2 is not bindable here")
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    session = sessions.Session(
        id="own", mode="forward", name=NAME, port=port, cwd=None, pid=None, log=""
    )
    target = ports.Listener("127.0.0.1", port)
    with forwarder.forwarding(target, bind="127.0.0.2", port=port):
        assert ports.listeners_on(port), "the relay should really be listening"
        observer = forward_leader._Observer(session)
        # The leader's own pid alone hides it ...
        assert observer().listeners == ()
        # ... and so does the gateway address, while a relay is up.
        assert ports.listeners_on(port, exclude_hosts=frozenset({"127.0.0.2"})) == []
        observer.relay_bind = "127.0.0.2"
        seen = observer()
        assert seen.listeners == ()
        assert seen.probe_ok is False


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not ports._proc_available(),
    reason="needs /proc/net/tcp",
)
def test_observer_asks_holder_and_owner_only_on_change(tmp_path, monkeypatch):
    session = sessions.Session(
        id="obs", mode="forward", name=NAME, port=PORT, cwd=None, pid=None, log=""
    )
    listening = [[SERVER]]
    monkeypatch.setattr(
        forward_leader.ports, "listeners_on", lambda *a, **k: list(listening[0])
    )
    holders = []
    owners = []
    home = str(forward_leader.Path.home())

    def holder(port, inodes=None):
        holders.append(inodes)
        return ports.Holder(pid=42, command="vite", directory=f"{home}/dev/lab/gulch")

    monkeypatch.setattr(forward_leader.ports, "holder", holder)
    monkeypatch.setattr(
        forward_leader.forwards,
        "port_owner",
        lambda port, *, exclude_session=None: owners.append(exclude_session),
    )
    observer = forward_leader._Observer(session)
    first = observer()
    observer()
    assert first.holder_directory == "~/dev/lab/gulch"
    assert first.holder_pid == 42
    assert holders == [{"100"}]
    assert owners == ["obs"]
    listening[0] = [ports.PortListener("127.0.0.1", PORT, "101")]
    observer()
    assert holders == [{"100"}, {"101"}]
    assert owners == ["obs", "obs"]


def test_leader_writes_state_only_on_change(tmp_path, monkeypatch):
    session = _forward(tmp_path)
    routes = []
    states = []
    real_route = sessions.set_route
    real_state = registry.set_forward_state

    def set_route(session_id, route, **kwargs):
        routes.append(route)
        real_route(session_id, route, **kwargs)

    def set_forward_state(name, state, **kwargs):
        states.append(state)
        return real_state(name, state, **kwargs)

    monkeypatch.setattr(forward_leader.sessions, "set_route", set_route)
    monkeypatch.setattr(forward_leader.registry, "set_forward_state", set_forward_state)
    _run(session, Script([NOTHING] * 4 + [GULCH] * 4))
    assert routes == [f"waiting: nothing listening on {PORT}", "up: ~/dev/lab/gulch"]
    assert states == ["waiting", None, "stopped"]


def test_leader_retries_a_failed_bridge_down(tmp_path, capsys):
    session = _forward(tmp_path)
    downs = []

    def flaky_down():
        downs.append("down")
        if len(downs) == 1:
            raise forward_leader.BridgeError("daemon busy")

    seen = []
    script = Script(
        [GULCH] + [NOTHING] * 14,
        on_poll=lambda s: seen.append(
            (len(downs), _record(session).bridge_project, _state_of())
        ),
    )
    assert _run(session, script, bridge_down=flaky_down) == 0
    project = f"localghost-forward-{NAME}"
    failed = seen.index(next(item for item in seen if item[0] == 1))
    # The failed `down` leaves the bridge on record, not yet waiting.
    assert seen[failed] == (1, project, None)
    # Retried on the next poll; once it succeeds the record lets go.
    assert seen[failed + 1] == (2, None, "waiting")
    assert len(downs) == 2  # nothing left to lower on exit
    assert capsys.readouterr().out.count("could not stop the bridge: daemon busy") == 1
