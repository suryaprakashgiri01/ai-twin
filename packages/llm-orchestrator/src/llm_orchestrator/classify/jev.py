"""TypeSafe Jev adapter: one `choice` question to `POST /v1/systemone`, over httpx.

Public tier only. 429/529 and 5xx are transient (honouring `retry-after`); 400/401/413/422 and
other 4xx are permanent. Neither the text nor the answer is logged.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
import structlog
from pydantic import SecretStr

from llm_orchestrator.classify.base import Classification, normalise
from llm_orchestrator.types import NoEligibleModelError, ProviderError, Role, Tier

logger = structlog.get_logger(__name__)

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MAX_TIER = Tier.PUBLIC
_QUESTION_KEY = "q"
_TRANSIENT_STATUSES = frozenset({408, 429, 529})
_MAX_RETRY_AFTER_SECONDS = 30.0


class JevError(ProviderError):
    """A Jev call failed; `transient` says whether retrying could help."""


def _retry_after(response: httpx.Response, fallback: float) -> float:
    try:
        return min(float(response.headers["retry-after"]), _MAX_RETRY_AFTER_SECONDS)
    except (KeyError, ValueError):
        return fallback


class JevClassifier:
    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str = "jev-1.13.0",
        url: str = JEV_URL,
        timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._url = url
        self._timeout = timeout_seconds
        self._max_attempts = max(1, max_attempts)
        self._client = client
        self._sleep = sleep

    @property
    def model_id(self) -> str:
        return f"jev:{self._model}"

    def request_body(self, text: str, question: str, labels: Mapping[str, str]) -> dict[str, Any]:
        return {
            "state": text,
            "model": self._model,
            "questions": {
                _QUESTION_KEY: {
                    "type": "choice",
                    "instructions": question,
                    "criteria": dict(labels),
                }
            },
        }

    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        if tier > JEV_MAX_TIER:
            raise NoEligibleModelError(Role.CLASSIFIER, tier)
        if not labels:
            raise ValueError("labels must not be empty")
        body = self.request_body(text, question, labels)
        if self._client is not None:
            payload = await self._post_with_retries(self._client, body)
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                payload = await self._post_with_retries(client, body)
        return self._map(payload, labels)

    async def _post_with_retries(
        self, client: httpx.AsyncClient, body: dict[str, Any]
    ) -> dict[str, Any]:
        for number in range(1, self._max_attempts + 1):
            try:
                return await self._post_once(client, body, number)
            except JevError as exc:
                if not exc.transient or number == self._max_attempts:
                    raise
                await self._sleep(float(exc.extra.get("retry_after", 0.5 * 2**number)))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _post_once(
        self, client: httpx.AsyncClient, body: dict[str, Any], number: int
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        started = time.perf_counter()
        try:
            response = await client.post(self._url, json=body, headers=headers)
        except httpx.TransportError as exc:
            self._log(number, started, "transient_error", type(exc).__name__, None)
            raise JevError("jev transport error", transient=True) from exc
        status = response.status_code
        if status >= 400:
            transient = status in _TRANSIENT_STATUSES or status >= 500
            outcome = "transient_error" if transient else "permanent_error"
            self._log(number, started, outcome, "HTTPStatusError", status)
            raise JevError(
                f"jev returned {status}",
                transient=transient,
                status_code=status,
                retry_after=_retry_after(response, 0.5 * 2**number),
            )
        self._log(number, started, "ok", None, status)
        try:
            payload = response.json()
        except ValueError as exc:
            raise JevError("jev returned invalid JSON", transient=False) from exc
        if not isinstance(payload, dict):
            raise JevError("jev returned a non-object body", transient=False)
        return payload

    def _map(self, payload: Mapping[str, Any], labels: Mapping[str, str]) -> Classification:
        try:
            answer = payload["answers"][_QUESTION_KEY]
            choice = answer["choice"]
            probabilities = answer.get("probabilities") or {choice: 1.0}
            confidence = float(answer["confidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JevError("jev answer missing fields", transient=False) from exc
        if choice not in labels:
            raise JevError("jev chose an unknown label", transient=False)
        return Classification(
            label=choice,
            probabilities=normalise(probabilities, labels),
            confidence=min(max(confidence, 0.0), 1.0),
            model=f"jev:{payload.get('model', self._model)}",
        )

    def _log(
        self, number: int, started: float, outcome: str, error_type: str | None, status: int | None
    ) -> None:
        method = logger.info if outcome == "ok" else logger.warning
        method(
            "llm attempt",
            role=Role.CLASSIFIER.value,
            tier=JEV_MAX_TIER.label,
            model=self.model_id,
            provider="jev",
            attempt=number,
            outcome=outcome,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error_type=error_type,
            status_code=status,
        )
