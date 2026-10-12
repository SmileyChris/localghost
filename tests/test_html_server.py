import http.client
import threading
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

from localghost import html_server


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(html_server, "_INTERVAL", 0.05)
    (tmp_path / "index.html").write_text("<html><body>root</body></html>")
    (tmp_path / "inner").mkdir()
    (tmp_path / "inner" / "index.html").write_text("inner")
    (tmp_path / "style.css").write_text("body {}")
    handler = partial(html_server.Handler, directory=str(tmp_path))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as running:
        threading.Thread(target=running.serve_forever, daemon=True).start()
        yield tmp_path, running.server_address[1]
        running.shutdown()


def get(port, path, method="GET"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path)
    response = connection.getresponse()
    return response, response.read()


def test_inject_goes_before_the_last_body_end_or_at_the_end():
    script = html_server.SCRIPT
    assert html_server.inject(b"<p>a</p></BODY>") == b"<p>a</p>" + script + b"</BODY>"
    assert html_server.inject(b"<p>a</p>") == b"<p>a</p>" + script


def test_html_pages_get_the_reload_script_and_other_files_do_not(server):
    _, port = server

    response, body = get(port, "/")
    assert body == b"<html><body>root" + html_server.SCRIPT + b"</body></html>"
    assert response.getheader("Content-Length") == str(len(body))
    assert response.getheader("Content-Type") == "text/html"
    assert get(port, "/inner/")[1] == b"inner" + html_server.SCRIPT
    assert get(port, "/style.css")[1] == b"body {}"


def test_a_directory_without_its_slash_still_redirects(server):
    _, port = server

    response, _ = get(port, "/inner")
    assert response.status == 301
    assert response.getheader("Location") == "/inner/"


def read_event(response):
    lines = []
    while (line := response.readline()) != b"\n":
        lines.append(line)
    return lines


def test_one_stream_reports_each_change_by_path(server):
    root, port = server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", html_server.RELOAD_PATH)
    response = connection.getresponse()
    assert response.getheader("Content-Type") == "text/event-stream"

    (root / "inner" / "new.html").write_text("new")
    assert read_event(response) == [b"data: /inner/new.html\n"]

    (root / "style.css").write_text("body { color: red }")
    assert read_event(response) == [b"data: /style.css\n"]


def test_changed_paths_are_url_paths_of_added_removed_and_modified_files(tmp_path):
    def at(name):
        return str(tmp_path / name)

    before = {at("kept.css"): 1, at("gone.html"): 1, at("edited.css"): 1}
    after = {at("kept.css"): 1, at("edited.css"): 2, at("new page.html"): 1}

    assert html_server.changed_paths(tmp_path, before, after) == [
        "/edited.css",
        "/gone.html",
        "/new%20page.html",
    ]


def test_head_reports_the_injected_length(server):
    _, port = server

    head, _ = get(port, "/", "HEAD")
    assert head.getheader("Content-Length") == str(len(get(port, "/")[1]))


def test_an_index_htm_gets_the_script_too(server):
    root, port = server
    (root / "old").mkdir()
    (root / "old" / "index.htm").write_text("old")

    assert get(port, "/old/")[1] == b"old" + html_server.SCRIPT


def test_the_reload_script_is_served_from_its_own_path(server):
    _, port = server

    response, body = get(port, html_server.RELOAD_SCRIPT_PATH)
    assert response.getheader("Content-Type") == "text/javascript"
    assert body == html_server.RELOAD_SCRIPT


@pytest.mark.parametrize("path", ["/.env", "/.git/config", "/%2eenv", "/.git/"])
def test_dotfiles_and_dot_directories_are_not_served(server, path):
    root, port = server
    (root / ".env").write_text("SECRET=1")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]")

    assert get(port, path)[0].status == 404


def test_snapshot_skips_dot_directories_and_node_modules(tmp_path):
    for directory in (".git", "node_modules", "pages"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "file").touch()

    assert list(html_server.snapshot(tmp_path)) == [str(tmp_path / "pages" / "file")]


def test_open_pages_share_one_scan_per_interval(tmp_path, monkeypatch):
    walks = []
    monkeypatch.setattr(html_server, "_last_scan", None)
    monkeypatch.setattr(html_server, "snapshot", lambda root: walks.append(root) or {})

    html_server.shared_snapshot(tmp_path)
    html_server.shared_snapshot(tmp_path)

    assert walks == [tmp_path]
