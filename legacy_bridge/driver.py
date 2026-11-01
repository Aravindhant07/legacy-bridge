"""Terminal drivers. A driver runs or connects to a terminal application and gives
the bridge three things: send keys, read the screen, wait for the screen to settle.

VTDriver: runs any VT100/ANSI program in a pseudo-terminal and emulates the screen
with pyte. A 3270 driver (e.g. wrapping s3270) would implement the same interface.
"""

from __future__ import annotations

import fcntl
import os
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import pyte

KEYS = {
    "ENTER": "\r", "TAB": "\t", "BACKTAB": "\x1b[Z", "BACKSPACE": "\x7f", "ESC": "\x1b",
    "F1": "\x1bOP", "F2": "\x1bOQ", "F3": "\x1bOR", "F4": "\x1bOS", "F5": "\x1b[15~", "F6": "\x1b[17~",
    "F7": "\x1b[18~", "F8": "\x1b[19~", "F9": "\x1b[20~", "F10": "\x1b[21~", "F11": "\x1b[23~", "F12": "\x1b[24~",
}


@dataclass(frozen=True)
class Snapshot:
    lines: tuple[str, ...]  # one string per row, padded to the screen width
    cursor: tuple[int, int]  # (row, col), 1-based

    def text_at(self, row: int, col: int, length: int) -> str:
        """1-based row/column, like screen maps."""
        if not 1 <= row <= len(self.lines):
            return ""
        return self.lines[row - 1][col - 1: col - 1 + length]

    def find(self, needle: str) -> tuple[int, int] | None:
        for r, line in enumerate(self.lines, 1):
            c = line.find(needle)
            if c >= 0:
                return r, c + 1
        return None

    def render(self) -> str:
        return "\n".join(line.rstrip() for line in self.lines)


class TerminalDriver(Protocol):
    def start(self) -> None: ...
    def send(self, keys: str) -> None: ...
    def press(self, key: str) -> None: ...
    def snapshot(self) -> Snapshot: ...
    def settle(self, quiet: float = 0.15, timeout: float = 10.0, response_wait: float = 1.0) -> Snapshot: ...
    def wait_for(self, predicate: Callable[[Snapshot], bool], timeout: float = 10.0) -> Snapshot | None: ...
    def close(self) -> None: ...


class VTDriver:
    def __init__(self, argv: list[str], rows: int = 24, cols: int = 80, env: dict[str, str] | None = None):
        self.argv, self.rows, self.cols = argv, rows, cols
        self.env = {**os.environ, "TERM": "xterm", **(env or {})}
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self._lock = threading.Lock()
        self._last_output = time.monotonic()
        self._last_send = 0.0
        self._proc: subprocess.Popen | None = None
        self._fd: int | None = None
        self._reader: threading.Thread | None = None
        self._changed = threading.Condition(self._lock)

    def start(self) -> None:
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
        self._proc = subprocess.Popen(self.argv, stdin=slave, stdout=slave, stderr=slave, env=self.env,
                                      start_new_session=True, close_fds=True)
        os.close(slave)
        self._fd = master
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self._fd is not None
        while True:
            try:
                data = os.read(self._fd, 65536)
            except OSError:
                break
            if not data:
                break
            with self._changed:
                self.stream.feed(data)
                self._last_output = time.monotonic()
                self._changed.notify_all()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def send(self, keys: str) -> None:
        assert self._fd is not None, "driver not started"
        with self._lock:
            self._last_send = time.monotonic()
        os.write(self._fd, keys.encode())

    def press(self, key: str) -> None:
        self.send(KEYS[key])

    def snapshot(self) -> Snapshot:
        with self._lock:
            lines = tuple(self.screen.display)
            return Snapshot(lines, (self.screen.cursor.y + 1, self.screen.cursor.x + 1))

    def settle(self, quiet: float = 0.15, timeout: float = 10.0, response_wait: float = 1.0) -> Snapshot:
        """Wait for the program's response to the last keys sent, then for `quiet` seconds of silence.

        If nothing at all arrives within `response_wait` seconds of the last send, return the
        current screen (some keys legitimately change nothing on screen).
        """
        start = time.monotonic()
        deadline = start + timeout
        while time.monotonic() < deadline:
            with self._lock:
                now = time.monotonic()
                responded = self._last_output >= self._last_send
                idle = now - self._last_output
            if responded and idle >= quiet:
                break
            if not responded and now - self._last_send >= response_wait:
                break
            time.sleep(0.02)
        return self.snapshot()

    def wait_for(self, predicate: Callable[[Snapshot], bool], timeout: float = 10.0) -> Snapshot | None:
        deadline = time.monotonic() + timeout
        while True:
            snap = self.settle(quiet=0.05, timeout=max(0.0, deadline - time.monotonic()))
            if predicate(snap):
                return snap
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            with self._changed:
                self._changed.wait(timeout=min(remaining, 0.25))

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


def demo_bank_argv(*flags: str) -> list[str]:
    import sys

    return [sys.executable, "-m", "legacy_bridge.demo.bank_app", *flags]
