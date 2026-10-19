"""Sync a personal GitHub repository: description, topics, languages, last push and README.

Only repositories named in `GITHUB_SYNC_REPOS` are fetched; the check happens before any
request, so a name outside the allowlist never leaves the machine. `owner/name@branch` syncs
that branch's README and last commit instead of the default branch's; an allowlist entry with
`@branch` makes that branch the one synced when a request names none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.kb.ingestion.models import IngestError, IngestErrorCode, Parsed

logger = structlog.get_logger(__name__)

API = "https://api.github.com"


# Git branch names: no spaces, no "..", no leading "-" or "/".
_BRANCH = re.compile(r"^(?!.*\.\.)[A-Za-z0-9._][A-Za-z0-9._/-]{0,99}$")


def split_repo(spec: str) -> tuple[str, str | None]:
    """`owner/name[@branch]` → (normalised `owner/name`, branch or None). Branches keep case."""
    name, _, branch = spec.strip().partition("@")
    return name.strip().strip("/").lower(), (branch.strip() or None)


def normalise_repo(name: str) -> str:
    return split_repo(name)[0]


def check_allowed(repo: str, allowlist: frozenset[str]) -> str:
    """The normalised `owner/name[@branch]`; raises NOT_ALLOWED for anything off the allowlist
    (any branch of an allowed repository is allowed) and INVALID for a malformed branch."""
    key, branch = split_repo(repo)
    if key.count("/") != 1 or key not in allowlist:
        raise IngestError(
            IngestErrorCode.NOT_ALLOWED, f"Repository {repo!r} is not in GITHUB_SYNC_REPOS"
        )
    if branch is None:
        return key
    if not _BRANCH.match(branch):
        raise IngestError(IngestErrorCode.NOT_ALLOWED, f"{branch!r} is not a branch name")
    return f"{key}@{branch}"


def parse_allowlist(raw: str) -> frozenset[str]:
    return frozenset(normalise_repo(r) for r in raw.split(",") if r.strip())


def default_branches(raw: str) -> dict[str, str]:
    """`owner/name@branch` allowlist entries: the branch synced when a request names none."""
    found = (split_repo(r) for r in raw.split(",") if r.strip())
    return {key: branch for key, branch in found if branch}


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
        key, branch = split_repo(check_allowed(repo, self.allowlist))
        async with self._client() as client:
            meta = await self._json(client, f"/repos/{key}")
            languages = await self._json(client, f"/repos/{key}/languages")
            head = await self._branch(client, key, branch) if branch else None
            readme = await self._readme(client, key, branch)
        title = str(meta.get("full_name") or key)
        return Parsed(
            markdown=render(meta, languages, readme, branch=branch, head=head),
            detected="github",
            title=f"{title} ({branch} branch)" if branch else title,
            category="project",
            key=f"{key}@{branch}" if branch else key,
        )

    async def _branch(self, client: httpx.AsyncClient, key: str, branch: str) -> dict[str, Any]:
        try:
            response = await client.get(f"/repos/{key}/branches/{branch}")
        except httpx.HTTPError as exc:
            raise IngestError(IngestErrorCode.FETCH_FAILED, "GitHub could not be reached") from exc
        if response.status_code == 404:
            raise IngestError(IngestErrorCode.FETCH_FAILED, f"No branch {branch!r} in {key}")
        if response.status_code != 200:
            raise IngestError(
                IngestErrorCode.FETCH_FAILED, f"GitHub answered {response.status_code}"
            )
        body = response.json()
        return body if isinstance(body, dict) else {}

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

    async def _readme(self, client: httpx.AsyncClient, key: str, branch: str | None) -> str:
        try:
            response = await client.get(
                f"/repos/{key}/readme",
                params={"ref": branch} if branch else None,
                headers={"Accept": "application/vnd.github.raw+json"},
            )
        except httpx.HTTPError as exc:
            raise IngestError(IngestErrorCode.FETCH_FAILED, "GitHub could not be reached") from exc
        return response.text if response.status_code == 200 else ""


def _commit_date(head: dict[str, Any] | None) -> str:
    commit = ((head or {}).get("commit") or {}).get("commit") or {}
    return str((commit.get("committer") or {}).get("date") or "")


def render(
    meta: dict[str, Any],
    languages: dict[str, Any],
    readme: str,
    *,
    branch: str | None = None,
    head: dict[str, Any] | None = None,
) -> str:
    name = str(meta.get("full_name") or meta.get("name") or "repository")
    lines = [f"# {name}" + (f" ({branch} branch)" if branch else ""), ""]
    if meta.get("description"):
        lines += [str(meta["description"]), ""]
    topics = [str(t) for t in meta.get("topics") or []]
    url = str(meta.get("html_url") or "")
    facts = [
        ("Topics", ", ".join(topics)),
        ("Languages", ", ".join(str(k) for k in languages)),
        ("Branch", branch or ""),
        ("Last commit on this branch", _commit_date(head)),
        ("Last push", str(meta.get("pushed_at") or "")),
        ("URL", f"{url}/tree/{branch}" if url and branch else url),
    ]
    lines += [f"- {label}: {value}" for label, value in facts if value]
    if readme.strip():
        lines += ["", "## README", "", readme.strip()]
    return "\n".join(lines) + "\n"
