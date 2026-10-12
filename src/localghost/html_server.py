"""Serve the current directory, updating open pages when a file changes.

Run as `python -I -m localghost.html_server PORT BIND`. HTML pages get a
script (`/__localghost/reload.js`, from html_server.js) that holds an event
stream open; while one is open the server checks file modification times and
sends the paths of any that change, and the page swaps changed stylesheets or
reloads. With no page open, nothing is checked. Dotfiles and dot-directories
are never served.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import threading
import time
import urllib.parse
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RELOAD_PATH = "/__localghost/reload"
# Served from a path rather than inline, so a `script-src 'self'` policy
# still lets it run.
RELOAD_SCRIPT_PATH = "/__localghost/reload.js"
RELOAD_SCRIPT = Path(__file__).with_name("html_server.js").read_bytes()
SCRIPT = f'<script src="{RELOAD_SCRIPT_PATH}"></script>'.encode()
_BODY_END = re.compile(rb"</body\s*>", re.IGNORECASE)
_INTERVAL = 0.5
# A comment line this often notices a closed tab, so its check stops.
_HEARTBEAT = 15.0
_SKIPPED_DIRECTORY = "node_modules"
# The stdlib server's own index names; its `index_pages` is 3.12+.
_INDEX_PAGES = ("index.html", "index.htm")
# Every open page shares one scan per interval rather than walking alone.
_scan_lock = threading.Lock()
_last_scan: tuple[Path, float, dict[str, int]] | None = None


def snapshot(root: Path) -> dict[str, int]:
    """Modification times of every file under root.

    Dot-directories and node_modules are skipped: neither is served content
    anyone edits by hand, and node_modules can hold a hundred thousand files.
    """
    times = {}
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [
            name
            for name in subdirectories
            if not name.startswith(".") and name != _SKIPPED_DIRECTORY
        ]
        for name in files:
            path = os.path.join(directory, name)
            # A file removed between the listing and the stat is just gone.
            with contextlib.suppress(OSError):
                times[path] = os.stat(path).st_mtime_ns
    return times


def shared_snapshot(root: Path) -> dict[str, int]:
    """A snapshot at most one interval old, so open pages share the walk.

    A reused snapshot can only predate a change, which errs toward one extra
    reload rather than a missed one.
    """
    global _last_scan
    with _scan_lock:
        now = time.monotonic()
        stale = _last_scan is None or now - _last_scan[1] >= _INTERVAL
        if stale or _last_scan[0] != root:
            _last_scan = (root, now, snapshot(root))
        return _last_scan[2]


def changed_paths(
    root: Path, before: dict[str, int], after: dict[str, int]
) -> list[str]:
    """URL paths of every file added, removed or modified between snapshots."""
    changed = {
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    }
    return sorted(
        urllib.parse.quote("/" + Path(path).relative_to(root).as_posix())
        for path in changed
    )


def inject(page: bytes) -> bytes:
    """Put the reload script before the last </body>, or at the end without one."""
    matches = list(_BODY_END.finditer(page))
    if not matches:
        return page + SCRIPT
    at = matches[-1].start()
    return page[:at] + SCRIPT + page[at:]


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        route = self.path.split("?", 1)[0]
        if route == RELOAD_PATH:
            self._stream_reloads()
            return
        if route == RELOAD_SCRIPT_PATH:
            self._send(RELOAD_SCRIPT, "text/javascript")
            return
        self._serve()

    def do_HEAD(self) -> None:
        self._serve()

    def _serve(self) -> None:
        """GET and HEAD alike, so both report the injected page's length."""
        if self._hidden():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        page = self._html_file()
        if page is None:
            if self.command == "HEAD":
                super().do_HEAD()
            else:
                super().do_GET()
            return
        try:
            body = inject(page.read_bytes())
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        # No charset: the page's own <meta charset> or BOM decides, as it
        # would from the stdlib server. The tag is ASCII, so it fits any.
        self._send(body, self.guess_type(str(page)))

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _hidden(self) -> bool:
        """Does the path name a dotfile or anything inside a dot-directory?"""
        path = urllib.parse.unquote(self.path.split("?", 1)[0].split("#", 1)[0])
        return any(part.startswith(".") for part in path.split("/"))

    def _html_file(self) -> Path | None:
        """The HTML file this request serves, leaving redirects and listings alone."""
        path = Path(self.translate_path(self.path))
        if path.is_dir():
            if not self.path.split("?", 1)[0].endswith("/"):
                return None
            path = next(
                (path / name for name in _INDEX_PAGES if (path / name).is_file()),
                path / "index.html",
            )
        if path.suffix.lower() in (".html", ".htm") and path.is_file():
            return path
        return None

    def _stream_reloads(self) -> None:
        # Before the headers, so a save the page races to see is still a change.
        root = Path(self.directory)
        before = shared_snapshot(root)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        quiet_since = time.monotonic()
        # The stream stays open: a page that swaps a stylesheet rather than
        # reloading would otherwise miss edits made while it reconnected.
        try:
            while True:
                time.sleep(_INTERVAL)
                after = shared_snapshot(root)
                if after != before:
                    paths = changed_paths(root, before, after)
                    event = "".join(f"data: {path}\n" for path in paths) + "\n"
                    before = after
                elif time.monotonic() - quiet_since > _HEARTBEAT:
                    event = ": still here\n\n"
                else:
                    continue
                self.wfile.write(event.encode())
                self.wfile.flush()
                quiet_since = time.monotonic()
        except OSError:  # The tab closed, or the connection otherwise went.
            return


def main() -> None:
    port, bind = int(sys.argv[1]), sys.argv[2]
    handler = partial(Handler, directory=os.getcwd())
    with (
        ThreadingHTTPServer((bind, port), handler) as server,
        contextlib.suppress(KeyboardInterrupt),
    ):
        server.serve_forever()


if __name__ == "__main__":
    main()
