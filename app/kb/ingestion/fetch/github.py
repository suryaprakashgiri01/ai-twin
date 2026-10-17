"""Sync a personal GitHub repository: description, topics, languages, last push and README.

Only repositories named in `GITHUB_SYNC_REPOS` are fetched; the check happens before any
request, so a name outside the allowlist never leaves the machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.kb.ingestion.models import IngestError, IngestErrorCode, Parsed

logger = structlog.get_logger(__name__)

API = "https://api.github.com"


def normalise_repo(name: str) -> str:
    return name.strip().strip("/").lower()


def check_allowed(repo: str, allowlist: frozenset[str]) -> str:
    """The normalised `owner/name`; raises NOT_ALLOWED for anything off the allowlist."""
    key = normalise_repo(repo)
    if key.count("/") != 1 or key not in allowlist:
        raise IngestError(
            IngestErrorCode.NOT_ALLOWED, f"Repository {repo!r} is not in GITHUB_SYNC_REPOS"
        )
    return key


def parse_allowlist(raw: str) -> frozenset[str]:
    return frozenset(normalise_repo(r) for r in raw.split(",") if r.strip())


@dataclass
class GitHubFetcher:
    allowlist: frozenset[str]
    token: str | None = None
    timeout_seconds: float = 15.0
    transport: httpx.AsyncBaseTransport | None = None

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.AsyncClient(
            base_url=API,
            headers=headers,
            timeout=self.timeout_seconds,
            transport=self.transport,
            trust_env=False,
        )

    async def fetch(self, repo: str) -> Parsed:
        key = check_allowed(repo, self.allowlist)
        async with self._client() as client:
            meta = await self._json(client, f"/repos/{key}")
            languages = await self._json(client, f"/repos/{key}/languages")
            readme = await self._readme(client, key)
        return Parsed(
            markdown=render(meta, languages, readme),
            detected="github",
            title=str(meta.get("full_name") or key),
            category="project",
            key=key,
        )

    async def _json(self, client: httpx.AsyncClient, path: str) -> dict[str, Any]:
        try:
            response = await client.get(path)
        except httpx.HTTPError as exc:
            raise IngestError(IngestErrorCode.FETCH_FAILED, "GitHub could not be reached") from exc
        if response.status_code != 200:
            raise IngestError(
                IngestErrorCode.FETCH_FAILED, f"GitHub answered {response.status_code}"
            )
        body = response.json()
        return body if isinstance(body, dict) else {}

    async def _readme(self, client: httpx.AsyncClient, key: str) -> str:
        try:
            response = await client.get(
                f"/repos/{key}/readme", headers={"Accept": "application/vnd.github.raw+json"}
            )
        except httpx.HTTPError as exc:
            raise IngestError(IngestErrorCode.FETCH_FAILED, "GitHub could not be reached") from exc
        return response.text if response.status_code == 200 else ""


def render(meta: dict[str, Any], languages: dict[str, Any], readme: str) -> str:
    name = str(meta.get("full_name") or meta.get("name") or "repository")
    lines = [f"# {name}", ""]
    if meta.get("description"):
        lines += [str(meta["description"]), ""]
    topics = [str(t) for t in meta.get("topics") or []]
    facts = [
        ("Topics", ", ".join(topics)),
        ("Languages", ", ".join(str(k) for k in languages)),
        ("Last push", str(meta.get("pushed_at") or "")),
        ("URL", str(meta.get("html_url") or "")),
    ]
    lines += [f"- {label}: {value}" for label, value in facts if value]
    if readme.strip():
        lines += ["", "## README", "", readme.strip()]
    return "\n".join(lines) + "\n"
