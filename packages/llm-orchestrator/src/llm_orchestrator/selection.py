"""Turn (role, tier, override) into the ordered list of models to try."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from llm_orchestrator.gateway import Need, fit_order
from llm_orchestrator.inventory import EffectiveInventory
from llm_orchestrator.policy import Availability, eligible, rejection
from llm_orchestrator.types import (
    ModelSpec,
    NoEligibleModelError,
    OverrideRejectedError,
    Prefer,
    Role,
    Tier,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Selection:
    role: Role
    tier: Tier
    models: list[ModelSpec]
    # True when a privileged caller pinned one model; no fallback then.
    pinned: bool = False


def select(
    inventory: EffectiveInventory,
    role: Role,
    tier: Tier,
    available: Availability,
    *,
    model: str | None = None,
    prefer: Prefer | None = None,
    privileged: bool = False,
    need: Need | None = None,
) -> Selection:
    """Eligible models for this call, best first.

    Overrides (`model`, `prefer`) are honoured only for privileged callers; anyone else's are
    ignored and logged. A rejected `model` override raises and never falls back silently.
    Raises `NoEligibleModelError` when nothing may serve the call.
    """
    if (model is not None or prefer is not None) and not privileged:
        logger.warning(
            "override ignored for unprivileged caller",
            role=role.value,
            tier=tier.label,
            override_model=model,
            prefer=prefer,
        )
        model, prefer = None, None

    if model is not None:
        spec = inventory.models.get(model)
        reason = rejection(spec, role, tier, available)
        if reason is not None:
            raise OverrideRejectedError(model, reason)
        assert spec is not None  # rejection() reports a missing spec as UNKNOWN_MODEL
        return Selection(role=role, tier=tier, models=[spec], pinned=True)

    chain = [s for s in inventory.chain(role) if eligible(s, role, tier, available)]
    if need is not None and chain:
        # The gateway reorders what policy already allowed; it never adds a model.
        chain = fit_order(chain, need)
    if prefer is not None:
        # Stable sort: preferred models first, the (fit) order kept within each group.
        chain.sort(key=lambda s: prefer not in s.tags)
    if not chain:
        raise NoEligibleModelError(role, tier)
    if need is not None:
        logger.info(
            "gateway",
            role=role.value,
            tier=tier.label,
            need=need.summary(),
            need_source=need.source,
            prefer=prefer,
            first=[s.id for s in chain[:3]],
        )
    return Selection(role=role, tier=tier, models=chain)
