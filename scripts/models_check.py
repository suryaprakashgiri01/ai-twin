"""Check every enabled cloud model id against the provider's live list: `make models-check`.

Providers retire model ids. A retired id fails with 404 on every request until
`config/models.yaml` is updated, so this lists, per model: `ok`, `missing` (the provider no
longer serves the id: update it), `no key` (skipped) or `unreachable` (network, VPN or the
provider is down). Exits 1 when an id is missing. Keys go in request headers only and are never
printed. Ollama models are checked by `make ollama ARGS="status"` instead.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from app.core.config import DEFAULT_MODEL_INVENTORY, Settings
from app.services.model_status import ENDPOINTS, listed_ids, models_url, setting_key
from llm_orchestrator import Inventory, ModelSpec, Provider
from scripts._common import fail, info, ok, warn

Status = Literal["ok", "missing", "no key", "unreachable"]
TIMEOUT = 8.0


@dataclass(frozen=True)
class Check:
    model_id: str
    provider: str
    provider_model: str
    status: Status


def check(specs: list[ModelSpec], settings: Settings, client: httpx.Client) -> list[Check]:
    cache: dict[tuple[Provider, str], set[str] | None] = {}
    results: list[Check] = []
    for spec in specs:
        endpoint = ENDPOINTS.get(spec.provider)
        if endpoint is None or not spec.enabled:
            continue
        _, headers, key_name, prefix = endpoint
        url = models_url(spec) or endpoint[0]
        key = setting_key(settings, key_name)
        status: Status
        if not key:
            status = "no key"
        else:
            if (spec.provider, url) not in cache:
                try:
                    response = client.get(url, headers=headers(key), timeout=TIMEOUT)
                    response.raise_for_status()
                    cache[(spec.provider, url)] = listed_ids(response.json(), prefix)
                except (httpx.HTTPError, ValueError):
                    cache[(spec.provider, url)] = None
            ids = cache[(spec.provider, url)]
            status = "unreachable" if ids is None else "ok" if spec.model in ids else "missing"
        results.append(Check(spec.id, spec.provider.value, spec.model, status))
    return results


def report(results: list[Check]) -> int:
    printer = {"ok": ok, "missing": fail, "no key": info, "unreachable": warn}
    for r in results:
        printer[r.status](f"{r.model_id} ({r.provider}: {r.provider_model}): {r.status}")
    missing = [r for r in results if r.status == "missing"]
    if missing:
        fail(
            f"{len(missing)} model id(s) no longer served; update them in config/models.yaml "
            "(or disable the entries)"
        )
    return 1 if missing else 0


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    inventory_path: Path = DEFAULT_MODEL_INVENTORY,
) -> int:
    settings = Settings()
    specs = list(Inventory.from_yaml(inventory_path).models.values())
    with httpx.Client(transport=transport) as client:
        return report(check(specs, settings, client))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
