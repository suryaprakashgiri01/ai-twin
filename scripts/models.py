"""Download the local models ingestion needs: `python -m scripts.models`.

- spaCy `en_core_web_md` (redaction NER): the release wheel is downloaded once, checked against
  its pinned SHA-256 and unpacked into `<MODELS_DIR>/spacy/en_core_web_md`. It lives outside the
  virtualenv, so `uv sync` never removes it.
- fastembed `BAAI/bge-small-en-v1.5`, `Qdrant/bm25` and the `Xenova/ms-marco-MiniLM-L-6-v2`
  reranker (with `--embeddings`): warmed into `<MODELS_DIR>/fastembed`; the app also fetches
  them on first use.

Both directories are git-ignored and left out of vault export. Re-running is a no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import httpx

from scripts._common import ROOT, fail, info, ok, read_env

SPACY_MODEL = "en_core_web_md"
SPACY_VERSION = "3.8.0"
SPACY_URL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    f"{SPACY_MODEL}-{SPACY_VERSION}/{SPACY_MODEL}-{SPACY_VERSION}-py3-none-any.whl"
)
SPACY_SHA256 = "5e6329fe3fecedb1d1a02c3ea2172ee0fede6cea6e4aefb6a02d832dba78a310"


def models_dir(env: dict[str, str] | None = None) -> Path:
    raw = (env if env is not None else read_env()).get("MODELS_DIR") or "vault/models"
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def spacy_path(base: Path) -> Path:
    return base / "spacy" / SPACY_MODEL


def spacy_ready(base: Path) -> bool:
    return (spacy_path(base) / "config.cfg").is_file()


def extract_spacy(wheel: bytes, target: Path) -> None:
    """Unpack `<model>/<model>-<version>/` from the wheel into `target`, atomically."""
    prefix = PurePosixPath(SPACY_MODEL, f"{SPACY_MODEL}-{SPACY_VERSION}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        staging = Path(tmp) / "model"
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if member.is_dir() or not path.is_relative_to(prefix):
                    continue
                relative = path.relative_to(prefix)
                if ".." in relative.parts:
                    raise ValueError("unsafe path in model wheel")
                out = staging.joinpath(*relative.parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(archive.read(member))
        if not (staging / "config.cfg").is_file():
            raise ValueError("model wheel has no config.cfg")
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)


def download(url: str, timeout: float = 300.0) -> bytes:
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def ensure_spacy(base: Path, fetch: Callable[[str], bytes] = download) -> bool:
    """True when the model had to be installed now, False when it was already there."""
    if spacy_ready(base):
        ok(f"spaCy {SPACY_MODEL} present")
        return False
    info(f"Downloading spaCy {SPACY_MODEL} {SPACY_VERSION} (~32 MB, once)")
    wheel = fetch(SPACY_URL)
    digest = hashlib.sha256(wheel).hexdigest()
    if digest != SPACY_SHA256:
        raise ValueError(f"spaCy model checksum mismatch ({digest[:12]}...)")
    extract_spacy(wheel, spacy_path(base))
    ok(f"spaCy {SPACY_MODEL} installed in {spacy_path(base)}")
    return True


def reranker_ready(base: Path) -> bool:
    from app.kb.retrieval.rerank import RERANK_MODEL

    name = RERANK_MODEL.split("/")[-1]
    cache = base / "fastembed"
    return cache.is_dir() and any(name in p.name for p in cache.iterdir())


def warm_embeddings(base: Path) -> None:  # pragma: no cover - downloads real models
    from app.kb.embedding import FastEmbedder
    from app.kb.retrieval.rerank import CrossEncoderReranker

    info("Downloading fastembed models (bge-small-en-v1.5, bm25, ms-marco-MiniLM-L-6-v2)")
    asyncio.run(FastEmbedder(base / "fastembed").embed(["warm up"]))
    CrossEncoderReranker(base / "fastembed")._score_sync("warm up", ["warm up"])
    ok("fastembed models ready")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embeddings", action="store_true", help="also fetch fastembed models")
    args = parser.parse_args(argv)
    base = models_dir()
    try:
        ensure_spacy(base)
        if args.embeddings:
            warm_embeddings(base)
    except (httpx.HTTPError, ValueError) as exc:
        fail(f"model download failed: {exc}")
        info(f"Retry later with: uv run python -m scripts.models  (URL: {SPACY_URL})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
