"""Repository-wide pytest fixtures.

This lives at the repository root rather than in tests/ so that any test
file collected from inside the checkout -- a scratch test at the root, a
worktree, an ad-hoc probe -- is isolated from the developer's real
localghost state. Tests in tests/ once leaked ghost-page entries into
~/.local/state/localghost when run from outside that directory.
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_localghost_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep tests independent of the developer's real trust and registry state."""
    monkeypatch.setenv("LOCALGHOST_STATE_DIR", str(tmp_path / "localghost-state"))
