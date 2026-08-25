"""Shared pytest isolation fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_localghost_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep tests independent of the developer's real trust configuration."""
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path / "localghost-state"))


@pytest.fixture(autouse=True)
def neutral_colour_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Rich's terminal detection out of the ambient environment's hands.

    Rich decides `is_terminal` from `TTY_COMPATIBLE`, then `FORCE_COLOR`,
    and only then from `isatty()`. Click's `CliRunner` already swaps in a
    non-tty stream, so the assertions on plain output hold under any capture
    mode -- but either of those variables overrides that, and a developer or
    CI runner exporting one turns the plain-output tests red against
    perfectly good code.
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("TTY_COMPATIBLE", raising=False)
