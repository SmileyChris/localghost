"""A status bar pinned to the last terminal row during foreground runs.

The public URL is printed once before an application starts, which its own
output then scrolls away within seconds. This keeps the URL on screen for as
long as the application runs.

It works by setting a DECSTBM scrolling region over every row *except* the
last, so application output scrolls in the rows above while the bottom row is
never touched. The region's top margin stays at row 1 deliberately: terminals
save a line falling off the top to scrollback only when the top margin is the
first row, so pinning at the bottom keeps scrollback intact where pinning a
header at the top would silently destroy it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator

from .feedback import LIME, MINT

# Below this width there is no room for a bar worth drawing.
MINIMUM_WIDTH = 40
# The child writes straight to the terminal, so a full-screen redraw on its
# part can erase the bar with no notification. Repainting on a slow timer
# heals that without needing to see the child's output.
_REPAINT_SECONDS = 1.0

_HINT = "Ctrl+C to stop"
_SPINNER_FRAMES = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
# The spinner has to tick faster than the healing repaint to read as motion.
_SPINNER_SECONDS = 0.1
# How often the readiness probe retries while the application boots.
_PROBE_SECONDS = 0.25
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_SAVE_CURSOR = "\x1b7"
_RESTORE_CURSOR = "\x1b8"


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def _fg(hex_colour: str) -> str:
    return "\x1b[38;2;{};{};{}m".format(*_rgb(hex_colour))


def _terminal_size() -> tuple[int, int]:
    size = shutil.get_terminal_size()
    return size.columns, size.lines


def _bar_line(
    url: str,
    width: int,
    *,
    spinner: str | None = None,
    message: str = "starting",
) -> str:
    """Render the bar, budgeted to stay a cell short of the row's width.

    Writing the final column can trigger auto-wrap, and a wrap on the last
    row scrolls the screen -- the exact thing the pin exists to prevent.

    A `spinner` frame marks the application as still starting: the URL is
    dimmed until it actually answers, so the bar doubles as the readiness
    signal rather than inviting a click that would 502. `message` names the
    step being waited on, and is shown only while the spinner is.
    """
    budget = width - 1
    wordmark = " localghost  "
    status = f"{spinner} {message}  " if spinner else ""
    room = budget - len(wordmark) - len(status)
    hint = f"  {_HINT} " if room >= len(url) + len(_HINT) + 3 else ""
    shown = url[: max(0, room - len(hint))] if len(url) + len(hint) > room else url
    padding = " " * max(0, room - len(shown) - len(hint))
    rendered_status = (
        f"{_fg(MINT)}{spinner}{_RESET} {_DIM}{message}{_RESET}  " if spinner else ""
    )
    rendered_hint = f"{_DIM}{hint}{_RESET}" if hint else ""
    url_style = _DIM if spinner else _fg(MINT)
    return (
        f"{_BOLD} local{_fg(LIME)}ghost{_RESET}  "
        f"{rendered_status}"
        f"{url_style}{shown}{_RESET}"
        f"{padding}"
        f"{rendered_hint}"
    )


def _poll_until_ready(
    probe: Callable[[], bool], stop: threading.Event, interval: float = _PROBE_SECONDS
) -> bool:
    """Retry `probe` until it succeeds, or `stop` is set. True means ready."""
    while not stop.is_set():
        try:
            if probe():
                return True
        except OSError:
            # A refused connection is the expected answer while booting.
            pass
        if stop.wait(interval):
            return False
    return False


def tcp_probe(
    port: int, host: str = "127.0.0.1", timeout: float = 0.25
) -> Callable[[], bool]:
    """Readiness probe for a host application: is anything listening yet?"""

    def probe() -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    return probe


# Traefik answers with these while a route exists but nothing is behind it.
_GATEWAY_STATUSES = frozenset({502, 503, 504})


def http_probe(url: str, timeout: float = 1.0) -> Callable[[], bool]:
    """Readiness probe for a Compose application, which owns its own ports.

    Any answer other than a gateway error means something is serving. TLS is
    left unverified on purpose: this only asks whether the loopback route
    answers, never who is on the other end, and a run before `localghost
    trust` would otherwise fail verification and spin forever.
    """
    context = ssl._create_unverified_context()

    def probe() -> bool:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=context
            ) as response:
                return response.status not in _GATEWAY_STATUSES
        except urllib.error.HTTPError as error:
            return error.code not in _GATEWAY_STATUSES
        except (OSError, urllib.error.URLError, ValueError):
            return False

    return probe


class _Bar:
    """Owns the reserved row for the lifetime of a foreground run."""

    def __init__(
        self,
        url: str,
        stream,
        probe: Callable[[], bool] | None,
        message: str = "starting",
    ) -> None:
        self._url = url
        self._stream = stream
        self._probe = probe
        self._message = message
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._previous_winch: object = None
        self._painter: threading.Thread | None = None
        self._watcher: threading.Thread | None = None
        self._frame = 0
        # With no probe there is nothing to wait for, so the bar opens ready.
        self.ready_event = threading.Event()
        if probe is None:
            self.ready_event.set()

    def _emit(self, text: str) -> None:
        # One write per repaint: the painter thread shares this descriptor
        # with the child process, and a small single write to a terminal is
        # not interleaved in practice.
        with self._lock:
            self._stream.write(text)
            self._stream.flush()

    def draw(self) -> None:
        width, height = _terminal_size()
        spinner = (
            None
            if self.ready_event.is_set()
            else _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
        )
        self._emit(
            f"{_SAVE_CURSOR}\x1b[{height};1H\x1b[2K"
            f"{_bar_line(self._url, width, spinner=spinner, message=self._message)}"
            f"{_RESET}{_RESTORE_CURSOR}"
        )

    def ready(self) -> None:
        """Mark the application as answering and repaint without the spinner."""
        self.ready_event.set()
        self.draw()

    def status(self, message: str) -> None:
        """Name the step now being waited on, without ending the wait."""
        self._message = message
        self.draw()

    def resize(self) -> None:
        """Re-reserve the region after the window's height changed."""
        _, height = _terminal_size()
        self._emit(f"\x1b[1;{height - 1}r")
        self.draw()

    def start(self) -> None:
        _, height = _terminal_size()
        self._emit(f"\x1b[1;{height - 1}r")
        self.draw()
        with contextlib.suppress(ValueError):
            # Signal handlers can only be installed from the main thread.
            self._previous_winch = signal.signal(signal.SIGWINCH, self._on_winch)
        if _REPAINT_SECONDS:
            self._painter = threading.Thread(target=self._repaint, daemon=True)
            self._painter.start()
        if self._probe is not None:
            self._watcher = threading.Thread(target=self._watch, daemon=True)
            self._watcher.start()

    def stop(self) -> None:
        self._stop.set()
        if self._previous_winch is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGWINCH, self._previous_winch)
        _, height = _terminal_size()
        # Release the region first, then wipe the row it was protecting.
        self._emit(f"\x1b[r{_SAVE_CURSOR}\x1b[{height};1H\x1b[2K{_RESTORE_CURSOR}")

    def _on_winch(self, signum: int, frame: object) -> None:
        del signum, frame
        self.resize()

    def _watch(self) -> None:
        assert self._probe is not None
        if _poll_until_ready(self._probe, self._stop):
            self.ready()

    def _repaint(self) -> None:
        while True:
            # A spinning bar has to tick faster than a settled one, which only
            # repaints to heal over a child that cleared the screen.
            settled = self.ready_event.is_set()
            if self._stop.wait(_REPAINT_SECONDS if settled else _SPINNER_SECONDS):
                return
            if not settled:
                self._frame += 1
            self.draw()


class _Disabled:
    """The no-op the caller gets when the terminal cannot host a bar."""

    def __init__(self) -> None:
        self.ready_event = threading.Event()
        self.ready_event.set()

    def resize(self) -> None:
        pass

    def ready(self) -> None:
        pass

    def status(self, message: str) -> None:
        pass


def supported(stream, *, enabled: bool = True) -> bool:
    if not enabled or not getattr(stream, "isatty", lambda: False)():
        return False
    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    width, height = _terminal_size()
    return width >= MINIMUM_WIDTH and height >= 3


@contextlib.contextmanager
def pinned(
    url: str,
    *,
    stream=None,
    enabled: bool = True,
    probe: Callable[[], bool] | None = None,
    message: str = "starting",
) -> Iterator[object]:
    """Pin `url` to the last terminal row for the duration of the block.

    With a `probe`, the bar opens in a loading state and flips to ready once
    the probe first succeeds.
    """
    if stream is None:
        import sys

        stream = sys.stdout
    if not supported(stream, enabled=enabled):
        yield _Disabled()
        return
    bar = _Bar(url, stream, probe, message)
    bar.start()
    try:
        yield bar
    finally:
        # Leaving a region set would hand the user's shell a terminal that
        # only scrolls its top rows, so this must run on every exit path.
        bar.stop()
