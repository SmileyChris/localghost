"""Forward-specific logic that is neither CLI nor the leader's IO."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from . import ports, sessions
from .paths import hub_http_port, hub_https_port

# `127.0.0.1:8080->80/tcp`, `[::1]:8080->80/tcp` or a range such as
# `0.0.0.0:8000-8002->8000-8002/tcp`; an unpublished `80/tcp` has no `->`.
_PUBLISHED = re.compile(r":(\d+)(?:-(\d+))?->")


def port_owner(port: int, *, exclude_session: str | None = None) -> str | None:
    """Say why localghost already routes `port`, or None if it does not."""
    if port in (hub_http_port(), hub_https_port()):
        return f"{port} is the localghost hub's port"
    for other in sessions.sessions():
        if other.port != port or not sessions.alive(other):
            continue
        if other.mode == "host":
            return (
                f"{port} is {other.name}'s port; "
                f"it is already at {other.name}.localhost"
            )
        if other.mode == "forward" and other.id != exclude_session:
            return f"{port} is already forwarded as {other.name}.localhost"
    container = _docker_publisher(port)
    if container:
        return f"{port} is published by {container}, already routed by localghost"
    return None


def _docker_publisher(port: int) -> str | None:
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "network=localghost",
                "--format",
                "{{.Names}}\t{{.Ports}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        name, _, ports = line.partition("\t")
        for entry in ports.split(","):
            match = _PUBLISHED.search(entry)
            if match and int(match[1]) <= port <= int(match[2] or match[1]):
                return name
    return None


_NO_RELAY = "no relay possible: the server must listen on a wider address"


@dataclass(frozen=True)
class Observation:
    """One look at the forwarded port, as the leader's IO saw it."""

    listeners: tuple[ports.PortListener, ...]  # already excluding our own
    probe_ok: bool  # TCP probe answered; used when listeners is empty
    holder_directory: str | None  # None: unknown or unreadable
    holder_pid: int | None
    owner: str | None  # port_owner(...) result


@dataclass(frozen=True)
class Decision:
    """What the leader should have up after a step, and what to log."""

    bridge: bool
    relay_to: ports.PortListener | None
    route: str
    events: tuple[str, ...]


class Watcher:
    """Decide from observations whether a forward's route should be up.

    Pure: the clock and the observations come in, decisions go out. The
    relay callables are asked only when the set of listening sockets changes
    or the route comes back, never on every step.
    """

    GRACE_SECONDS = 5.0

    def __init__(
        self,
        name: str,
        port: int,
        *,
        relay_needed: Callable[[], bool],
        relay_possible: Callable[[], bool],
    ) -> None:
        self.name = name
        self.port = port
        self.pinned_directory: str | None = None
        self._relay_needed = relay_needed
        self._relay_possible = relay_possible
        self._bridge = False
        self._relay_to: ports.PortListener | None = None
        self._route = ""
        self._gone_since: float | None = None
        # (inodes, relay needed, relay possible) for the listeners last asked
        # about; None forces the callables to be asked again.
        self._relay_check: tuple[frozenset[str], bool, bool] | None = None
        self._said_cannot_tell = False

    def step(self, now: float, seen: Observation) -> Decision:
        events: list[str] = []
        if not seen.listeners and not seen.probe_ok:
            self._nothing_listening(now, events)
        else:
            self._gone_since = None
            self._listening(seen, events)
        return Decision(
            bridge=self._bridge,
            relay_to=self._relay_to,
            route=self._route,
            events=tuple(events),
        )

    def _nothing_listening(self, now: float, events: list[str]) -> None:
        self._relay_to = None
        self._relay_check = None
        if self._gone_since is None:
            self._gone_since = now
        if self._bridge and now - self._gone_since < self.GRACE_SECONDS:
            return  # a reload's gap: keep the bridge and the route as they are
        self._bridge = False
        self._set_route(
            f"waiting: nothing listening on {self.port}",
            f"nothing is listening on {self.port} yet; "
            f"{self.name}.localhost will serve it once something does",
            events,
        )

    def _listening(self, seen: Observation, events: list[str]) -> None:
        directory = seen.holder_directory
        if seen.owner is not None:
            self._lower(f"waiting: {seen.owner}", seen.owner, events)
            return
        if (
            directory is not None
            and self.pinned_directory is not None
            and directory != self.pinned_directory
        ):
            self._lower(
                f"waiting: {self.port} held by {directory}",
                f"port {self.port} is held by {directory} "
                f"(pid {seen.holder_pid}); not routing {self.name}",
                events,
            )
            return
        if directory is None:
            if not self._said_cannot_tell:
                self._said_cannot_tell = True
                events.append(
                    f"cannot tell which process owns {self.port}; "
                    f"{self.name}.localhost will serve whatever listens there"
                )
        elif self.pinned_directory is None:
            self.pinned_directory = directory

        relay_to = self._relay_target(seen.listeners)
        if relay_to is False:
            self._bridge = False
            self._relay_to = None
            self._set_route(f"failing: {_NO_RELAY}", _NO_RELAY, events)
            return
        self._bridge = True
        self._relay_to = relay_to
        if directory is None:
            self._set_route("up", None, events)
        else:
            self._set_route(
                f"up: {directory}",
                f"forwarding to {self.port}, held by {directory}",
                events,
            )

    def _relay_target(
        self, listeners: tuple[ports.PortListener, ...]
    ) -> ports.PortListener | None | bool:
        """The loopback listener to relay to, None for none, False if impossible."""
        loopback = [item for item in listeners if ports._is_loopback(item.host)]
        if not loopback or len(loopback) != len(listeners):
            # Probe only (nothing to relay to) or a wider bind the hub reaches.
            return None
        inodes = frozenset(item.inode for item in listeners)
        if self._relay_check is None or self._relay_check[0] != inodes:
            needed = self._relay_needed()
            possible = self._relay_possible() if needed else True
            self._relay_check = (inodes, needed, possible)
        _, needed, possible = self._relay_check
        if not needed:
            return None
        if not possible:
            return False
        # IPv4 first, so the choice does not depend on /proc's order.
        return min(loopback, key=lambda item: (":" in item.host, item.host))

    def _lower(self, route: str, event: str, events: list[str]) -> None:
        self._bridge = False
        self._relay_to = None
        self._relay_check = None
        self._set_route(route, event, events)

    def _set_route(self, route: str, event: str | None, events: list[str]) -> None:
        if route != self._route:
            self._route = route
            if event is not None:
                events.append(event)
