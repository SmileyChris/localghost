"""Session record changes from two writers must not undo each other."""

import threading
import time

import pytest

from localghost import sessions


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path))
    return tmp_path


def _forward() -> sessions.Session:
    return sessions.create(
        mode="forward",
        name="api",
        port=5173,
        cwd=None,
        command=(),
        log=None,
        pid=None,
    )


@pytest.mark.parametrize("pid_first", [True, False])
def test_pid_and_route_survive_either_order(pid_first) -> None:
    session = _forward()

    def pid() -> None:
        sessions.update(session.id, pid=123)

    def route() -> None:
        sessions.set_route(session.id, "up", bridge_project="p", bridge_yaml="y")

    for step in (pid, route) if pid_first else (route, pid):
        step()

    saved = sessions.read(session.id)
    assert saved.pid == 123
    assert saved.route == "up"
    assert saved.bridge_project == "p"
    assert saved.bridge_yaml == "y"


def test_a_stale_copy_does_not_overwrite_a_newer_field() -> None:
    session = _forward()
    stale = sessions.read(session.id)  # read before the leader's write

    sessions.set_route(session.id, "up", bridge_project="p", bridge_yaml="y")
    sessions.update(stale.id, pid=123)

    saved = sessions.read(session.id)
    assert (saved.pid, saved.bridge_project) == (123, "p")


def test_update_of_a_removed_record_does_nothing(state) -> None:
    session = _forward()
    sessions.discard(session)

    assert sessions.update(session.id, pid=123) is None
    assert sessions.read(session.id) is None
    assert not (state / "sessions" / f"{session.id}.json").exists()


def test_update_refuses_unknown_fields() -> None:
    session = _forward()

    with pytest.raises(TypeError):
        sessions.update(session.id, colour="red")


def test_removing_a_record_removes_its_lock(state) -> None:
    session = _forward()
    sessions.update(session.id, pid=123)
    assert (state / "sessions" / f"{session.id}.lock").exists()

    sessions.discard(session)

    assert not (state / "sessions" / f"{session.id}.lock").exists()


def test_concurrent_updates_of_different_fields_lose_nothing(monkeypatch) -> None:
    session = _forward()
    original = sessions.save

    def slow_save(record) -> None:
        # Widen the gap between read and write, where a lost update happens.
        time.sleep(0.0005)
        original(record)

    monkeypatch.setattr(sessions, "save", slow_save)
    rounds = 150
    errors: list = []

    def writer(change) -> None:
        try:
            for value in range(1, rounds + 1):
                change(value)
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(
            target=writer, args=(lambda v: sessions.update(session.id, pid=v),)
        ),
        threading.Thread(
            target=writer,
            args=(lambda v: sessions.update(session.id, port=10000 + v),),
        ),
        threading.Thread(
            target=writer,
            args=(lambda v: sessions.set_route(
                session.id, f"up {v}", bridge_project=f"p{v}", bridge_yaml=""
            ),),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    saved = sessions.read(session.id)
    assert saved.pid == rounds
    assert saved.port == 10000 + rounds
    assert saved.route == f"up {rounds}"
    assert saved.bridge_project == f"p{rounds}"
    assert list((sessions.state_dir()).glob("*.tmp")) == []
