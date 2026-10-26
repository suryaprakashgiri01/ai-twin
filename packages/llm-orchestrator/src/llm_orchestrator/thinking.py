"""Keep a reasoning model's thinking out of answers, and hand it to whoever may show it.

Ollama models that declare `reasoning` run with thinking separated by the client: it arrives in
`additional_kwargs["reasoning_content"]`, never in the content. Other models may still put
`<think>…</think>` in the text. `strip_thinking` splits a whole reply; `ThinkingFilter` does the
same for a stream whose tags may be split across chunks.

The thinking is never part of an answer and never logged (only its size). Owner channels show
it as a separate `thinking` event; the public channel drops it.
"""

from __future__ import annotations

import re

OPEN = "<think>"
CLOSE = "</think>"
REASONING_KEY = "reasoning_content"
_BLOCK = re.compile(r"<think>(.*?)(?:</think>|\Z)", re.DOTALL)


def strip_thinking(text: str) -> tuple[str, str]:
    """(answer text, thinking text). An unclosed block runs to the end."""
    if OPEN not in text:
        return text, ""
    thinking = "\n".join(m.strip() for m in _BLOCK.findall(text) if m.strip())
    return _BLOCK.sub("", text).lstrip(), thinking


class ThinkingFilter:
    """Feed streamed text; get back only what is outside `<think>` blocks. What was inside is
    collected: `take()` returns the thinking seen since the last call."""

    def __init__(self) -> None:
        self._inside = False
        self._pending = ""
        self._started = False
        self._thought: list[str] = []
        self.removed = 0

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
            self.removed += len(tag)
            self._inside = not self._inside
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
            self._emit(rest, [])
            return ""
        return rest

    def take(self) -> str:
        thought, self._thought = "".join(self._thought), []
        return thought

    def _emit(self, body: str, out: list[str]) -> None:
        if self._inside:
            self.removed += len(body)
            self._thought.append(body)
        else:
            out.append(body)


def _partial_suffix(data: str, tag: str) -> int:
    """How many trailing characters could be the start of `tag` (held back for the next chunk)."""
    for size in range(min(len(tag) - 1, len(data)), 0, -1):
        if tag.startswith(data[-size:]):
            return size
    return 0
