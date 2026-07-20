"""Consistent terminal feedback with a plain-text fallback."""

from __future__ import annotations

import shlex
from collections.abc import Iterable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def info(message: str, *, err: bool = False) -> None:
    _message(message, "cyan", err=err)


def success(message: str, *, err: bool = False) -> None:
    _message(message, "green", err=err, symbol="✓")


def warning(title: str, messages: Iterable[str]) -> None:
    items = list(messages)
    if _rich_terminal(True):
        body = Text("\n".join(f"• {message}" for message in items))
        _console(True).print(Panel(body, title=title, border_style="yellow"))
        return
    for message in items:
        _console(True).print(f"Warning: {message}")


def run_plan(
    *,
    framework: str,
    command: tuple[str, ...],
    port: int,
    url: str,
    dry_run: bool,
) -> None:
    rows = (
        ("Framework", framework),
        ("Command", shlex.join(command)),
        ("Host port", str(port)),
        ("Public URL", url),
    )
    destination_is_error = dry_run
    if _rich_terminal(destination_is_error):
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold")
        table.add_column()
        for label, value in rows:
            table.add_row(label, value)
        title = "Dry run" if dry_run else "Run configuration"
        _console(destination_is_error).print(
            Panel(table, title=title, border_style="cyan")
        )
        return
    heading = "Dry run:" if dry_run else "Run configuration:"
    lines = [heading, *(f"  {label}: {value}" for label, value in rows)]
    _console(destination_is_error).print("\n".join(lines))


def routes(items: Iterable[tuple[str, str]]) -> None:
    entries = list(items)
    if not entries:
        info("No application routes are active yet.")
        return
    if _rich_terminal(False):
        table = Table(title="Active routes", show_header=True, header_style="bold cyan")
        table.add_column("Hostname")
        table.add_column("Location")
        for hostname, location in entries:
            table.add_row(hostname, location)
        _console(False).print(table)
        return
    lines = ["Active routes:", *(f"  {host}: {location}" for host, location in entries)]
    _console(False).print("\n".join(lines))


def _message(message: str, color: str, *, err: bool, symbol: str = "•") -> None:
    console = _console(err)
    if _rich_terminal(err):
        console.print(Text.assemble((f"{symbol} ", color), message))
    else:
        console.print(message)


def _console(err: bool) -> Console:
    return Console(stderr=err, highlight=False)


def _rich_terminal(err: bool) -> bool:
    return _console(err).is_terminal
