"""Filesystem locations shared by the CLI and the session store."""

from __future__ import annotations

import os
from pathlib import Path

import click


def state_directory() -> Path:
    """Return the directory holding this proxy's local state.

    `LOCALGHOST_STATE_DIR` overrides everything; otherwise the XDG base
    directory specification applies. Both the retained public root and the
    session records live here, so every caller must agree on it.
    """
    configured = os.environ.get("LOCALGHOST_STATE_DIR")
    if configured:
        return Path(configured)
    state_home = os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    return Path(state_home) / "localghost"


def hub_http_port() -> int:
    """Return the hub's HTTP port on the host."""
    return _environment_port("LOCALGHOST_HTTP_PORT", 80)


def hub_https_port() -> int:
    """Return the hub's HTTPS port on the host."""
    return _environment_port("LOCALGHOST_HTTPS_PORT", 443)


def _environment_port(name: str, default: int) -> int:
    value = os.environ.get(name) or str(default)
    if not value.isascii() or not value.isdecimal():
        raise click.ClickException(f"{name} must be an integer from 1 to 65535")
    port = int(value)
    if not 1 <= port <= 65535:
        raise click.ClickException(f"{name} must be an integer from 1 to 65535")
    return port
