"""Filesystem locations shared by the CLI and the session store."""

from __future__ import annotations

import os
from pathlib import Path


def state_directory() -> Path:
    """Return the directory holding this proxy's local state.

    `LOCALGHOST_STATE_DIR` overrides everything; otherwise the XDG base
    directory specification applies. Both the retained public root and the
    detached session records live here, so every caller must agree on it.
    """
    configured = os.environ.get("LOCALGHOST_STATE_DIR")
    if configured:
        return Path(configured)
    state_home = os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    return Path(state_home) / "localghost"
