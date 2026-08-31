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
    selected = [line for line in lines if "\x1b[7m" in line]
    assert len(selected) == 1
    assert "b" in selected[0]
