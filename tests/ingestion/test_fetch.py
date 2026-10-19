"""Ingestion T4: SSRF-safe URL fetch, Google export rewrite, GitHub allowlist, LinkedIn ZIP."""

from __future__ import annotations

import io
import ipaddress
import zipfile
from typing import Any

import httpx
import pytest

from app.kb.ingestion.fetch import github, linkedin
from app.kb.ingestion.fetch.url import (
    MAX_REDIRECTS,
    PinnedBackend,
    PinnedTransport,
    UrlFetcher,
    is_public_address,
    rewrite_google,
)
from app.kb.ingestion.models import IngestError, IngestErrorCode
from tests.ingestion import persona

DNS = {
    "public.example": ["93.184.215.14"],
    "v6.example": ["2606:2800:220:1::1"],
    "internal.example": ["10.0.0.5"],
    "loop.example": ["127.0.0.1"],
    "metadata.example": ["169.254.169.254"],
    "mixed.example": ["93.184.215.14", "192.168.1.10"],
    "mapped.example": ["::ffff:127.0.0.1"],
}


async def resolver(host: str, port: int) -> list[str]:
    if host in DNS:
        return DNS[host]
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        raise OSError("nxdomain") from None


def fetcher(handler: Any, max_bytes: int = 1024) -> UrlFetcher:
    return UrlFetcher(
        max_bytes=max_bytes, resolver=resolver, transport=httpx.MockTransport(handler)
    )


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        text="<html><body>hello</body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )


async def test_public_url_is_fetched() -> None:
    result = await fetcher(ok_handler).fetch("https://public.example/page")
    assert result.url == "https://public.example/page"
    assert result.content_type == "text/html"
    assert b"hello" in result.data


@pytest.mark.parametrize(
    "url",
    ["ftp://public.example/x", "file:///etc/passwd", "javascript:alert(1)", "gopher://x", "//x"],
)
async def test_non_http_schemes_are_blocked(url: str) -> None:
    with pytest.raises(IngestError) as caught:
        await fetcher(ok_handler).fetch(url)
    assert caught.value.code == IngestErrorCode.FETCH_BLOCKED


@pytest.mark.parametrize(
    "url",
    [
        "http://internal.example/",
        "http://loop.example/",
        "http://metadata.example/latest/meta-data/",
        "http://mixed.example/",
        "http://mapped.example/",
        "http://127.0.0.1:8000/admin",
        "http://[::1]/",
        "http://0.0.0.0/",
    ],
)
async def test_private_addresses_are_blocked(url: str) -> None:
    with pytest.raises(IngestError) as caught:
        await fetcher(ok_handler).fetch(url)
    assert caught.value.code == IngestErrorCode.FETCH_BLOCKED


def redirecting(chain: list[str]) -> Any:
    hops = iter(chain)

    def handler(request: httpx.Request) -> httpx.Response:
        target = next(hops, None)
        if target is None:
            return ok_handler(request)
        return httpx.Response(302, headers={"location": target})

    return handler


@pytest.mark.parametrize(
    "target", ["http://internal.example/secret", "http://169.254.169.254/", "file:///etc/passwd"]
)
async def test_redirects_are_checked_on_every_hop(target: str) -> None:
    handler = redirecting(["/step", target])
    with pytest.raises(IngestError) as caught:
        await fetcher(handler).fetch("https://public.example/start")
    assert caught.value.code == IngestErrorCode.FETCH_BLOCKED


async def test_redirect_limit() -> None:
    allowed = [f"https://public.example/{i}" for i in range(MAX_REDIRECTS)]
    result = await fetcher(redirecting(allowed)).fetch("https://public.example/start")
    assert result.url.endswith(f"/{MAX_REDIRECTS - 1}")
    too_many = [f"https://public.example/{i}" for i in range(MAX_REDIRECTS + 1)]
    with pytest.raises(IngestError) as caught:
        await fetcher(redirecting(too_many)).fetch("https://public.example/start")
    assert caught.value.code == IngestErrorCode.FETCH_BLOCKED


async def test_oversize_bodies_are_refused() -> None:
    def declared(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 10, headers={"content-length": "999999"})

    def streamed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    for handler in (declared, streamed):
        with pytest.raises(IngestError) as caught:
            await fetcher(handler).fetch("https://public.example/big")
        assert caught.value.code == IngestErrorCode.TOO_LARGE


async def test_http_errors_and_unresolvable_hosts_fail() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("reset")

    def bare_redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    for handler in (missing, broken, bare_redirect):
        with pytest.raises(IngestError) as caught:
            await fetcher(handler).fetch("https://public.example/x")
        assert caught.value.code == IngestErrorCode.FETCH_FAILED
    with pytest.raises(IngestError) as unknown:
        await fetcher(ok_handler).fetch("https://nowhere.invalid/")
    assert unknown.value.code == IngestErrorCode.FETCH_FAILED


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://docs.google.com/document/d/AbC_123-x/edit?usp=sharing",
            "https://docs.google.com/document/d/AbC_123-x/export?format=docx",
        ),
        (
            "https://docs.google.com/spreadsheets/d/Sheet1/edit#gid=42",
            "https://docs.google.com/spreadsheets/d/Sheet1/export?format=csv&gid=42",
        ),
        (
            "https://docs.google.com/presentation/d/Deck9/edit",
            "https://docs.google.com/presentation/d/Deck9/export/pptx",
        ),
        ("https://public.example/doc", "https://public.example/doc"),
    ],
)
def test_google_links_are_rewritten_to_export(url: str, expected: str) -> None:
    assert rewrite_google(url) == expected


def test_address_classification() -> None:
    assert is_public_address("93.184.215.14")
    assert is_public_address("2606:2800:220:1::1")
    for address in (
        "10.1.2.3",
        "172.16.0.1",
        "192.168.0.1",
        "100.64.0.1",
        "fe80::1%en0",
        "fd00::1",
    ):
        assert not is_public_address(address)


class FakeInner:
    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host: str, port: int, *args: Any) -> Any:
        self.connected.append((host, port))
        return "stream"


async def test_pinned_backend_connects_to_the_checked_address() -> None:
    backend = PinnedBackend(resolver)
    inner = FakeInner()
    backend._inner = inner  # type: ignore[assignment]
    stream: object = await backend.connect_tcp("public.example", 443)
    assert stream == "stream"
    assert inner.connected == [("93.184.215.14", 443)]
    with pytest.raises(IngestError):
        await backend.connect_tcp("internal.example", 80)
    assert len(inner.connected) == 1
    assert isinstance(PinnedTransport(resolver), httpx.AsyncHTTPTransport)


# --- GitHub -----------------------------------------------------------------------------

ALLOW = github.parse_allowlist(" kavya-testpal/rate-limiter , Kavya-Testpal/Notes ")


def github_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/languages"):
        return httpx.Response(200, json={"Python": 1200, "Go": 300})
    if path.endswith("/readme"):
        return httpx.Response(200, text="# Rate limiter\n\nToken bucket in Go.")
    return httpx.Response(
        200,
        json={
            "full_name": "kavya-testpal/rate-limiter",
            "description": "A token bucket rate limiter",
            "topics": ["rate-limiting", "go"],
            "pushed_at": "2026-09-01T10:00:00Z",
            "html_url": "https://github.com/kavya-testpal/rate-limiter",
        },
    )


async def test_github_sync_reads_an_allowlisted_repo() -> None:
    fetcher = github.GitHubFetcher(ALLOW, token="t", transport=httpx.MockTransport(github_handler))
    parsed = await fetcher.fetch("Kavya-Testpal/Rate-Limiter")
    assert parsed.category == "project"
    assert parsed.key == "kavya-testpal/rate-limiter"
    for needle in ("token bucket", "Topics: rate-limiting, go", "Python, Go", "## README"):
        assert needle in parsed.markdown


@pytest.mark.parametrize("repo", ["someone-else/rate-limiter", "kavya-testpal", "a/b/c", ""])
async def test_github_refuses_repos_outside_the_allowlist(repo: str) -> None:
    calls: list[httpx.Request] = []

    def spy(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    fetcher = github.GitHubFetcher(ALLOW, transport=httpx.MockTransport(spy))
    with pytest.raises(IngestError) as caught:
        await fetcher.fetch(repo)
    assert caught.value.code == IngestErrorCode.NOT_ALLOWED
    assert calls == []


async def test_github_errors_and_missing_readme() -> None:
    def no_readme(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/readme"):
            return httpx.Response(404)
        return github_handler(request)

    fetcher = github.GitHubFetcher(ALLOW, transport=httpx.MockTransport(no_readme))
    assert "## README" not in (await fetcher.fetch("kavya-testpal/notes")).markdown

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(IngestError) as caught:
        await github.GitHubFetcher(ALLOW, transport=httpx.MockTransport(down)).fetch(
            "kavya-testpal/notes"
        )
    assert caught.value.code == IngestErrorCode.FETCH_FAILED

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(IngestError):
        await github.GitHubFetcher(ALLOW, transport=httpx.MockTransport(unreachable)).fetch(
            "kavya-testpal/notes"
        )


async def test_github_sync_of_a_named_branch() -> None:
    seen: list[httpx.Request] = []

    def branchy(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/branches/develop"):
            commit = {"commit": {"committer": {"date": "2026-10-19T07:43:38Z"}}}
            return httpx.Response(200, json={"name": "develop", "commit": commit})
        if "/branches/" in request.url.path:
            return httpx.Response(404)
        return github_handler(request)

    fetcher = github.GitHubFetcher(ALLOW, transport=httpx.MockTransport(branchy))
    parsed = await fetcher.fetch("Kavya-Testpal/Rate-Limiter@develop")
    assert parsed.key == "kavya-testpal/rate-limiter@develop"
    assert parsed.title == "kavya-testpal/rate-limiter (develop branch)"
    assert "Last commit on this branch: 2026-10-19T07:43:38Z" in parsed.markdown
    assert "rate-limiter/tree/develop" in parsed.markdown
    readme = next(r for r in seen if r.url.path.endswith("/readme"))
    assert readme.url.params["ref"] == "develop"
    with pytest.raises(IngestError) as caught:
        await fetcher.fetch("kavya-testpal/rate-limiter@nope")
    assert "No branch 'nope'" in caught.value.message


def test_branch_names_and_allowlist_defaults() -> None:
    assert github.check_allowed("kavya-testpal/notes@Feature/X-1", ALLOW) == (
        "kavya-testpal/notes@Feature/X-1"
    )
    for bad in ("kavya-testpal/notes@../main", "kavya-testpal/notes@-x", "kavya-testpal/notes@a b"):
        with pytest.raises(IngestError):
            github.check_allowed(bad, ALLOW)
    raw = "kavya-testpal/rate-limiter@develop, kavya-testpal/notes"
    assert github.parse_allowlist(raw) == ALLOW
    assert github.default_branches(raw) == {"kavya-testpal/rate-limiter": "develop"}


# --- LinkedIn ---------------------------------------------------------------------------


def linkedin_zip(extra: dict[str, str] | None = None) -> bytes:
    files = {
        "Profile.csv": (
            "First Name,Last Name,Headline,Summary,Industry\n"
            f"Kavya,Testpal,Backend engineer,Builds APIs at {persona.COMPANY},Software\n"
        ),
        "Positions.csv": (
            "Company Name,Title,Description,Location,Started On,Finished On\n"
            "Acme Widgets,Backend Engineer,Owned the billing API,Remote,Jan 2022,\n"
            "Example Labs,Intern,Wrote tests,Pune,Jun 2020,Dec 2020\n"
        ),
        "Education.csv": "School Name,Start Date,End Date,Notes,Degree Name,Activities\n"
        "Example Institute,2014,2018,,B.Tech,Robotics club\n",
        "Skills.csv": "Name\nPython\nGo\n",
        "Certifications.csv": "Name,Url,Authority,Started On,Finished On\n"
        "Cloud Basics,https://cert.example/1,Example Cloud,Mar 2023,\n",
        "Projects.csv": "Title,Description,Url,Started On,Finished On\n"
        "Rate limiter,Token bucket,https://github.com/kavya-testpal/rate-limiter,2024,\n",
        "Connections.csv": 'Notes:\n"Connections exported"\n\nFirst Name,Last Name\nA,B\n',
        "messages.csv": "FROM,TO,CONTENT\nx,y,private chat\n",
        "Rich_Media.csv": "Date,Media\n",
        **(extra or {}),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
        archive.writestr("media/", "")
    return buffer.getvalue()


def test_linkedin_export_yields_one_document_per_entity() -> None:
    docs = linkedin.parse_export(linkedin_zip())
    by_key = {d.key: d for d in docs}
    assert set(by_key) == {
        "profile",
        "position:0",
        "position:1",
        "education:0",
        "skills",
        "certification:0",
        "project:0",
    }
    assert by_key["position:0"].category == "experience"
    assert by_key["position:0"].title == "Backend Engineer at Acme Widgets"
    assert "Jan 2022 - present" in by_key["position:0"].markdown
    assert "- Go" in by_key["skills"].markdown
    assert all("private chat" not in d.markdown for d in docs)


def test_linkedin_rejects_non_zip_bombs_and_empty_exports() -> None:
    with pytest.raises(IngestError) as not_zip:
        linkedin.parse_export(b"not a zip")
    assert not_zip.value.code == IngestErrorCode.UNSUPPORTED_TYPE

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("messages.csv", "FROM,TO\n")
    with pytest.raises(IngestError) as empty:
        linkedin.parse_export(buffer.getvalue())
    assert empty.value.code == IngestErrorCode.EMPTY_CONTENT


def test_linkedin_refuses_huge_archives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linkedin, "MAX_UNCOMPRESSED", 10)
    with pytest.raises(IngestError) as caught:
        linkedin.parse_export(linkedin_zip())
    assert caught.value.code == IngestErrorCode.TOO_LARGE
