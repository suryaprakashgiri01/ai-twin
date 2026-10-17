"""Detect a file's type from its bytes, check the name agrees, and route it to a parser.

The extension is only a cross-check: `report.pdf` that is really a PNG is refused with the
detected type named, so a renamed file can never reach a parser meant for something else.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePath

from app.kb.ingestion.models import IngestError, IngestErrorCode, Parsed
from app.kb.ingestion.parsers import image, office, pdf, text
from app.kb.ingestion.parsers.image import OcrEngine

SUPPORTED: frozenset[str] = frozenset(
    {"pdf", "docx", "xlsx", "pptx", "csv", "md", "txt", "html", "png", "jpeg", "webp"}
)
# Extension -> the detected type it may hold. Text formats cannot be told apart by content
# alone, so a text extension accepts any text and names the parser to use.
_EXTENSIONS: dict[str, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "xlsx": "xlsx",
    "pptx": "pptx",
    "csv": "csv",
    "md": "md",
    "markdown": "md",
    "txt": "txt",
    "html": "html",
    "htm": "html",
    "png": "png",
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "webp": "webp",
}
_TEXT_TYPES = frozenset({"csv", "md", "txt", "html"})
_OOXML_PARTS = {
    "word/document.xml": "docx",
    "xl/workbook.xml": "xlsx",
    "ppt/presentation.xml": "pptx",
}
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF8", "gif"),
    (b"\xd0\xcf\x11\xe0", "ole2"),  # legacy .doc/.xls/.ppt
    (b"\x1f\x8b", "gzip"),
    (b"7z\xbc\xaf", "7z"),
    (b"MZ", "exe"),
    (b"\x7fELF", "elf"),
)


def _zip_type(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return "binary"
    for part, kind in _OOXML_PARTS.items():
        if part in names:
            return kind
    return "zip"


def _text_type(data: bytes) -> str:
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "binary"
    if "\x00" in decoded:
        return "binary"
    head = decoded.lstrip()[:512].lower()
    if head.startswith(("<!doctype html", "<html")) or "<body" in head:
        return "html"
    return "txt"


def detect(data: bytes) -> str:
    """The type the bytes are, whatever the file is called."""
    for signature, kind in _SIGNATURES:
        if data.startswith(signature):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"PK\x03\x04"):
        return _zip_type(data)
    return _text_type(data)


def resolve(data: bytes, filename: str | None) -> str:
    """The parser to use. Raises VALIDATION-mapped errors that name the detected type."""
    if not data.strip():
        raise IngestError(IngestErrorCode.EMPTY_CONTENT, "The file is empty")
    detected = detect(data)
    ext = PurePath(filename or "").suffix.lower().lstrip(".")
    claimed = _EXTENSIONS.get(ext) if ext else None
    if ext and claimed is None:
        raise IngestError(
            IngestErrorCode.UNSUPPORTED_TYPE,
            f"Unsupported file extension .{ext} (detected type: {detected})",
            detected=detected,
        )
    if detected not in SUPPORTED:
        raise IngestError(
            IngestErrorCode.UNSUPPORTED_TYPE,
            f"Unsupported file type: detected {detected}",
            detected=detected,
        )
    if claimed is None:
        return detected
    if detected in _TEXT_TYPES and claimed in _TEXT_TYPES:
        # Named .md/.csv/.txt: parse as named; HTML content stays HTML.
        return "html" if detected == "html" else claimed
    if claimed != detected:
        raise IngestError(
            IngestErrorCode.TYPE_MISMATCH,
            f"File named .{ext} but its content is {detected}",
            detected=detected,
        )
    return detected


def parse(data: bytes, filename: str | None = None, *, ocr: OcrEngine | None = None) -> Parsed:
    kind = resolve(data, filename)
    try:
        if kind == "pdf":
            markdown, ocr_used = pdf.to_markdown(data, ocr=ocr)
        elif kind in ("png", "jpeg", "webp"):
            markdown, ocr_used = image.to_markdown(data, ocr=ocr), True
        elif kind in ("md", "txt"):
            markdown, ocr_used = text.to_markdown(data), False
        else:
            markdown, ocr_used = office.to_markdown(data, kind), False
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(
            IngestErrorCode.PARSE_FAILED, f"Could not parse the {kind} file", detected=kind
        ) from exc
    if not markdown.strip():
        raise IngestError(
            IngestErrorCode.EMPTY_CONTENT, f"No text found in the {kind} file", detected=kind
        )
    title = PurePath(filename).stem if filename else None
    return Parsed(markdown=markdown.strip() + "\n", detected=kind, title=title, ocr_used=ocr_used)
