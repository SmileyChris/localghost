"""Process-group leader for a detached host run.

A foreground run relays a loopback-bound application from the Docker gateway
for as long as it lasts. A detached run leaves no localghost process behind to
do that, so the hub could never reach such an application. This stays behind
in the application's process group instead: it starts the command, raises the
same relay once the application binds loopback, and exits with the command's
status. `sessions stop` signals the whole group, which reaches the application
directly.

Usage: python -m localghost.detached PORT {shared,isolated} COMMAND...
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys

from . import forwarder, ports

_POLL = 0.5


def _target(port: int, isolated: bool) -> ports.Listener | None:
    if isolated:
        # pasta always takes host loopback, and hides its sockets from /proc.
        return ports.Listener("127.0.0.1", port)
    return ports.loopback_only(os.getpgrp(), port)


def main(argv: list[str]) -> int:
    port, isolated, command = int(argv[0]), argv[1] == "isolated", argv[2:]
    child = subprocess.Popen(command)
    # The group signal is meant for the application; this only outlives it
    # long enough to report its status.
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda *_: None)
    # Asked once: it consults Docker, and the answer does not change.
    settled = forwarder.hub_reaches_loopback()
    with contextlib.ExitStack() as relays:
        while True:
            try:
                code = child.wait(timeout=_POLL)
                return 128 - code if code < 0 else code
            except subprocess.TimeoutExpired:
                pass
            if settled:
                continue
            target = _target(port, isolated)
            if target is None:
                continue
            settled = True
            address = forwarder.gateway()
            try:
                if address is None:
                    raise OSError("no Docker gateway address")
                relays.enter_context(
                    forwarder.forwarding(target, bind=address, port=port)
                )
            except OSError as exc:
                print(f"localghost: cannot relay to {port}: {exc}", flush=True)
                continue
            print(
                f"localghost: relaying {address}:{port} so the hub can reach it.",
                flush=True,
            )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
