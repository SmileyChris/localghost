"""A host-side relay from the Docker gateway to a loopback application.

The hub reverse-proxies to `host.docker.internal`, reaching the host over the
Docker gateway address. No loopback socket accepts that, so an application
bound to `127.0.0.1` or `[::1]` is unreachable through its public URL however
its port was chosen.

Relaying from the gateway address closes that gap without asking the
application to widen what it bound. The relay claims the same port on the
gateway address, which is free precisely because the application took only
loopback, so the hub's existing route needs no change. Binding that one
address and no other keeps the application off the LAN, which is more than
can be said for telling it to listen on `0.0.0.0`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator

from .ports import Listener

_BUFFER = 65536

# What Docker's `host-gateway` alias resolves to. The default bridge network
# owns that address even for a container attached to another network, so this
# is the address the hub's bridge reaches the host on.
_GATEWAY_QUERY = (
    "docker",
    "network",
    "inspect",
    "bridge",
    "--format",
    '{{range .IPAM.Config}}{{.Gateway}}{{"\\n"}}{{end}}',
)


def hub_reaches_loopback() -> bool:
    """Does the hub's bridge reach a host application bound to loopback?

    Where Docker is native to this host it connects over the host gateway
    address, which no loopback socket accepts, so such an application
    genuinely cannot be served without help. Where Docker runs in a VM, or
    under rootless networking, the connection is instead proxied by a process
    on the host itself, which can reach loopback -- so a loopback bind is not
    known to be a problem there and is left alone rather than condemned on a
    guess. The gateway tells the two apart: a native daemon's gateway is an
    address of this host, and any other daemon's is not.
    """
    if not sys.platform.startswith("linux"):
        return True
    address = gateway()
    return address is not None and not local(address)


def available() -> bool:
    """Can a relay stand in for an application that binds only loopback?

    All of it has to hold. Reading what a process group bound needs /proc,
    the gateway address has to be knowable, and it has to be an address this
    host can bind: Docker Desktop and rootless Docker report one that lives
    inside a VM or a user namespace, on which no relay can be raised. Where
    any of that is missing there is nothing to relay with, and an application
    must be asked for the wider bind instead -- so this is what decides
    whether asking is still necessary.
    """
    if not sys.platform.startswith("linux"):
        return False
    address = gateway()
    return address is not None and local(address)


def local(address: str) -> bool:
    """Is `address` one this host can bind, and so relay from?

    Docker reports the gateway from the daemon's side, which is only this
    host's side when the daemon is native to it. Binding is the one test
    that cannot be fooled by that.
    """
    try:
        family = (
            socket.AF_INET6
            if ipaddress.ip_address(address).version == 6
            else socket.AF_INET
        )
    except ValueError:
        return False
    with socket.socket(family) as probe:
        try:
            probe.bind((address, 0))
        except OSError:
            return False
    return True


def gateway() -> str | None:
    """The host address the hub's bridge container reaches, if it can be read.

    A daemon may override the alias with `host-gateway-ip`, which cannot be
    read back, so this is a best guess and binding it is the real test.
    None means the caller should not try: guessing would bind an arbitrary
    interface, where explaining the problem is the honest alternative.
    """
    try:
        result = subprocess.run(
            _GATEWAY_QUERY, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if result.returncode:
        return None
    # A bridge with IPv6 enabled reports a gateway per family. The hub's
    # route to the host is the IPv4 one.
    for line in result.stdout.splitlines():
        address = line.strip()
        try:
            if ipaddress.ip_address(address).version == 4:
                return address
        except ValueError:
            continue
    return None


async def _shut(writer: asyncio.StreamWriter) -> None:
    """Close a stream and wait for it, so its transport goes with the loop."""
    # RuntimeError covers a loop already closed under a handler being
    # collected during shutdown, where closing can no longer be scheduled.
    with contextlib.suppress(OSError, RuntimeError):
        writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), timeout=5)


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(_BUFFER):
            writer.write(data)
            await writer.drain()
    except OSError:
        # Either end going away is ordinary; the connection just ends.
        pass
    finally:
        # Carry the half-close through, so a peer waiting on end-of-stream
        # is not left hanging by the relay.
        with contextlib.suppress(OSError):
            writer.write_eof()


class _Relay:
    """Accepts on one address and mirrors each connection to `target`."""

    def __init__(self, target: Listener, bind: str, port: int) -> None:
        self._target = target
        self._bind = bind
        self._port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping: asyncio.Future | None = None
        self._open: set[asyncio.StreamWriter] = set()
        self._ready = threading.Event()
        self._error: BaseException | None = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            upstream_r, upstream_w = await asyncio.open_connection(
                self._target.host, self._target.port
            )
        except OSError:
            await _shut(writer)
            return
        self._open.update({writer, upstream_w})
        try:
            await asyncio.gather(
                _pump(reader, upstream_w),
                _pump(upstream_r, writer),
                return_exceptions=True,
            )
        finally:
            self._open.difference_update({writer, upstream_w})
            await _shut(writer)
            await _shut(upstream_w)

    async def _serve(self) -> None:
        try:
            server = await asyncio.start_server(
                self._handle,
                host=self._bind,
                port=self._port,
                family=socket.AF_INET,
            )
        except BaseException as exc:  # surfaced to start() below
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            await self._stopping
        finally:
            # Close in-flight connections while the loop is still running.
            # A relayed WebSocket never ends on its own, and a transport
            # collected after the loop has gone raises out of its destructor.
            server.close()
            await asyncio.gather(
                *(_shut(writer) for writer in list(self._open)),
                return_exceptions=True,
            )
            # Every handler has to finish before the loop goes, so each
            # accepted transport is closed by the handler's own cleanup. One
            # abandoned mid-flight is never closed, and raises out of its
            # destructor once the server it still points at has torn down.
            # Repeated, because cancelling one handler can let another be
            # accepted: a connection arriving mid-shutdown would otherwise
            # be abandoned still open.
            for _ in range(5):
                pending = [
                    task
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                ]
                if not pending:
                    break
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=5)

    def start(self) -> None:
        def run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._stopping = loop.create_future()
            try:
                loop.run_until_complete(self._serve())
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return

        def release() -> None:
            if self._stopping is not None and not self._stopping.done():
                self._stopping.set_result(None)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(release)
        thread.join(timeout=5)


@contextlib.contextmanager
def forwarding(target: Listener, *, bind: str, port: int) -> Iterator[None]:
    """Relay `bind`:`port` to `target` for the duration of the block.

    Raises OSError when the address cannot be bound, which is the only
    reliable test of whether the gateway address is usable here.
    """
    relay = _Relay(target, bind, port)
    relay.start()
    try:
        yield
    finally:
        relay.stop()
