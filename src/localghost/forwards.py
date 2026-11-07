"""Forward-specific logic that is neither CLI nor the leader's IO."""

from __future__ import annotations

import re
import subprocess

from . import sessions
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
