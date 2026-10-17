"""PDF through pymupdf4llm, page by page. A page with no text layer is rendered and OCR'd."""

from __future__ import annotations

from typing import Any

from app.kb.ingestion.parsers.image import OcrEngine, default_engine

OCR_DPI = 200


def _page_texts(doc: Any) -> list[str]:
    import pymupdf4llm

    # OCR is ours (below), so pages are handled the same way as screenshots.
    chunks = pymupdf4llm.to_markdown(doc, page_chunks=True, use_ocr=False, show_progress=False)
    return [str(chunk.get("text", "")) for chunk in chunks]


def to_markdown(data: bytes, *, ocr: OcrEngine | None = None) -> tuple[str, bool]:
    """Markdown for the whole file, and whether any page needed OCR."""
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        texts = _page_texts(doc)
        ocr_used = False
        for number, page in enumerate(doc):
            if page.get_text().strip():
                continue
            png = page.get_pixmap(dpi=OCR_DPI).tobytes("png")
            lines = (ocr or default_engine())(png)
            if number < len(texts):
                texts[number] = "\n".join(lines)
            ocr_used = True
    return "\n\n".join(t.strip() for t in texts if t.strip()), ocr_used
