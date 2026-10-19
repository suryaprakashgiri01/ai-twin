"""Cloudflare Turnstile siteverify. Off (every submit passes) until TURNSTILE_SECRET_KEY is set.

The portfolio widget passes the token through with the visitor's submit. A missing token, a
rejected one, or a siteverify outage all refuse the submit: with the secret set, an unverified
submit never stores a lead.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


@dataclass(frozen=True)
class TurnstileResult:
    ok: bool
    # disabled | verified | missing | invalid | unavailable
    reason: str


class Turnstile:
    def __init__(
        self,
        secret: str,
        *,
        url: str = SITEVERIFY_URL,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._secret = secret
        self._url = url
        self._timeout = timeout
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self._secret)

    async def verify(self, token: str | None) -> TurnstileResult:
        if not self.enabled:
            return TurnstileResult(True, "disabled")
        if not token:
            return TurnstileResult(False, "missing")
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.post(
                    self._url, data={"secret": self._secret, "response": token}
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("turnstile unavailable", error_type=type(exc).__name__)
            return TurnstileResult(False, "unavailable")
        if body.get("success") is True:
            return TurnstileResult(True, "verified")
        codes = [str(c) for c in body.get("error-codes", [])][:5]
        logger.info("turnstile rejected", error_codes=codes)
        return TurnstileResult(False, "invalid")
