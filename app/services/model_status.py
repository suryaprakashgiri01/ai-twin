"""Whether each model in the inventory can actually answer here: pulled, keyed, still served.

- Ollama: the model must be pulled (`GET /api/tags` on the configured Ollama).
- Cloud: its provider key must be set; with `verify`, the provider's live model list must still
  carry the id (what `make models-check` checks). Keys go in request headers only, never logged.
- Disabled models (config or override) are reported as such and not probed.

`make models-check` uses the same endpoint table, so the console and the script agree.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Literal

import httpx
import structlog

from app.core.config import Settings
from llm_orchestrator import ModelSpec, Provider, ProviderSecrets
from llm_orchestrator.providers import is_available

logger = structlog.get_logger(__name__)

ModelStatus = Literal["active", "disabled", "no key", "not pulled", "unreachable", "missing"]
TIMEOUT = 4.0
OPENAI_BASE = "https://api.openai.com/v1"

# Provider -> (models URL, header builder, key setting name, id prefix to strip).
Endpoint = tuple[str, Callable[[str], dict[str, str]], str, str]
ENDPOINTS: dict[Provider, Endpoint] = {
    Provider.GROQ: (
        "https://api.groq.com/openai/v1/models",
        lambda k: {"Authorization": f"Bearer {k}"},
        "groq_api_key",
        "",
    ),
    Provider.CEREBRAS: (
        "https://api.cerebras.ai/v1/models",
        lambda k: {"Authorization": f"Bearer {k}"},
        "cerebras_api_key",
        "",
    ),
    Provider.OPENROUTER: (
        "https://openrouter.ai/api/v1/models",
        lambda k: {"Authorization": f"Bearer {k}"},
        "openrouter_api_key",
        "",
    ),
    Provider.GEMINI: (
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
        lambda k: {"x-goog-api-key": k},
        "gemini_api_key",
        "models/",
    ),
    Provider.ANTHROPIC: (
        "https://api.anthropic.com/v1/models?limit=1000",
        lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"},
        "anthropic_api_key",
        "",
    ),
    Provider.OPENAI: (
        f"{OPENAI_BASE}/models",
        lambda k: {"Authorization": f"Bearer {k}"},
        "openai_api_key",
        "",
    ),
}


def listed_ids(payload: Any, prefix: str) -> set[str]:
    """Model ids from an OpenAI-style `data`, Gemini `models` or Anthropic `data` listing."""
    if not isinstance(payload, dict):
        return set()
    rows = payload.get("data") or payload.get("models") or []
    ids = set()
    for row in rows:
        raw = str(row.get("id") or row.get("name") or "")
        ids.add(raw.removeprefix(prefix) if prefix else raw)
    return ids


def setting_key(settings: Settings, name: str) -> str:
    secret = getattr(settings, name, None)
    return secret.get_secret_value().strip() if secret is not None else ""


def models_url(spec: ModelSpec) -> str | None:
    endpoint = ENDPOINTS.get(spec.provider)
    if endpoint is None:
        return None
    if spec.provider is Provider.OPENAI and spec.base_url:
        return f"{spec.base_url.rstrip('/')}/models"
    return endpoint[0]


def ollama_name(spec: ModelSpec, secrets: ProviderSecrets) -> str:
    return secrets.ollama_models.get(spec.id) or spec.model


def pulled(name: str, tags: set[str]) -> bool:
    return name in tags or (":" not in name and f"{name}:latest" in tags)


class ModelStatusChecker:
    def __init__(
        self,
        settings: Settings,
        secrets: ProviderSecrets,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._transport = transport

    async def check(
        self,
        specs: Sequence[ModelSpec],
        enabled: Callable[[ModelSpec], bool],
        *,
        verify: bool = False,
    ) -> dict[str, tuple[ModelStatus, str | None]]:
        """model id -> (status, a hint to fix it)."""
        async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT) as client:
            tags = await self._ollama_tags(client, specs)
            listings = await self._listings(client, specs) if verify else {}
        out: dict[str, tuple[ModelStatus, str | None]] = {}
        for spec in specs:
            out[spec.id] = (
                ("disabled", None) if not enabled(spec) else self._one(spec, tags, listings, verify)
            )
        return out

    def _one(
        self,
        spec: ModelSpec,
        tags: set[str] | None,
        listings: dict[str, set[str] | None],
        verify: bool,
    ) -> tuple[ModelStatus, str | None]:
        if spec.provider is Provider.MOCK:
            return ("active", None) if self._secrets.mock_enabled else ("disabled", None)
        if spec.provider is Provider.OLLAMA:
            name = ollama_name(spec, self._secrets)
            if tags is None:
                return "unreachable", 'start Ollama (make ollama ARGS="status")'
            if not pulled(name, tags):
                return "not pulled", f'make ollama ARGS="pull {name}"'
            return "active", None
        if not is_available(spec, self._secrets):
            endpoint = ENDPOINTS.get(spec.provider)
            key = endpoint[2].upper() if endpoint else "the provider key"
            return "no key", f"set {key} in .env"
        if verify:
            ids = listings.get(models_url(spec) or "")
            if ids is None:
                return "unreachable", "network, VPN or the provider is down"
            if spec.model not in ids:
                return "missing", "the provider no longer serves this id: update config/models.yaml"
        return "active", None

    async def _ollama_tags(
        self, client: httpx.AsyncClient, specs: Sequence[ModelSpec]
    ) -> set[str] | None:
        base = self._secrets.ollama_base_url
        if not base or not any(s.provider is Provider.OLLAMA for s in specs):
            return None
        try:
            response = await client.get(f"{base.rstrip('/')}/api/tags")
            response.raise_for_status()
            return {str(m.get("name", "")) for m in response.json().get("models", [])}
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("ollama tags unavailable", error_type=type(exc).__name__)
            return None

    async def _listings(
        self, client: httpx.AsyncClient, specs: Sequence[ModelSpec]
    ) -> dict[str, set[str] | None]:
        wanted: dict[str, tuple[str, Endpoint]] = {}
        for spec in specs:
            url = models_url(spec)
            endpoint = ENDPOINTS.get(spec.provider)
            if url is None or endpoint is None or not is_available(spec, self._secrets):
                continue
            key = setting_key(self._settings, endpoint[2])
            if key:
                wanted[url] = (key, endpoint)

        async def fetch(url: str, key: str, endpoint: Endpoint) -> set[str] | None:
            try:
                response = await client.get(url, headers=endpoint[1](key))
                response.raise_for_status()
                return listed_ids(response.json(), endpoint[3])
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("model listing unavailable", url=url, error_type=type(exc).__name__)
                return None

        urls = list(wanted)
        found = await asyncio.gather(*(fetch(u, *wanted[u]) for u in urls))
        return dict(zip(urls, found, strict=True))
