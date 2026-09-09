"""Host port selection, and naming whoever already holds a port."""

from __future__ import annotations

import errno
import glob
import os
import socket
import sys
from dataclasses import dataclass

import click

# Treated as "this host has no IPv6" rather than as a busy port.
_NO_IPV6 = frozenset({errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT})


def port_available(port: int) -> bool:
    """Is `port` free in both address families?

    A listener on one family does not block a bind on the other, so checking
    IPv4 alone calls a port free while a Node dev server holds it over IPv6
    loopback -- and the application then quietly starts somewhere else.
    """
    for family, host in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            # No IPv6 on this host, so nothing can be listening over it.
            continue
        with probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family is socket.AF_INET6:
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                probe.bind((host, port))
            except OSError as exc:
                if family is socket.AF_INET6 and exc.errno in _NO_IPV6:
                    continue
                return False
    return True


def select_port(port: int, strict: bool) -> int:
    if port_available(port):
        return port
    if strict:
        raise click.ClickException(in_use_message(port))
    for candidate in range(port + 1, min(port + 100, 65536)):
        if port_available(candidate):
            return candidate
    raise click.ClickException(
        f"no free host port found from {port} through {min(port + 99, 65535)}"
    )


def in_use_message(port: int) -> str:
    """Say a port is taken, naming its holder whenever the host will tell us.

    Naming the holder only diagnoses the dead end, so the way out is always
    stated too, whether or not there was anything to name.
    """
    taken = f"host port {port} is already in use"
    remedy = "stop it, or choose another port with --port"
    found = holder(port)
    detail = []
    if found is not None and found.pid is not None:
        held = f"  held by pid {found.pid}"
        if found.command:
            held += f": {found.command}"
        detail.append(held)
        if found.directory:
            detail.append(f"  working directory: {found.directory}")
    if not detail:
        return f"{taken}; {remedy}"
    return "\n".join([f"{taken}:", *detail, remedy])


@dataclass(frozen=True)
class Holder:
    """Whoever is already listening on a port, as far as we can tell."""

    pid: int | None = None
    command: str | None = None
    directory: str | None = None


# The state column /proc/net/tcp uses for a listening socket.
_LISTEN = "0A"


def _listening_inodes(port: int) -> set[str]:
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as handle:
                rows = handle.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if (
                len(fields) > 9
                and fields[3] == _LISTEN
                and int(fields[1].rsplit(":", 1)[1], 16) == port
            ):
                inodes.add(fields[9])
    return inodes


def holder(port: int) -> Holder | None:
    """Name the process listening on `port`, as far as this host will say.

    Purely diagnostic, and only ever called on a path that is already about
    to fail: a dead end is far easier to act on when it names the project
    that owns the port. Nothing here is authoritative -- another user's
    process hides its descriptors, and a platform without /proc tells us
    nothing at all -- so every step degrades to a vaguer answer rather than
    to an error.
    """
    if not sys.platform.startswith("linux"):
        return None
    inodes = _listening_inodes(port)
    if not inodes:
        return None
    for descriptor in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            target = os.readlink(descriptor)
        except OSError:
            # Another user's process, or one that exited mid-scan.
            continue
        if not target.startswith("socket:[") or target[8:-1] not in inodes:
            continue
        pid = int(descriptor.split("/")[2])
        return Holder(pid=pid, command=_command(pid), directory=_directory(pid))
    # Listening, but owned by somebody we cannot inspect.
    return Holder()


def _command(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cmdline", encoding="utf-8", errors="replace") as handle:
            return handle.read().replace("\0", " ").strip() or None
    except OSError:
        return None


def _directory(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None
