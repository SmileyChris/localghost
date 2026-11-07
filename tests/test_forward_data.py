import json

from localghost import registry, sessions


def test_record_forward_writes_a_directoryless_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record_forward("api", 5173, detached=False)
    payload = json.loads((tmp_path / "registry" / "api.json").read_text())
    assert payload["directory"] is None
    assert payload["type"] == "forward"
    assert payload["flags"]["port"] == 5173
    assert payload["forward_state"] is None
    assert registry.is_forward(registry.entries()[0])


def test_entries_reads_old_files_without_forward_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record("blog", tmp_path, "django")
    path = tmp_path / "registry" / "blog.json"
    payload = json.loads(path.read_text())
    del payload["forward_state"]
    path.write_text(json.dumps(payload))
    entry = registry.entries()[0]
    assert entry.forward_state is None
    assert not registry.is_forward(entry)


def test_set_forward_state_round_trips_on_a_forward(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record_forward("api", 5173, detached=True)
    registry.set_forward_state("api", "waiting")
    assert registry.entries()[0].forward_state == "waiting"
    registry.set_forward_state("api", None)
    assert registry.entries()[0].forward_state is None


def test_set_forward_state_only_touches_forwards(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record("blog", tmp_path, "django")
    path = tmp_path / "registry" / "blog.json"
    before = path.read_bytes()
    registry.set_forward_state("blog", "stopped")
    registry.set_forward_state("missing", "stopped")
    assert path.read_bytes() == before
    assert not (tmp_path / "registry" / "missing.json").exists()


def test_session_record_round_trips_cwd_none_and_route():
    created = sessions.create(
        mode="forward",
        name="api",
        port=5173,
        cwd=None,
        command=(),
        log=None,
        pid=123,
        route="",
    )
    sessions.set_route(
        created.id, "up", bridge_project="localghost-forward-api", bridge_yaml="x"
    )
    loaded = sessions.sessions()[0]
    assert loaded.cwd is None
    assert loaded.route == "up"
    assert loaded.bridge_project == "localghost-forward-api"
    assert loaded.bridge_yaml == "x"


def test_old_session_records_without_route_still_load():
    sessions.state_dir().mkdir(parents=True)
    record = sessions.Session("abc", "host", "blog", 8000, "/x", 1, "").as_dict()
    del record["route"]
    (sessions.state_dir() / "abc.json").write_text(json.dumps(record))
    assert sessions.sessions()[0].route == ""
