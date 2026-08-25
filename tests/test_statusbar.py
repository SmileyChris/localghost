"""Behaviour of the pinned status bar drawn during foreground runs."""

import re
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from localghost import statusbar

URL = "http://demo.localhost"

# CSI sequences the bar relies on, matched loosely so styling can change.
SET_REGION = re.compile(r"\x1b\[(\d+);(\d+)r")
RESET_REGION = "\x1b[r"


class Stream:
    """A stand-in tty that records everything written to it."""

    def __init__(self, *, tty: bool = True):
        self.chunks: list[str] = []
        self._tty = tty

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self._tty

    @property
    def text(self) -> str:
        return "".join(self.chunks)


@pytest.fixture(autouse=True)
def terminal(monkeypatch):
    """Give every test a predictable 80x24 terminal with a usable TERM."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(statusbar, "_terminal_size", lambda: (80, 24))
    # The repaint thread is timing-dependent; tests drive redraws directly.
    monkeypatch.setattr(statusbar, "_REPAINT_SECONDS", 0)


def visible(text: str) -> str:
    """Strip CSI/charset escapes, leaving the cells a user would see."""
    without_csi = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return re.sub(r"\x1b[78]", "", without_csi)


def test_reserves_the_last_row_and_releases_it_on_exit():
    stream = Stream()

    with statusbar.pinned(URL, stream=stream):
        during = stream.text

    match = SET_REGION.search(during)
    assert match, "expected DECSTBM to reserve a scrolling region"
    # Row 24 is the bar; the region must stop one row short of it.
    assert match.group(2) == "23"
    assert RESET_REGION in stream.text


def test_region_top_margin_is_row_one_so_scrollback_survives():
    stream = Stream()

    with statusbar.pinned(URL, stream=stream):
        pass

    # A top margin below row 1 makes terminals discard scrolled-off lines
    # instead of saving them to scrollback -- the whole reason the bar sits
    # at the bottom rather than the top.
    match = SET_REGION.search(stream.text)
    assert match and match.group(1) == "1"


def test_releases_the_region_when_the_body_raises():
    stream = Stream()

    with pytest.raises(RuntimeError), statusbar.pinned(URL, stream=stream):
        raise RuntimeError("child exploded")

    assert RESET_REGION in stream.text


def test_bar_shows_the_url():
    stream = Stream()

    with statusbar.pinned(URL, stream=stream):
        pass

    assert URL in visible(stream.text)


def test_bar_never_writes_the_final_column():
    drawn = statusbar._bar_line(URL, 80)

    # Filling column 80 can trigger auto-wrap, which on the last row scrolls
    # the screen -- exactly what the pin exists to prevent.
    assert len(visible(drawn)) <= 79


def test_long_url_is_truncated_to_fit():
    long_url = "http://" + "a" * 200 + ".localhost"

    drawn = statusbar._bar_line(long_url, 80)

    assert len(visible(drawn)) <= 79


def test_does_nothing_when_the_stream_is_not_a_terminal():
    stream = Stream(tty=False)

    with statusbar.pinned(URL, stream=stream):
        pass

    assert stream.text == ""


def test_does_nothing_on_a_dumb_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    stream = Stream()

    with statusbar.pinned(URL, stream=stream):
        pass

    assert stream.text == ""


def test_does_nothing_when_disabled():
    stream = Stream()

    with statusbar.pinned(URL, stream=stream, enabled=False):
        pass

    assert stream.text == ""


def test_does_nothing_when_the_window_is_too_narrow(monkeypatch):
    monkeypatch.setattr(statusbar, "_terminal_size", lambda: (20, 24))
    stream = Stream()

    with statusbar.pinned(URL, stream=stream):
        pass

    assert stream.text == ""


def test_resize_reissues_the_region_for_the_new_height(monkeypatch):
    stream = Stream()
    size = [80, 24]
    monkeypatch.setattr(statusbar, "_terminal_size", lambda: tuple(size))

    with statusbar.pinned(URL, stream=stream) as bar:
        size[1] = 40
        bar.resize()
        after = stream.text

    assert "\x1b[1;39r" in after, "resize must recompute the bottom margin"


def test_restores_the_previous_winch_handler():
    original = signal.getsignal(signal.SIGWINCH)
    stream = Stream()

    with statusbar.pinned(URL, stream=stream):
        assert signal.getsignal(signal.SIGWINCH) is not original

    assert signal.getsignal(signal.SIGWINCH) is original


def test_bar_shows_a_starting_state_before_the_app_is_up():
    drawn = visible(statusbar._bar_line(URL, 80, spinner="⠋"))

    assert "⠋" in drawn
    assert "starting" in drawn


def test_ready_bar_drops_the_starting_state():
    drawn = visible(statusbar._bar_line(URL, 80))

    assert "starting" not in drawn
    assert URL in drawn


def test_starting_state_still_respects_the_width_budget():
    long_url = "http://" + "a" * 200 + ".localhost"

    drawn = statusbar._bar_line(long_url, 80, spinner="⠋")

    assert len(visible(drawn)) <= 79


def test_poll_until_ready_returns_true_once_the_probe_succeeds():
    attempts = []

    def probe():
        attempts.append(1)
        return len(attempts) >= 3

    stop = threading.Event()

    assert statusbar._poll_until_ready(probe, stop, interval=0) is True
    assert len(attempts) == 3


def test_poll_until_ready_gives_up_when_stopped():
    stop = threading.Event()
    stop.set()

    assert statusbar._poll_until_ready(lambda: False, stop, interval=0) is False


def test_tcp_probe_detects_a_listening_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert statusbar.tcp_probe(port)() is True
    finally:
        listener.close()

    # Once the listener is gone the same probe must report the app as down.
    assert statusbar.tcp_probe(port)() is False


def test_bar_starts_in_the_loading_state_when_a_probe_is_given():
    stream = Stream()

    with statusbar.pinned(URL, stream=stream, probe=lambda: False):
        drawn = stream.text

    assert "starting" in visible(drawn)


def test_bar_without_a_probe_is_ready_immediately():
    stream = Stream()

    with statusbar.pinned(URL, stream=stream):
        drawn = stream.text

    assert "starting" not in visible(drawn)


def test_probe_success_flips_the_bar_to_ready():
    stream = Stream()

    with statusbar.pinned(URL, stream=stream, probe=lambda: True) as bar:
        assert bar.ready_event.wait(2), "probe never marked the bar ready"
        latest = stream.chunks[-1]

    assert "starting" not in visible(latest)
    assert URL in visible(latest)


def test_a_probe_that_never_succeeds_leaves_the_bar_loading():
    stream = Stream()

    with statusbar.pinned(URL, stream=stream, probe=lambda: False) as bar:
        assert bar.ready_event.wait(0.2) is False

    assert "starting" in visible(stream.text)


class _Server:
    """A real local HTTP server answering with one fixed status."""

    def __init__(self, status: int):
        handler_status = status

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(handler_status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_http_probe_treats_a_served_response_as_ready():
    server = _Server(200)
    try:
        assert statusbar.http_probe(server.url)() is True
    finally:
        server.close()


def test_http_probe_treats_a_bad_gateway_as_not_ready():
    # Traefik answers 502 while the route exists but the backend is still
    # booting -- the exact window the loading state covers.
    server = _Server(502)
    try:
        assert statusbar.http_probe(server.url)() is False
    finally:
        server.close()


def test_http_probe_treats_a_client_error_as_ready():
    # A 404 still proves the application is answering; only the gateway
    # statuses mean nothing is behind the route yet.
    server = _Server(404)
    try:
        assert statusbar.http_probe(server.url)() is True
    finally:
        server.close()


def test_http_probe_treats_a_refused_connection_as_not_ready():
    server = _Server(200)
    url = server.url
    server.close()

    assert statusbar.http_probe(url)() is False


def test_bar_renders_the_step_it_is_waiting_on():
    drawn = visible(statusbar._bar_line(URL, 80, spinner="⠋", message="starting hub"))

    assert "starting hub" in drawn


def test_ready_bar_drops_the_step_message():
    drawn = visible(statusbar._bar_line(URL, 80, message="starting hub"))

    assert "starting hub" not in drawn


def test_a_long_step_message_still_respects_the_width_budget():
    drawn = statusbar._bar_line(
        URL, 80, spinner="⠋", message="waiting for something with a very long name"
    )

    assert len(visible(drawn)) <= 79


def test_status_updates_the_step_without_ending_the_loading_state():
    stream = Stream()

    with statusbar.pinned(
        URL, stream=stream, probe=lambda: False, message="starting hub"
    ) as bar:
        bar.status("starting app")
        latest = stream.chunks[-1]

    assert "starting app" in visible(latest)
    assert not bar.ready_event.is_set()


def test_disabled_bar_accepts_status_updates():
    stream = Stream(tty=False)

    with statusbar.pinned(URL, stream=stream) as bar:
        bar.status("starting app")

    assert stream.text == ""
