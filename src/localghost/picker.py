"""Interactive terminal picker for remembered projects.

`summon` with no name uses this when attached to a terminal: navigate the
registry with the arrow keys, Enter summons, Delete forgets, q leaves.
The model and key handling are pure so they can be tested without a TTY;
only `pick` touches the terminal, mirroring how the status bar keeps its
raw-terminal handling at the edge.
"""

from __future__ import annotations

import os
import shutil
import sys
import termios
import tty
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .feedback import LIME, MINT
from .registry import RegistryEntry

QUIT = object()


def _fg(hex_colour: str) -> str:
    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return f"\x1b[38;2;{r};{g};{b}m"


_HIGHLIGHT = "\x1b[1m" + _fg(LIME)
_NOTICE = _fg(MINT)
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


@dataclass(frozen=True)
class Summon:
    entry: RegistryEntry


@dataclass(frozen=True)
class Forgot:
    entry: RegistryEntry


def decode_key(data: bytes) -> str:
    if data in (b"\x1b[A", b"k"):
        return "up"
    if data in (b"\x1b[B", b"j"):
        return "down"
    if data in (b"\r", b"\n"):
        return "enter"
    if data in (b"\x1b[3~", b"\x7f", b"\x08"):
        return "delete"
    if data == b"u":
        return "undo"
    if data in (b"q", b"\x1b"):
        return "quit"
    if data == b"\x03":
        return "interrupt"
    return "other"


@dataclass(frozen=True)
class Restored:
    entry: RegistryEntry


@dataclass
class PickerModel:
    entries: list[RegistryEntry]
    selected: int = 0
    notice: str = ""
    last_forgotten: tuple[int, RegistryEntry] | None = None

    def current(self) -> RegistryEntry | None:
        if not self.entries:
            return None
        return self.entries[self.selected]

    def apply(self, key: str) -> Summon | Forgot | object | None:
        if key == "up":
            self.selected = max(0, self.selected - 1)
            return None
        if key == "down":
            self.selected = min(max(len(self.entries) - 1, 0), self.selected + 1)
            return None
        if key == "enter":
            entry = self.current()
            return Summon(entry) if entry else None
        if key == "delete":
            entry = self.current()
            if entry is None:
                return None
            index = self.selected
            self.entries.remove(entry)
            self.selected = min(self.selected, max(len(self.entries) - 1, 0))
            self.last_forgotten = (index, entry)
            self.notice = f"Forgot {entry.name}. (u to undo)"
            return Forgot(entry)
        if key == "undo":
            if self.last_forgotten is None:
                return None
            index, entry = self.last_forgotten
            self.last_forgotten = None
            index = min(index, len(self.entries))
            self.entries.insert(index, entry)
            self.selected = index
            self.notice = f"Remembered {entry.name} again."
            return Restored(entry)
        if key == "quit":
            return QUIT
        return None


def _relative(stamp: str) -> str:
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return "a while ago"
    elapsed = datetime.now(UTC) - when.astimezone(UTC)
    minutes = int(elapsed.total_seconds() // 60)
    if minutes < 1:
        return "moments ago"
    if minutes < 60:
        return f"{minutes} minutes ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} hours ago"
    return f"{minutes // (24 * 60)} days ago"


def _tilde(directory: str) -> str:
    home = str(Path.home())
    if directory == home or directory.startswith(home + "/"):
        return "~" + directory[len(home) :]
    return directory


def render_lines(model: PickerModel, width: int = 80) -> list[str]:
    lines = [
        f"{_DIM}Remembered projects — Enter summon · Del forget · q or Esc quit{_RESET}"
    ]
    host_width = max((len(e.hostname) for e in model.entries), default=0)
    type_width = max((len(e.type) for e in model.entries), default=0)
    for index, entry in enumerate(model.entries):
        row = (
            f"  {entry.hostname:<{host_width}}  {entry.type:<{type_width}}  "
            f"{_tilde(entry.directory)}  ({_relative(entry.last_started)})"
        )
        # Clip before styling so a wrapped line never breaks the redraw's
        # cursor arithmetic, and style by index so the clip stays honest.
        if len(row) > width:
            row = row[: max(width - 1, 1)] + "…"
        if index == model.selected:
            row = f"{_HIGHLIGHT}» {row[2:]}{_RESET}"
        else:
            split = min(2 + host_width, len(row))
            row = f"{row[:split]}{_DIM}{row[split:]}{_RESET}"
        lines.append(row)
    if not model.entries:
        lines.append(f"{_DIM}Nothing remembered any more.{_RESET}")
    if model.notice:
        lines.append(f"{_NOTICE}{model.notice}{_RESET}")
    return lines


def pick(
    entries: list[RegistryEntry],
    *,
    forget: Callable[[str], bool],
    restore: Callable[[RegistryEntry], None],
) -> RegistryEntry | None:
    """Run the picker; return the entry to summon, or None."""
    model = PickerModel(list(entries))
    out = sys.stdout
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    drawn = 0

    def redraw() -> None:
        nonlocal drawn
        if drawn:
            out.write(f"\x1b[{drawn}A")
        lines = render_lines(model, width=shutil.get_terminal_size().columns)
        for line in lines:
            out.write(f"\x1b[2K{line}\r\n")
        # A shrinking list (after Delete) leaves a stale line behind.
        for _ in range(drawn - len(lines)):
            out.write("\x1b[2K\r\n")
        extra = max(drawn - len(lines), 0)
        if extra:
            out.write(f"\x1b[{extra}A")
        drawn = len(lines)
        out.flush()

    out.write("\x1b[?25l")
    tty.setcbreak(fd)
    try:
        while True:
            redraw()
            key = decode_key(os.read(fd, 6))
            if key == "interrupt":
                raise KeyboardInterrupt
            outcome = model.apply(key)
            if outcome is QUIT:
                return None
            if isinstance(outcome, Forgot):
                forget(outcome.entry.name)
            elif isinstance(outcome, Restored):
                restore(outcome.entry)
            elif isinstance(outcome, Summon):
                return outcome.entry
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write("\x1b[?25h")
        out.flush()
