"""Consistent terminal feedback with a plain-text fallback."""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path

from rich.console import Console
from rich.text import Text

LIME = "#a3e635"
MINT = "#2dd4bf"


def _command() -> str:
    """How to invoke localghost again, matching how it was run this time.

    uvx runs from a throwaway environment inside uv's cache (and sets UV), so
    plain `localghost` may not be on PATH; an installed tool or venv is. The
    cache root, wherever UV_CACHE_DIR puts it, carries the standard
    CACHEDIR.TAG; the environment's own tag doesn't count, since `uv tool
    install` environments have one too.
    """
    if os.environ.get("UV") and any(
        (parent / "CACHEDIR.TAG").is_file() for parent in Path(sys.prefix).parents
    ):
        return "uvx localghost"
    return "localghost"


COMMAND = _command()


_wordmark_shown = False


def title(*, welcome: bool = False) -> None:
    """Show the Localghost wordmark in interactive terminals, once.

    The wordmark is a per-invocation brand mark, not a per-command one, and
    commands compose: `save --run` re-enters `run`, which prints a title
    of its own. Guarding here, rather than threading a suppression flag
    through every branch of `save`, keeps the mark at the top of the
    output and closes every future double print for free.

    Commands that report to a person or change something show it; commands
    whose output is the content itself (`sessions`, `hub logs`, `--json`)
    don't. Block titles below it name a section of the screen, never the
    command that was typed.
    """
    global _wordmark_shown
    if _wordmark_shown or not _rich_terminal(False):
        return
    _wordmark_shown = True
    console = _console(False)
    console.print(_wordmark())
    console.print()
    if welcome:
        console.print("Easy .localhost URLs for your local apps.")


def sign_off() -> None:
    """Mark where localghost speaks again after an application's own output."""
    if not _rich_terminal(False):
        return
    console = _console(False)
    console.print()
    console.print(_wordmark())


def _wordmark() -> Text:
    return Text.assemble(("local", "bold"), ("ghost", f"bold {LIME}"))


def next_actions(*, https_enabled: bool) -> None:
    """Show useful commands after a successful hub launch."""
    action("Stop the hub", f"{COMMAND} hub down")
    action(
        "Save a setup",
        f"{COMMAND} save",
        (
            (", or ", "default"),
            (f"{COMMAND} run", LIME),
            (" to run a local app.", "default"),
        ),
    )
    if not https_enabled:
        action(
            "Enable HTTPS",
            f"{COMMAND} trust install",
            " after installing mkcert.",
        )


def action(
    label: str,
    command: str,
    detail: str | Iterable[tuple[str, str]] = "",
    *,
    err: bool = False,
) -> None:
    """Show a runnable command with a consistent visual hierarchy."""
    console = _console(err)
    detail_parts = [(detail, "default")] if isinstance(detail, str) else list(detail)
    if _rich_terminal(err):
        console.print(
            Text.assemble((f"{label}: ", "bold"), (command, LIME), *detail_parts),
            soft_wrap=True,
        )
        return
    console.print(
        f"{label}: {command}{''.join(text for text, _ in detail_parts)}",
        soft_wrap=True,
    )


def details(
    rows: Iterable[tuple[str, str]],
    *,
    title: str | None = None,
    label_style: str = "bold",
    err: bool = False,
) -> None:
    """Show concise labelled values in a table on a terminal or text elsewhere."""
    entries = list(rows)
    console = _console(err)
    if _rich_terminal(err):
        if title:
            console.print(Text(title, style="bold"))
        # One line per row, soft-wrapped by the terminal rather than folded by
        # rich, so fingerprints and paths survive a copy at any width.
        width = max((len(label) for label, _ in entries), default=0)
        for label, value in entries:
            console.print(
                Text.assemble((label.ljust(width) + " ", label_style), (value, MINT)),
                soft_wrap=True,
            )
        return
    lines = ([title] if title else []) + [
        f"{label}: {value}" for label, value in entries
    ]
    console.print("\n".join(lines), soft_wrap=True)


def styled(parts: Iterable[tuple[str, str]], *, err: bool = False) -> None:
    """Show one line of (text, style) parts; plain text off a terminal."""
    entries = list(parts)
    console = _console(err)
    if _rich_terminal(err):
        console.print(Text.assemble(*entries), soft_wrap=True)
        return
    console.print("".join(text for text, _ in entries), soft_wrap=True)


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
    console.print("\n".join(lines), soft_wrap=True)


def info(message: str | list[tuple[str, str]], *, err: bool = False) -> None:
    _message(message, MINT, err=err)


def success(message: str, *, err: bool = False) -> None:
    _message(message, LIME, err=err, symbol="✓")


def failure(message: str) -> None:
    _message(message, "red", err=True, symbol="✗")


def warning(title: str, messages: Iterable[str]) -> None:
    items = list(messages)
    if _rich_terminal(True):
        console = _console(True)
        console.print(Text(title, style="bold yellow"))
        for message in items:
            console.print(Text.assemble(("• ", "yellow"), message))
        return
    for message in items:
        _console(True).print(f"Warning: {title}: {message}", soft_wrap=True)


def _labeled_block(rows: list[tuple[str, str]], *, heading: str, err: bool) -> None:
    """Render label/value rows under a heading, marked by a mint accent bar."""
    if _rich_terminal(err):
        console = _console(err)
        console.print(Text.assemble(("▌ ", MINT), (heading, "bold")))
        # Soft-wrapped lines, as in `details`, so long paths copy whole.
        width = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            console.print(
                Text.assemble(
                    ("▌ ", MINT), (label.ljust(width) + " ", "bold"), (value, MINT)
                ),
                soft_wrap=True,
            )
        return
    lines = [heading, *(f"  {label}: {value}" for label, value in rows)]
    _console(err).print("\n".join(lines), soft_wrap=True)


def run_plan(
    *,
    name: str,
    type: str,
    command: tuple[str, ...],
    port: int,
    url: str,
    dry_run: bool,
    app_port: int | None = None,
    project_root: Path | None = None,
    working_directory: Path | None = None,
    tailnet_url: str | None = None,
) -> None:
    rows = []
    if project_root:
        rows.append(("Project root", str(project_root)))
    if working_directory and working_directory != project_root:
        rows.append(("Working directory", str(working_directory)))
    rows.extend(
        [
            ("Command", shlex.join(command)),
            ("Host port", str(port)),
        ]
    )
    if app_port is not None:
        rows.append(("Isolated port", str(app_port)))
    rows.append(("Public URL", url))
    if tailnet_url:
        rows.append(("Tailnet URL", tailnet_url))
    heading = f"Dry run: {name}" if dry_run else f"Running {name}"
    # A custom command says nothing as a type; the Command row shows it.
    if type != "custom":
        heading += f" ({type})"
    _labeled_block(rows, heading=heading, err=dry_run)


def compose_dry_run(*, project: str, url: str, tailnet_url: str | None = None) -> None:
    """The Compose analogue of `run_plan`'s dry-run block.

    Compose owns its own port and command, so those rows never apply here;
    only the project name and the public URL are meaningful.
    """
    rows = [("Public URL", url)]
    if tailnet_url:
        rows.append(("Tailnet URL", tailnet_url))
    _labeled_block(rows, heading=f"Dry run: {project} (compose)", err=True)


def routes(items: Iterable[tuple[str, str]]) -> None:
    entries = list(items)
    if not entries:
        info("No application routes are active yet.")
        return
    details(entries, title="Active routes", label_style="default")


def interrupt_break() -> None:
    """Give the terminal's own ^C echo a line to itself.

    The tty echoes ^C wherever the cursor happens to be, so teardown output
    runs straight on from it. Only a terminal shows that echo -- redirected
    output would gain nothing but a stray blank line.

    This asks the stream itself rather than `_rich_terminal`, which answers
    "should this be styled" and is deliberately swayed by FORCE_COLOR. The
    question here is whether a terminal echoed anything, which no environment
    variable can make true.
    """
    if sys.stdout.isatty():
        _console(False).print()


def _message(
    message: str | list[tuple[str, str]], color: str, *, err: bool, symbol: str = "•"
) -> None:
    """Show a message; one given as (text, style) parts can highlight a command."""
    parts = [message] if isinstance(message, str) else message
    console = _console(err)
    if _rich_terminal(err):
        console.print(Text.assemble((f"{symbol} ", color), *parts), soft_wrap=True)
    else:
        plain = "".join(part if isinstance(part, str) else part[0] for part in parts)
        console.print(plain, soft_wrap=True)


def _console(err: bool) -> Console:
    return Console(stderr=err, highlight=False)


def _rich_terminal(err: bool) -> bool:
    return _console(err).is_terminal
