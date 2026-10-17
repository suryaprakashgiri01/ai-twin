"""Markdown-aware chunking that keeps heading context in every chunk.

Text is split into sections at headings; paragraphs are packed into chunks of about `target`
tokens, and each new chunk repeats the last `overlap` tokens of the previous one. Every chunk
starts with its heading path ("Title > Section > Subsection"), so a chunk read alone still says
what it is about. Tokens are counted as words and punctuation marks, a close enough estimate
for a 512-token embedding model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.kb.ingestion.models import ChunkDraft

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TOKEN = re.compile(r"\w+|[^\w\s]")


def count_tokens(text: str) -> int:
    return len(_TOKEN.findall(text))


@dataclass
class _Section:
    path: list[str]
    paragraphs: list[str] = field(default_factory=list)


def _sections(markdown: str) -> list[_Section]:
    sections = [_Section(path=[])]
    stack: list[tuple[int, str]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph and "".join(paragraph).strip():
            sections[-1].paragraphs.append("\n".join(paragraph).strip())
        paragraph.clear()

    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            stack = [(lvl, text) for lvl, text in stack if lvl < level]
            stack.append((level, heading.group(2).strip()))
            sections.append(_Section(path=[text for _, text in stack]))
        elif line.strip():
            paragraph.append(line.rstrip())
        else:
            flush()
    flush()
    return [s for s in sections if s.paragraphs]


def _split_long(paragraph: str, target: int) -> list[str]:
    words = paragraph.split()
    if count_tokens(paragraph) <= target:
        return [paragraph]
    pieces, current = [], []
    for word in words:
        current.append(word)
        if count_tokens(" ".join(current)) >= target:
            pieces.append(" ".join(current))
            current = []
    if current:
        pieces.append(" ".join(current))
    return pieces


def _tail(text: str, overlap: int) -> str:
    if overlap <= 0:
        return ""
    words = text.split()
    tail: list[str] = []
    for word in reversed(words):
        if count_tokens(" ".join([word, *tail])) > overlap:
            break
        tail.insert(0, word)
    return " ".join(tail)


def _context(title: str, path: list[str]) -> str:
    parts = [title] + [p for p in path if p.lower() != title.lower()]
    return " > ".join(p for p in parts if p)


def chunk_markdown(
    markdown: str, *, title: str, target: int = 400, overlap: int = 60
) -> list[ChunkDraft]:
    if overlap >= target:
        raise ValueError("overlap must be smaller than target")
    chunks: list[ChunkDraft] = []
    for section in _sections(markdown):
        heading = _context(title, section.path)
        budget = max(1, target - count_tokens(heading))
        units = [p for para in section.paragraphs for p in _split_long(para, budget)]
        body: list[str] = []
        for unit in units:
            candidate = "\n\n".join([*body, unit])
            if body and count_tokens(candidate) > budget:
                text = "\n\n".join(body)
                chunks.append(_draft(len(chunks), heading, text))
                tail = _tail(text, overlap)
                body = [tail] if tail else []
            body.append(unit)
        if body:
            chunks.append(_draft(len(chunks), heading, "\n\n".join(body)))
    return chunks


def _draft(ordinal: int, heading: str, body: str) -> ChunkDraft:
    text = f"{heading}\n\n{body}" if heading else body
    return ChunkDraft(ordinal=ordinal, text=text, heading=heading)
