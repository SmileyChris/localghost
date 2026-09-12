"""Host port selection, and naming whoever already holds a port."""

from __future__ import annotations

import errno
import glob
import ipaddress
import os
import socket
import subprocess
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


# How far past a busy port selection looks for a free one. Dev servers that
# choose their own replacement walk upward the same way, which is what lets a
# run recognise where one landed after ignoring the port it was given.
WALK = 100


def select_port(port: int, strict: bool) -> int:
    if port_available(port):
        return port
    if strict:
        raise click.ClickException(in_use_message(port))
    for candidate in range(port + 1, min(port + WALK, 65536)):
        if port_available(candidate):
            return candidate
    raise click.ClickException(
        f"no free host port found from {port} through "
        f"{min(port + WALK - 1, 65535)}"
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


@dataclass(frozen=True)
class Listener:
    """A socket a process group is accepting connections on."""

    host: str
    port: int


def _pids_in_group(pgid: int) -> list[int]:
    found = []
    for entry in glob.glob("/proc/[0-9]*/stat"):
        try:
            with open(entry, encoding="utf-8", errors="replace") as handle:
                data = handle.read()
        except OSError:
            continue
        try:
            # comm can hold spaces and brackets; the fields after the last
            # ")" are the only ones that can be split safely.
            fields = data[data.rindex(")") + 2 :].split()
            if int(fields[2]) == pgid:
                found.append(int(entry.split("/")[2]))
        except (ValueError, IndexError):
            continue
    return found


def _group_socket_inodes(pgid: int) -> set[str]:
    inodes = set()
    for pid in _pids_in_group(pgid):
        for descriptor in glob.glob(f"/proc/{pid}/fd/*"):
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    return inodes


def _decode_address(packed: str) -> str:
    raw = bytes.fromhex(packed)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    # /proc stores IPv6 as four little-endian words.
    ordered = b"".join(raw[index : index + 4][::-1] for index in range(0, 16, 4))
    return socket.inet_ntop(socket.AF_INET6, ordered)


def _proc_available() -> bool:
    return os.path.exists("/proc/net/tcp")


# lsof ships with macOS and most Linux distributions, understands process
# groups directly, and its -F output is designed to be parsed.
_LSOF_QUERY = ("lsof", "-a", "-g", "{pgid}", "-iTCP", "-sTCP:LISTEN", "-nP", "-Fn")


def _lsof_listeners(pgid: int) -> list[Listener]:
    command = tuple(part.format(pgid=pgid) for part in _LSOF_QUERY)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False
        )
    except OSError:
        return []
    found = []
    for line in result.stdout.splitlines():
        if not line.startswith("n"):
            continue
        host, _, port = line[1:].rpartition(":")
        # lsof writes a wildcard bind as "*", which has to mean what the
        # /proc reader calls 0.0.0.0 or the loopback verdict would flip.
        host = "0.0.0.0" if host == "*" else host.strip("[]")
        try:
            found.append(Listener(host, int(port)))
        except ValueError:
            continue
    return found


def listeners(pgid: int) -> list[Listener]:
    """Every address a process group is listening on, as far as it can be read.

    /proc answers this without spawning anything, so it is preferred where it
    exists. Elsewhere -- macOS above all -- lsof answers the same question,
    which keeps a run diagnosable on hosts the /proc reader cannot serve.
    """
    if not _proc_available():
        return _lsof_listeners(pgid)
    inodes = _group_socket_inodes(pgid)
    if not inodes:
        return []
    found = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as handle:
                rows = handle.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) > 9 and fields[3] == _LISTEN and fields[9] in inodes:
                packed, port = fields[1].rsplit(":", 1)
                found.append(Listener(_decode_address(packed), int(port, 16)))
    return found


def loopback_only(pgid: int, port: int) -> Listener | None:
    """A listener to blame when nothing the group opened is reachable.

    The hub reaches the host from inside a container, over its gateway
    address, which no loopback socket will accept. An application that binds
    only loopback is therefore up and working for direct use while its public
    URL can never resolve -- a state the readiness probe alone reports as
    "still starting", forever.

    Only `port` is judged. A run routinely has other processes in the group
    binding loopback on purpose -- a database the application talks to over
    127.0.0.1, say -- and one of those appearing first during boot must not
    condemn the application that has not finished starting.

    None means there is nothing to say: either nothing holds `port` yet, or
    what holds it is reachable.
    """
    opened = [item for item in listeners(pgid) if item.port == port]
    if not opened:
        return None
    if any(not _is_loopback(item.host) for item in opened):
        return None
    return opened[0]


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
