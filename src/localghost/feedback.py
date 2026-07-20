"""Consistent terminal feedback with a plain-text fallback."""

from __future__ import annotations

import shlex
from collections.abc import Iterable

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

LIME = "#a3e635"
MINT = "#2dd4bf"


def title(*, welcome: bool = False) -> None:
    """Show the Localghost wordmark in interactive terminals."""
    if not _rich_terminal(False):
        return
    console = _console(False)
    console.print(Text.assemble(("local", "bold"), ("ghost", f"bold {LIME}")))
    console.print()
    if welcome:
        console.print("Easy .localhost URLs for your local apps.")


def next_actions() -> None:
    """Show the two useful commands after a successful proxy launch."""
    action("Stop the proxy", "uvx localghost down")
    action(
        "Add a route",
        "uvx localghost generate",
        " for Docker Compose, or uvx localghost run for a local app.",
    )


def action(label: str, command: str, detail: str = "", *, err: bool = False) -> None:
    """Show a runnable command with a consistent visual hierarchy."""
    console = _console(err)
    if _rich_terminal(err):
        console.print(
            Text.assemble(
                (f"{label}: ", "bold"), (command, LIME), (detail, "default")
            )
        )
        return
    console.print(f"{label}: {command}{detail}")


def details(
    rows: Iterable[tuple[str, str]], *, title: str | None = None, err: bool = False
) -> None:
    """Show concise labelled values in a table on a terminal or text elsewhere."""
    entries = list(rows)
    console = _console(err)
    if _rich_terminal(err):
        if title:
            console.print(Text(title, style="bold"))
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold")
        table.add_column(style=MINT)
        for label, value in entries:
            table.add_row(label, value)
        console.print(table)
        return
    lines = ([title] if title else []) + [
        f"{label}: {value}" for label, value in entries
    ]
    console.print("\n".join(lines), soft_wrap=True)


def choices(title: str, items: Iterable[tuple[str, str, bool]]) -> None:
    """Show interactive choices with the same terminal hierarchy as other output."""
    entries = list(items)
    console = _console(False)
    if _rich_terminal(False):
        console.print(Text(title, style="bold"))
        for name, description, is_likely in entries:
            marker = "suggested" if is_likely else ""
            console.print(
                Text.assemble(
                    ("• ", MINT),
                    (name, LIME if is_likely else "default"),
                    (f"  {description}", "default"),
                    (f"  {marker}" if marker else "", MINT),
                )
            )
        return
    lines = [title + ":"]
    for name, description, is_likely in entries:
        marker = " (likely)" if is_likely else ""
        lines.append(f"  {name}: {description}{marker}")
    console.print("\n".join(lines))


def info(message: str, *, err: bool = False) -> None:
    _message(message, MINT, err=err)


def success(message: str, *, err: bool = False) -> None:
    _message(message, LIME, err=err, symbol="✓")


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
            Panel(table, title=title, border_style=MINT)
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
        table = Table(
            title="Active routes",
            box=box.HORIZONTALS,
            pad_edge=False,
            show_header=True,
            show_lines=True,
            header_style=f"bold {LIME}",
        )
        table.add_column("Hostname")
        table.add_column("Location")
        for hostname, location in entries:
            table.add_row(hostname, location)
        console = _console(False)
        console.print()
        console.print(table)
        console.print()
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
