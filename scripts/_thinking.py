"""A one-line "thinking" indicator for the terminal chats while the API has not answered yet."""

from __future__ import annotations

import os
import random
import sys
import threading
from collections.abc import Sequence
from types import TracebackType

from scripts._common import paint

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
FRAME_SECONDS = 0.1
PHRASE_SECONDS = 2.5

ASK_PHRASES = (
    "Looking through your vault…",
    "Reading your notes…",
    "Checking your records…",
    "Pulling the relevant bits together…",
    "Lining up the sources…",
    "Weighing what matters…",
    "Connecting the dots…",
    "Putting it in words…",
)
INTAKE_PHRASES = (
    "Reading what you said…",
    "Picking out the details…",
    "Matching it to an action…",
    "Checking what's still missing…",
    "Getting the draft ready…",
    "Thinking it over on your local model…",
    "Double-checking names and dates…",
)
TITLE_PHRASES = (
    "Summing up the conversation…",
    "Finding a good name for it…",
    "Noting anything worth remembering…",
)


def _enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


class Thinking:
    """Shows `prefix` plus a spinner and rotating phrases until `stop()`; then prints `prefix`.

    Without a terminal it shows nothing, so piped output and tests see only `prefix`."""

    def __init__(self, phrases: Sequence[str], prefix: str = "", visible_prefix: str = "") -> None:
        self._phrases = random.sample(list(phrases), len(phrases))
        self._prefix = prefix
        self._width = len(visible_prefix)
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._shown = 0

    def __enter__(self) -> Thinking:
        if _enabled():
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def stop(self) -> None:
        """Clear the indicator (once) and print the prefix in its place."""
        if self._stopped:
            return
        self._stopped = True
        if self._thread is not None:
            self._done.set()
            self._thread.join()
            sys.stdout.write("\r" + " " * self._shown + "\r")
        sys.stdout.write(self._prefix)
        sys.stdout.flush()

    def _spin(self) -> None:
        tick, per_phrase = 0, max(1, round(PHRASE_SECONDS / FRAME_SECONDS))
        while not self._done.wait(0 if tick == 0 else FRAME_SECONDS):
            phrase = self._phrases[(tick // per_phrase) % len(self._phrases)]
            text = f"{FRAMES[tick % len(FRAMES)]} {phrase}"
            pad = " " * max(0, self._shown - self._width - len(text))
            sys.stdout.write(f"\r{self._prefix}{paint(text, 'dim')}{pad}")
            sys.stdout.flush()
            self._shown = max(self._shown, self._width + len(text))
            tick += 1
