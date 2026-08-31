from localghost import picker
from localghost.registry import RegistryEntry


def entry(name: str) -> RegistryEntry:
    return RegistryEntry(
        hostname=f"{name}.localhost",
        name=name,
        directory=f"/tmp/{name}",
        type="django",
        last_started="2026-08-30T12:00:00+00:00",
    )


def test_decode_key_variants():
    assert picker.decode_key(b"\x1b[A") == "up"
    assert picker.decode_key(b"\x1b[B") == "down"
    assert picker.decode_key(b"k") == "up"
    assert picker.decode_key(b"j") == "down"
    assert picker.decode_key(b"\r") == "enter"
    assert picker.decode_key(b"\n") == "enter"
    assert picker.decode_key(b"\x1b[3~") == "delete"
    assert picker.decode_key(b"q") == "quit"
    assert picker.decode_key(b"\x1b") == "quit"
    assert picker.decode_key(b"\x03") == "interrupt"
    assert picker.decode_key(b"x") == "other"


def test_move_clamps_selection():
    model = picker.PickerModel([entry("a"), entry("b"), entry("c")])
    model.apply("up")
    assert model.selected == 0
    model.apply("down")
    model.apply("down")
    model.apply("down")
    assert model.selected == 2


def test_enter_returns_summon_of_current():
    model = picker.PickerModel([entry("a"), entry("b")])
    model.apply("down")
    outcome = model.apply("enter")
    assert isinstance(outcome, picker.Summon)
    assert outcome.entry.name == "b"


def test_delete_removes_current_and_clamps():
    model = picker.PickerModel([entry("a"), entry("b")])
    model.apply("down")
    outcome = model.apply("delete")
    assert isinstance(outcome, picker.Forgot)
    assert outcome.entry.name == "b"
    assert [e.name for e in model.entries] == ["a"]
    assert model.selected == 0


def test_delete_last_entry_leaves_empty_model():
    model = picker.PickerModel([entry("a")])
    outcome = model.apply("delete")
    assert isinstance(outcome, picker.Forgot)
    assert model.entries == []
    assert model.apply("enter") is None
    assert model.apply("delete") is None


def test_quit_and_noise_keys():
    model = picker.PickerModel([entry("a")])
    assert model.apply("quit") is picker.QUIT
    assert model.apply("other") is None


def test_render_lines_marks_selection():
    model = picker.PickerModel([entry("a"), entry("b")])
    model.apply("down")
    lines = picker.render_lines(model)
    assert any("a.localhost" in line for line in lines)
    selected = [line for line in lines if "\x1b[38;2;163;230;53m" in line]
    assert len(selected) == 1
    assert "b" in selected[0]


def test_decode_undo_and_backspace():
    assert picker.decode_key(b"u") == "undo"
    assert picker.decode_key(b"\x7f") == "delete"
    assert picker.decode_key(b"\x08") == "delete"


def test_undo_restores_forgotten_entry_at_its_place():
    model = picker.PickerModel([entry("a"), entry("b"), entry("c")])
    model.apply("down")
    model.apply("delete")
    assert [e.name for e in model.entries] == ["a", "c"]
    outcome = model.apply("undo")
    assert isinstance(outcome, picker.Restored)
    assert outcome.entry.name == "b"
    assert [e.name for e in model.entries] == ["a", "b", "c"]
    assert model.selected == 1
    assert "Remembered b" in model.notice


def test_undo_without_forget_is_noop():
    model = picker.PickerModel([entry("a")])
    assert model.apply("undo") is None


def test_undo_only_restores_once():
    model = picker.PickerModel([entry("a")])
    model.apply("delete")
    assert isinstance(model.apply("undo"), picker.Restored)
    assert model.apply("undo") is None
    assert len(model.entries) == 1


def test_forget_notice_teaches_undo():
    model = picker.PickerModel([entry("a")])
    model.apply("delete")
    assert "(u to undo)" in model.notice


def test_render_aligns_columns():
    import re

    model = picker.PickerModel([entry("a"), entry("longername")])
    lines = [
        re.sub(r"\x1b\[[0-9;]*m", "", line)
        for line in picker.render_lines(model, width=120)
    ]
    assert lines[1].index("django") == lines[2].index("django")


def test_render_truncates_to_width():
    model = picker.PickerModel([entry("averyveryverylongprojectname")])
    lines = picker.render_lines(model, width=40)
    for line in lines[1:]:
        stripped = line
        for code in ("\x1b[7m", "\x1b[0m", "\x1b[1m", "\x1b[2m"):
            stripped = stripped.replace(code, "")
        while "\x1b[38;2;" in stripped:
            start = stripped.index("\x1b[38;2;")
            end = stripped.index("m", start) + 1
            stripped = stripped[:start] + stripped[end:]
        assert len(stripped) <= 40
    assert any("…" in line for line in lines)


def test_render_abbreviates_home_directory(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp")
    model = picker.PickerModel([entry("a")])
    lines = picker.render_lines(model, width=120)
    assert "~/a" in lines[1]
    assert "/tmp/a" not in lines[1]


def test_render_empty_list_explains_itself():
    model = picker.PickerModel([entry("a")])
    model.apply("delete")
    lines = picker.render_lines(model, width=120)
    assert any("Nothing remembered" in line for line in lines)
