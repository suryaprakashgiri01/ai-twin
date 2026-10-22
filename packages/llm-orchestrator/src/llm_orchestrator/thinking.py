"""Keep a reasoning model's thinking out of answers.

Ollama models that declare `reasoning` run with thinking separated by the client, so their
content is already clean. Other models (and Ollama ones without the flag) may still put
`<think>…</think>` in the text. `strip_thinking` removes it from a whole reply;
`ThinkingFilter` does the same for a stream whose tags may be split across chunks. The thinking
text itself is dropped, never logged or returned; callers log only how much was removed.
"""

from __future__ import annotations

import re

OPEN = "<think>"
CLOSE = "</think>"
_BLOCK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL)


def strip_thinking(text: str) -> tuple[str, int]:
    """(text without thinking, characters removed). An unclosed block runs to the end."""
    if OPEN not in text:
        return text, 0
    cleaned = _BLOCK.sub("", text)
    return cleaned.lstrip(), len(text) - len(cleaned)


class ThinkingFilter:
    """Feed streamed text; get back only what is outside `<think>` blocks."""

    def __init__(self) -> None:
        self._inside = False
        self._pending = ""
        self.removed = 0
        self._started = False

    def feed(self, chunk: str) -> str:
        data = self._pending + chunk
        self._pending = ""
        out: list[str] = []
        while data:
            tag = CLOSE if self._inside else OPEN
            at = data.find(tag)
            if at == -1:
                keep = _partial_suffix(data, tag)
                body, self._pending = data[: len(data) - keep], data[len(data) - keep :]
                self._emit(body, out)
                break
            self._emit(data[:at], out)
            if self._inside:
                self.removed += len(tag)
            self._inside = not self._inside
            if self._inside:
                self.removed += len(tag)
            data = data[at + len(tag) :]
        text = "".join(out)
        if not self._started:
            text = text.lstrip()
            self._started = bool(text)
        return text

    def flush(self) -> str:
        """What is left at the end of the stream (a partial tag that never completed)."""
        rest, self._pending = self._pending, ""
        if self._inside:
            self.removed += len(rest)
            return ""
        return rest

    def _emit(self, body: str, out: list[str]) -> None:
        if self._inside:
            self.removed += len(body)
        else:
            out.append(body)


def _partial_suffix(data: str, tag: str) -> int:
    """How many trailing characters could be the start of `tag` (held back for the next chunk)."""
    for size in range(min(len(tag) - 1, len(data)), 0, -1):
        if tag.startswith(data[-size:]):
            return size
    return 0
