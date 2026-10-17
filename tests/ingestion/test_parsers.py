"""Ingestion T3: content-sniffed dispatch and every supported parser (OCR faked)."""

from __future__ import annotations

import pytest

from app.kb.ingestion.models import IngestError, IngestErrorCode
from app.kb.ingestion.parsers import dispatch
from tests.ingestion import persona


class FakeOcr:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.calls = 0

    def __call__(self, image: bytes) -> list[str]:
        self.calls += 1
        assert image.startswith(b"\x89PNG")
        return self.lines


@pytest.mark.parametrize(
    ("filename", "data", "expected", "needle"),
    [
        ("note.md", persona.note().encode(), "md", "## Round 1"),
        ("note.txt", b"\xef\xbb\xbfplain\r\ntext", "txt", "plain\ntext"),
        ("table.csv", b"skill,years\npython,6\n", "csv", "| skill | years |"),
        ("page.html", b"<html><body><h1>Hi</h1><p>There</p></body></html>", "html", "# Hi"),
        ("page.txt", b"<!DOCTYPE html><html><body><p>Kept</p></body></html>", "html", "Kept"),
        ("cv.pdf", persona.pdf_bytes(f"{persona.NAME} backend"), "pdf", "Testpal"),
        ("cv.docx", persona.docx_bytes("Built a rate limiter"), "docx", "rate limiter"),
        ("s.xlsx", persona.xlsx_bytes([["skill", "years"], ["go", "3"]]), "xlsx", "go"),
        ("deck.pptx", persona.pptx_bytes("Quarterly demo"), "pptx", "Quarterly demo"),
        (None, b"no name at all", "txt", "no name"),
    ],
    ids=["md", "txt-bom", "csv", "html", "html-as-txt", "pdf", "docx", "xlsx", "pptx", "noname"],
)
def test_each_type_parses_to_markdown(
    filename: str | None, data: bytes, expected: str, needle: str
) -> None:
    parsed = dispatch.parse(data, filename, ocr=FakeOcr([]))
    assert parsed.detected == expected
    assert needle in parsed.markdown
    assert parsed.markdown.strip()


def test_image_goes_through_ocr() -> None:
    ocr = FakeOcr(["Screenshot of", "  a build log  "])
    parsed = dispatch.parse(persona.png_bytes(), "shot.png", ocr=ocr)
    assert parsed.detected == "png"
    assert parsed.markdown == "Screenshot of\na build log\n"
    assert parsed.ocr_used and ocr.calls == 1


def test_pdf_page_without_text_layer_is_ocrd() -> None:
    ocr = FakeOcr(["scanned certificate text"])
    parsed = dispatch.parse(persona.image_pdf_bytes(), "scan.pdf", ocr=ocr)
    assert ocr.calls == 1
    assert parsed.ocr_used
    assert "scanned certificate text" in parsed.markdown


@pytest.mark.parametrize(
    ("data", "detected"),
    [
        (b"\xff\xd8\xff\xe0" + b"0" * 20, "jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b"GIF89a....", "gif"),
        (b"PK\x03\x04garbage", "binary"),
        (b"\x00\x01\x02\xff\xfe", "binary"),
    ],
)
def test_detect_by_magic_bytes(data: bytes, detected: str) -> None:
    assert dispatch.detect(data) == detected


def test_plain_zip_is_not_office() -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "hi")
    assert dispatch.detect(buffer.getvalue()) == "zip"


def test_mislabelled_file_names_the_detected_type() -> None:
    with pytest.raises(IngestError) as caught:
        dispatch.parse(persona.png_bytes(), "resume.pdf")
    assert caught.value.code == IngestErrorCode.TYPE_MISMATCH
    assert caught.value.detected == "png"
    assert "png" in caught.value.message


@pytest.mark.parametrize(
    ("filename", "data", "detected"),
    [
        ("anim.gif", b"GIF89a....", "gif"),
        ("tool.exe", b"MZ\x90\x00", "exe"),
        ("anim.png", b"GIF89a....", "gif"),
        (None, b"\x00\x01binary\xff", "binary"),
    ],
)
def test_unsupported_types_are_refused(filename: str | None, data: bytes, detected: str) -> None:
    with pytest.raises(IngestError) as caught:
        dispatch.parse(data, filename)
    assert caught.value.code == IngestErrorCode.UNSUPPORTED_TYPE
    assert caught.value.detected == detected


def test_empty_and_textless_files_are_refused() -> None:
    with pytest.raises(IngestError) as empty:
        dispatch.parse(b"  \n", "blank.txt")
    assert empty.value.code == IngestErrorCode.EMPTY_CONTENT
    with pytest.raises(IngestError) as no_text:
        dispatch.parse(persona.png_bytes(), "blank.png", ocr=FakeOcr([]))
    assert no_text.value.code == IngestErrorCode.EMPTY_CONTENT


def test_parser_crash_becomes_parse_failed() -> None:
    with pytest.raises(IngestError) as caught:
        dispatch.parse(b"%PDF-1.7 not really a pdf", "broken.pdf")
    assert caught.value.code == IngestErrorCode.PARSE_FAILED
    assert caught.value.detected == "pdf"


def test_ocr_failure_is_parse_failed() -> None:
    def broken(image: bytes) -> list[str]:
        raise RuntimeError("onnx")

    with pytest.raises(IngestError) as caught:
        dispatch.parse(persona.png_bytes(), "x.png", ocr=broken)
    assert caught.value.code == IngestErrorCode.PARSE_FAILED


def test_markitdown_import_leaves_the_environment_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from app.kb.ingestion.parsers.office import without_new_env

    monkeypatch.setenv("KEPT_VAR", "1")

    def load() -> str:
        os.environ["DOTENV_PROBE_KEY"] = "secret"
        return "ok"

    assert without_new_env(load) == "ok"
    assert "DOTENV_PROBE_KEY" not in os.environ and os.environ["KEPT_VAR"] == "1"
