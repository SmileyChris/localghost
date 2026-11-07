"""Background leader for a forward: route a hostname to whatever serves a port.

A forward has no command of its own to supervise. The server on its port is
started, restarted and stopped by someone else -- an IDE, a debugger, a dev
server's own reloader -- so this stays behind in its place: it polls the
port, hands each look to `forwards.Watcher`, and raises or lowers the Docker
bridge and the gateway relay as the watcher decides. Every change is written
to the session record's `route` (which the status bar reads) and to the
registry's `forward_state` (which the ghost page reads), and logged as one
line on stdout, which is the session log.

The CLI writes the session record first and then starts this, so the record
may take a moment to appear. `sessions stop` sends SIGTERM; the relay and the
bridge come down, the registry says `stopped`, and the record is left for
`sessions stop` to remove.

Usage: python -m localghost.forward_leader SESSION_ID
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import forwarder, forwards, ports, registry, sessions
from .generator import render_override
from .runner import create_run_bridge_compose
from .statusbar import tcp_probe

_PROC_POLL = 0.5
_PROBE_POLL = 2.0
_RECORD_WAIT = 5.0
_RECORD_POLL = 0.1


class Stopped(Exception):
    """SIGTERM or SIGINT arrived: leave cleanly."""


class BridgeError(Exception):
    """`docker compose` could not raise or lower the bridge."""


def _stop(signum: int, frame: object) -> None:
    raise Stopped


def bridge_project(name: str) -> str:
    return f"localghost-forward-{name}"


def _compose(name: str, bridge_yaml: str, action: list[str]) -> None:
    """Run `docker compose` on the fileless bridge project, as `runner` does."""
    command = [
        "docker",
        "compose",
        "--project-name",
        bridge_project(name),
        "--file",
        "-",
        *action,
    ]
    try:
        result = subprocess.run(
            command,
            input=bridge_yaml,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise BridgeError(str(exc) or "docker is required") from exc
    if result.returncode:
        lines = (result.stderr or "").strip().splitlines()
        raise BridgeError(
            lines[0].strip() if lines else f"docker compose exited {result.returncode}"
        )


def _tilde(directory: str) -> str:
    home = str(Path.home())
    if directory == home:
        return "~"
    if directory.startswith(home.rstrip("/") + "/"):
        return "~" + directory[len(home.rstrip("/")) :]
    return directory


class _Observer:
    """One real look at the forwarded port each call.

    `holder` and `port_owner` are only asked again when the set of listening
    sockets or the probe's answer changes, never on every poll. Set
    `relay_bind` while a relay is up, so its socket on the gateway address is
    never mistaken for the server.
    """

    def __init__(self, session: sessions.Session) -> None:
        self.session_id = session.id
        self.port = session.port
        self.relay_bind: str | None = None
        self.proc = ports._proc_available()
        self._key: object = None
        self._holder: ports.Holder | None = None
        self._owner: str | None = None

    def __call__(self) -> forwards.Observation:
        listeners: tuple[ports.PortListener, ...] = ()
        if self.proc:
            listeners = tuple(
                ports.listeners_on(
                    self.port,
                    exclude_pids=frozenset({os.getpid()}),
                    exclude_hosts=(
                        frozenset({self.relay_bind}) if self.relay_bind else frozenset()
                    ),
                )
            )
        probe_ok = not listeners and tcp_probe(self.port)()
        inodes = frozenset(item.inode for item in listeners)
        key = (inodes, probe_ok)
        if key != self._key:
            self._key = key
            self._holder = ports.holder(self.port, set(inodes)) if inodes else None
            self._owner = (
                forwards.port_owner(self.port, exclude_session=self.session_id)
                if listeners or probe_ok
                else None
            )
        directory = self._holder.directory if self._holder else None
        return forwards.Observation(
            listeners=listeners,
            probe_ok=probe_ok,
            holder_directory=_tilde(directory) if directory else None,
            holder_pid=self._holder.pid if self._holder else None,
            owner=self._owner,
        )


def _load(
    session_id: str, clock: Callable[[], float], sleep: Callable[[float], None]
) -> sessions.Session | None:
    """The leader's record; the CLI may still be writing it."""
    deadline = clock() + _RECORD_WAIT
    while True:
        session = sessions._read(session_id)
        if session is not None or clock() >= deadline:
            return session
        sleep(_RECORD_POLL)


def _say(line: str) -> None:
    print(line, flush=True)


class _Leader:
    """Keep the bridge, the relay and the written state in line with decisions."""

    def __init__(
        self,
        session: sessions.Session,
        *,
        bridge_yaml: str,
        bridge_up: Callable[[], None],
        bridge_down: Callable[[], None],
        observer: _Observer | None,
    ) -> None:
        self.session = session
        self.name = session.name
        self.port = session.port
        self.bridge_yaml = bridge_yaml
        self._bridge_up = bridge_up
        self._bridge_down = bridge_down
        self._observer = observer
        self.relays = contextlib.ExitStack()
        self.relay_to: ports.PortListener | None = None
        self.wanted_bridge = False
        self.bridge_is_up = False
        self.bridge_touched = False  # anything to lower on the way out
        self.failure: str | None = None  # the bridge's, until the next transition
        self.relay_failure: str | None = None  # the relay's, until its target moves
        self._written: tuple[str, bool] | None = None
        self._forward_state: object = ()  # never a real state: first write goes
        self._registry_warned = False
        self._route_warned = False
        self._down_failed = False

    def apply(self, decision: forwards.Decision) -> None:
        for event in decision.events:
            _say(event)
        if decision.relay_to != self.relay_to:
            self._move_relay(decision.relay_to)
        if decision.bridge and not self.wanted_bridge:
            self.failure = None
            self._raise_bridge()
        elif not decision.bridge and self.wanted_bridge:
            self.failure = None
            self._lower_bridge()
        elif not decision.bridge and self.bridge_is_up:
            self._lower_bridge()  # an earlier `down` failed: try again
        self.wanted_bridge = decision.bridge
        route = decision.route
        if self.failure is not None and decision.bridge:
            route = f"failing: {self.failure}"
        elif self.relay_failure is not None and decision.relay_to is not None:
            route = f"failing: {self.relay_failure}"
        self._write(route)

    def _move_relay(self, target: ports.PortListener | None) -> None:
        self.relays.close()
        self.relay_to = None
        self.relay_failure = None
        if self._observer is not None:
            self._observer.relay_bind = None
        if target is None:
            return
        address = forwarder.gateway()
        try:
            if address is None:
                raise OSError("no Docker gateway address")
            self.relays.enter_context(
                forwarder.forwarding(
                    ports.Listener(target.host, target.port),
                    bind=address,
                    port=self.port,
                )
            )
        except OSError as exc:
            self.relay_failure = f"could not start the relay: {exc}"
            _say(self.relay_failure)
            # Remember the target anyway: retry when the watcher changes it,
            # not on every poll.
            self.relay_to = target
            return
        self.relay_to = target
        if self._observer is not None:
            self._observer.relay_bind = address
        _say(
            f"relaying {address}:{self.port} to {target.host}:{self.port} "
            "so the hub can reach it"
        )

    def _raise_bridge(self) -> None:
        self.bridge_touched = True
        try:
            self._bridge_up()
        except BridgeError as exc:
            self.failure = f"could not start the bridge: {exc}"
            _say(self.failure)
            return
        self.bridge_is_up = True

    def _lower_bridge(self) -> None:
        try:
            self._bridge_down()
        except BridgeError as exc:
            # Its containers may still run: keep it on record, so the next
            # poll and `sessions stop` can still find and lower it.
            if not self.bridge_is_up or not self._down_failed:
                _say(f"could not stop the bridge: {exc}")
            self._down_failed = True
            self.bridge_is_up = True
            return
        if self._down_failed:
            _say("stopped the bridge")
        self._down_failed = False
        self.bridge_is_up = False
        self.bridge_touched = False

    def _write(self, route: str) -> None:
        up = self.bridge_is_up
        if self._written != (route, up):
            try:
                sessions.set_route(
                    self.session.id,
                    route,
                    bridge_project=bridge_project(self.name) if up else None,
                    bridge_yaml=self.bridge_yaml if up else "",
                )
            except OSError as exc:
                if not self._route_warned:
                    self._route_warned = True
                    _say(f"localghost: could not record the route: {exc}")
            else:
                self._written = (route, up)
        self.set_forward_state(None if up else "waiting")

    def set_forward_state(self, state: str | None) -> None:
        if state == self._forward_state:
            return
        self._forward_state = state
        error = registry.set_forward_state(self.name, state, warn=False)
        if error is not None and not self._registry_warned:
            self._registry_warned = True
            _say(
                f"localghost: could not update {self.name} for ghost pages: {error}; "
                "routing carries on"
            )

    def close(self) -> None:
        self.relays.close()
        self.relay_to = None
        if self.bridge_touched:
            self._lower_bridge()
        self.set_forward_state("stopped")


def main(
    argv: list[str],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    observe: Callable[[], forwards.Observation] | None = None,
    bridge_up: Callable[[], None] | None = None,
    bridge_down: Callable[[], None] | None = None,
) -> int:
    session_id = argv[0]
    previous = {
        signum: signal.signal(signum, _stop)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    leader: _Leader | None = None
    try:
        session = _load(session_id, clock, sleep)
        if session is None:
            _say(f"localghost: no session record {session_id}")
            return 1
        observer = None
        if observe is None:
            observer = _Observer(session)
            observe = observer
        bridge_yaml = render_override(
            create_run_bridge_compose(session.name, session.port)
        )
        leader = _Leader(
            session,
            bridge_yaml=bridge_yaml,
            bridge_up=bridge_up
            or (lambda: _compose(session.name, bridge_yaml, ["up", "--detach"])),
            bridge_down=bridge_down
            or (
                lambda: _compose(
                    session.name, bridge_yaml, ["down", "--remove-orphans"]
                )
            ),
            observer=observer,
        )
        watcher = forwards.Watcher(
            session.name,
            session.port,
            relay_needed=lambda: not forwarder.hub_reaches_loopback(),
            relay_possible=forwarder.available,
        )
        interval = _PROC_POLL if ports._proc_available() else _PROBE_POLL
        while True:
            leader.apply(watcher.step(clock(), observe()))
            sleep(interval)
    except Stopped:
        return 0
    finally:
        # A second signal must not cut the cleanup short.
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        try:
            if leader is not None:
                leader.close()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
