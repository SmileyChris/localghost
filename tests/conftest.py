"""Shared pytest isolation fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_localghost_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep tests independent of the developer's real trust configuration."""
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path / "localghost-state"))
