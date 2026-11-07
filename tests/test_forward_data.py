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


def _sleeper():
    # A thread reaps the child, as init would a real leader, so it is not a
    # zombie that still answers signal 0.
    import subprocess
    import threading

    child = subprocess.Popen(["sleep", "60"])
    threading.Thread(target=child.wait, daemon=True).start()
    return child


def _forward(pid, *, bridge=True):
    created = sessions.create(
        mode="forward",
        name="api",
        port=5173,
        cwd=None,
        command=(),
        log=None,
        pid=pid,
        route="",
    )
    if bridge:
        sessions.set_route(
            created.id, "up", bridge_project="localghost-forward-api", bridge_yaml="y"
        )
    return sessions.sessions()[0]


def test_stop_forward_signals_only_the_leader(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    killed_groups = []
    monkeypatch.setattr(sessions.os, "killpg", lambda *a: killed_groups.append(a))
    monkeypatch.setattr(sessions, "_stop_bridge", lambda session: None)
    leader, target = _sleeper(), _sleeper()
    try:
        sessions.stop(_forward(leader.pid))
        assert leader.poll() is not None
        assert target.poll() is None
        assert killed_groups == []
        assert sessions.sessions() == []
    finally:
        target.kill()
        leader.kill()


def test_stop_forward_marks_stopped_and_removes_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record_forward("api", 5173, detached=True)
    registry.set_forward_state("api", "waiting")
    downed = []
    monkeypatch.setattr(sessions, "_stop_bridge", downed.append)
    leader = _sleeper()
    try:
        sessions.stop(_forward(leader.pid))
    finally:
        leader.kill()
    assert [item.bridge_project for item in downed] == ["localghost-forward-api"]
    assert registry.entries()[0].forward_state == "stopped"


def test_reap_marks_a_killed_forward_with_bridge_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record_forward("api", 5173, detached=True)
    registry.set_forward_state("api", "waiting")
    downed = []
    monkeypatch.setattr(sessions, "_stop_bridge", downed.append)
    leader = _sleeper()
    _forward(leader.pid)
    leader.kill()
    leader.wait()
    assert sessions.reap() == 1
    assert len(downed) == 1
    assert registry.entries()[0].forward_state == "stopped"
    assert sessions.sessions()[0].bridge_project is None


def test_reap_marks_a_killed_forward_without_bridge_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    registry.record_forward("api", 5173, detached=True)
    registry.set_forward_state("api", "waiting")
    downed = []
    monkeypatch.setattr(sessions, "_stop_bridge", downed.append)
    leader = _sleeper()
    _forward(leader.pid, bridge=False)
    leader.kill()
    leader.wait()
    assert sessions.reap() == 0
    assert downed == []
    assert registry.entries()[0].forward_state == "stopped"
