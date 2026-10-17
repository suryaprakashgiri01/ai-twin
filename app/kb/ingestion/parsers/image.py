"""Images and screenshots through RapidOCR (ONNX, local). Lines are kept in reading order."""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from app.kb.ingestion.models import IngestError, IngestErrorCode


class OcrEngine(Protocol):
    def __call__(self, image: bytes) -> list[str]:
        """Text lines found in an encoded image (PNG/JPEG/WebP bytes)."""
        ...


class RapidOcrEngine:
    def __init__(self) -> None:
        from rapidocr import RapidOCR

        self._engine = RapidOCR()

    def __call__(self, image: bytes) -> list[str]:
        result = self._engine(image)
        return [str(line) for line in (getattr(result, "txts", None) or ())]


@lru_cache(maxsize=1)
def default_engine() -> OcrEngine:
    return RapidOcrEngine()


def to_markdown(data: bytes, *, ocr: OcrEngine | None = None) -> str:
    try:
        lines = (ocr or default_engine())(data)
    except Exception as exc:
        raise IngestError(IngestErrorCode.PARSE_FAILED, "OCR failed on the image") from exc
    return "\n".join(line.strip() for line in lines if line.strip())
