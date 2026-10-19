"""SSRF-safe fetch of an owner-supplied URL.

Rules, checked on the first request and again on every redirect hop:
- only `http` and `https`;
- every address the host resolves to must be public: private, loopback, link-local, multicast,
  reserved and unspecified ranges are refused (IPv4-mapped IPv6 included);
- at most `MAX_REDIRECTS` redirects;
- the body is streamed and cut off at `max_bytes`.

The connection itself goes through `PinnedBackend`, which resolves, checks and then connects to
the checked address, so a DNS answer that changes between the check and the connect (rebinding)
cannot reach an internal address. TLS still verifies the original host name.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
import structlog

from app.kb.ingestion.models import IngestError, IngestErrorCode

logger = structlog.get_logger(__name__)

MAX_REDIRECTS = 5
ALLOWED_SCHEMES = frozenset({"http", "https"})
USER_AGENT = "ai-twin-ingest/1.0 (+personal knowledge base)"

Resolver = Callable[[str, int], Awaitable[list[str]]]

_GOOGLE = re.compile(
    r"^https://docs\.google\.com/(?P<kind>document|spreadsheets|presentation)/d/(?P<id>[\w-]+)"
)
_GOOGLE_EXPORT = {
    "document": "export?format=docx",
    "spreadsheets": "export?format=csv",
    "presentation": "export/pptx",
}


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return sorted({str(info[4][0]) for info in infos})


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _blocked(message: str) -> IngestError:
    return IngestError(IngestErrorCode.FETCH_BLOCKED, message)


def rewrite_google(url: str) -> str:
    """A public Google Docs/Sheets/Slides link becomes its export URL; anything else is kept."""
    match = _GOOGLE.match(url)
    if match is None:
        return url
    base = f"https://docs.google.com/{match['kind']}/d/{match['id']}/"
    export = _GOOGLE_EXPORT[match["kind"]]
    gid = re.search(r"[#?&]gid=(\d+)", url)
    if match["kind"] == "spreadsheets" and gid:
        export += f"&gid={gid.group(1)}"
    return base + export


def check_scheme(url: str) -> tuple[str, int]:
    """The host and port of an allowed URL; raises FETCH_BLOCKED otherwise."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise _blocked(f"Only http and https URLs are allowed (got {parts.scheme or 'none'})")
    if not parts.hostname:
        raise _blocked("The URL has no host")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise _blocked("The URL has an invalid port") from exc
    return parts.hostname, port


async def check_host(host: str, port: int, resolver: Resolver) -> list[str]:
    """Resolve and return the host's addresses, refusing any that are not public."""
    try:
        addresses = await resolver(host, port)
    except OSError as exc:
        raise IngestError(IngestErrorCode.FETCH_FAILED, "The host name does not resolve") from exc
    if not addresses:
        raise IngestError(IngestErrorCode.FETCH_FAILED, "The host name does not resolve")
    for address in addresses:
        if not is_public_address(address):
            raise _blocked("The URL points to a private, loopback or link-local address")
    return addresses


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Connects only to a checked public address of the requested host."""

    def __init__(self, resolver: Resolver = system_resolver) -> None:
        self._resolver = resolver
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await check_host(host, port, self._resolver)
        return await self._inner.connect_tcp(
            addresses[0], port, timeout, local_address, socket_options
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:  # pragma: no cover - never configured
        raise _blocked("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - retries are off
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, resolver: Resolver = system_resolver) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=PinnedBackend(resolver),
        )


@dataclass(frozen=True)
class Fetched:
    url: str
    content_type: str
    data: bytes


@dataclass
class UrlFetcher:
    max_bytes: int
    timeout_seconds: float = 15.0
    resolver: Resolver = system_resolver
    # Tests pass an `httpx.MockTransport`; production uses the pinned transport.
    transport: httpx.AsyncBaseTransport | None = None

    async def fetch(self, url: str) -> Fetched:
        current = rewrite_google(url.strip())
        transport = self.transport or PinnedTransport(self.resolver)
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=self.timeout_seconds,
            headers={"User-Agent": USER_AGENT},
            trust_env=False,
        ) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                host, port = check_scheme(current)
                await check_host(host, port, self.resolver)
                result = await self._get(client, current)
                if isinstance(result, Fetched):
                    return result
                current = result
        raise _blocked(f"More than {MAX_REDIRECTS} redirects")

    async def _get(self, client: httpx.AsyncClient, url: str) -> Fetched | str:
        """The body, or the next URL when the response is a redirect."""
        try:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    location: str | None = response.headers.get("location")
                    if not location:
                        raise IngestError(IngestErrorCode.FETCH_FAILED, "Redirect without target")
                    return str(urljoin(url, location))
                if response.status_code >= 400:
                    raise IngestError(
                        IngestErrorCode.FETCH_FAILED, f"The server answered {response.status_code}"
                    )
                data = await self._read_capped(response)
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                return Fetched(url=url, content_type=content_type.lower(), data=data)
        except httpx.HTTPError as exc:
            logger.warning("url fetch failed", error_type=type(exc).__name__)
            raise IngestError(IngestErrorCode.FETCH_FAILED, "The URL could not be fetched") from exc

    async def _read_capped(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            raise IngestError(IngestErrorCode.TOO_LARGE, "The response is over the size limit")
        body = bytearray()
        async for piece in response.aiter_bytes():
            body += piece
            if len(body) > self.max_bytes:
                raise IngestError(IngestErrorCode.TOO_LARGE, "The response is over the size limit")
        return bytes(body)
