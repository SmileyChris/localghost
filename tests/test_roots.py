import click
import pytest

from localghost import roots


def test_config_is_discovered_from_a_subdirectory(tmp_path, monkeypatch):
    # $HOME sits beside the project, not above it: `_search_path` excludes
    # $HOME unconditionally, so a config living at $HOME would never be
    # discovered (see test_config_discovery_never_reaches_home below).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".localghost.toml").write_text("[run]\n")
    nested = project / "src" / "app"
    nested.mkdir(parents=True)

    assert roots.discover_config(nested) == project / ".localghost.toml"


def test_config_discovery_never_reaches_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    nested = home / "project"
    nested.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (home / ".localghost.toml").write_text("[run]\n")

    assert roots.discover_config(nested) is None


def test_the_flag_outranks_every_other_source(tmp_path):
    flagged = tmp_path / "flagged"
    flagged.mkdir()
    configured_dir = tmp_path / "configured"
    configured_dir.mkdir()

    resolved = roots.resolve_root(
        start=tmp_path,
        flag=flagged,
        configured="configured",
        config_dir=tmp_path,
    )

    assert resolved == flagged


def test_configured_root_resolves_against_the_config_directory(tmp_path):
    config_dir = tmp_path / "tools"
    config_dir.mkdir()

    resolved = roots.resolve_root(
        start=config_dir, flag=None, configured="..", config_dir=config_dir
    )

    assert resolved == tmp_path


def test_an_absolute_configured_root_is_taken_as_given(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()

    resolved = roots.resolve_root(
        start=tmp_path, flag=None, configured=str(target), config_dir=tmp_path
    )

    assert resolved == target


def test_the_config_directory_anchors_the_root(tmp_path):
    config_dir = tmp_path / "app"
    config_dir.mkdir()

    resolved = roots.resolve_root(
        start=config_dir, flag=None, configured=None, config_dir=config_dir
    )

    assert resolved == config_dir


def test_nothing_pins_the_root_without_a_flag_or_config(tmp_path):
    assert (
        roots.resolve_root(start=tmp_path, flag=None, configured=None, config_dir=None)
        is None
    )


def test_a_missing_root_names_the_value_and_its_base(tmp_path):
    with pytest.raises(click.ClickException) as error:
        roots.resolve_root(
            start=tmp_path, flag=None, configured="absent", config_dir=tmp_path
        )

    assert "absent" in str(error.value)
    assert str(tmp_path) in str(error.value)


def test_a_file_is_not_a_valid_root(tmp_path):
    target = tmp_path / "file.txt"
    target.touch()

    with pytest.raises(click.ClickException, match="not a directory"):
        roots.resolve_root(
            start=tmp_path, flag=None, configured=str(target), config_dir=tmp_path
        )
