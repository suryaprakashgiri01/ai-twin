"""The LinkedIn "Get a copy of your data" ZIP: one document per profile entity.

Profile, positions, education, skills, certifications and projects are read. Messages,
connections and every other file are ignored; an unknown CSV is logged by name only, so a
format change degrades to "less data", never to a failure or a leak.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Callable
from pathlib import PurePosixPath

import structlog

from app.kb.ingestion.models import IngestError, IngestErrorCode, Parsed

logger = structlog.get_logger(__name__)

MAX_MEMBERS = 500
MAX_UNCOMPRESSED = 200 * 1024 * 1024
# Read deliberately never: private conversations and other people's data.
IGNORED = frozenset(
    {
        "messages",
        "connections",
        "invitations",
        "imported contacts",
        "phonenumbers",
        "email addresses",
    }
)

Row = dict[str, str]


def _rows(text: str) -> list[Row]:
    """CSV rows, skipping any preamble before the header (some exports start with notes)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.count(",") >= 1), 0)
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader]


def _pick(row: Row, *keys: str) -> str:
    for key in keys:
        if row.get(key):
            return row[key]
    return ""


def _dates(row: Row) -> str:
    start, end = _pick(row, "Started On", "Start Date"), _pick(row, "Finished On", "End Date")
    if not start and not end:
        return ""
    return f"{start or '?'} - {end or 'present'}"


def _doc(title: str, category: str, key: str, body: list[str]) -> Parsed:
    text = "\n".join([f"# {title}", "", *[line for line in body if line]]) + "\n"
    return Parsed(markdown=text, detected="linkedin", title=title, category=category, key=key)


def _profile(rows: list[Row]) -> list[Parsed]:
    docs = []
    for row in rows[:1]:
        name = " ".join(p for p in (row.get("First Name"), row.get("Last Name")) if p)
        body = [_pick(row, "Headline"), "", _pick(row, "Summary"), _pick(row, "Industry")]
        docs.append(_doc(name or "Profile", "persona", "profile", body))
    return docs


def _positions(rows: list[Row]) -> list[Parsed]:
    return [
        _doc(
            f"{_pick(r, 'Title')} at {_pick(r, 'Company Name')}".strip(),
            "experience",
            f"position:{i}",
            [_dates(r), _pick(r, "Location"), "", _pick(r, "Description")],
        )
        for i, r in enumerate(rows)
    ]


def _education(rows: list[Row]) -> list[Parsed]:
    return [
        _doc(
            _pick(r, "School Name") or "Education",
            "education",
            f"education:{i}",
            [_pick(r, "Degree Name"), _dates(r), _pick(r, "Notes"), _pick(r, "Activities")],
        )
        for i, r in enumerate(rows)
    ]


def _skills(rows: list[Row]) -> list[Parsed]:
    names = [_pick(r, "Name", "Skill") for r in rows]
    names = [n for n in names if n]
    return [_doc("Skills", "skill", "skills", [f"- {n}" for n in names])] if names else []


def _certifications(rows: list[Row]) -> list[Parsed]:
    return [
        _doc(
            _pick(r, "Name") or "Certification",
            "certification",
            f"certification:{i}",
            [_pick(r, "Authority"), _dates(r), _pick(r, "Url")],
        )
        for i, r in enumerate(rows)
    ]


def _projects(rows: list[Row]) -> list[Parsed]:
    return [
        _doc(
            _pick(r, "Title") or "Project",
            "project",
            f"project:{i}",
            [_dates(r), _pick(r, "Url"), "", _pick(r, "Description")],
        )
        for i, r in enumerate(rows)
    ]


PARSERS: dict[str, Callable[[list[Row]], list[Parsed]]] = {
    "profile": _profile,
    "positions": _positions,
    "education": _education,
    "skills": _skills,
    "certifications": _certifications,
    "projects": _projects,
}


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise IngestError(IngestErrorCode.UNSUPPORTED_TYPE, "Not a ZIP archive") from exc
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise IngestError(IngestErrorCode.TOO_LARGE, "The archive is too large when unpacked")
    return archive


def parse_export(data: bytes) -> list[Parsed]:
    docs: list[Parsed] = []
    with _open(data) as archive:
        for info in sorted(archive.infolist(), key=lambda i: i.filename):
            path = PurePosixPath(info.filename)
            if info.is_dir() or path.suffix.lower() != ".csv":
                continue
            stem = path.stem.lower()
            parser = PARSERS.get(stem)
            if parser is None:
                if stem not in IGNORED:
                    logger.info("linkedin export file skipped", file=path.name)
                continue
            text = archive.read(info).decode("utf-8-sig", errors="replace")
            docs += parser(_rows(text))
    if not docs:
        raise IngestError(IngestErrorCode.EMPTY_CONTENT, "No profile data found in the export")
    return docs
