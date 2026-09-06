"""Shared pytest isolation fixtures."""

from pathlib import Path

import pytest

from localghost import feedback


@pytest.fixture(autouse=True)
def unshown_wordmark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a fresh copy of `title`'s once-per-process guard.

    `feedback.title` prints the wordmark at most once per process so that
    `save --run`, which re-enters `run`, does not print it twice. A test
    session is one process running many CLI invocations, so the guard has
    to be reset between them or the first test to reach a terminal-shaped
    `title` would silence every later one.
    """
    monkeypatch.setattr(feedback, "_wordmark_shown", False)


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
