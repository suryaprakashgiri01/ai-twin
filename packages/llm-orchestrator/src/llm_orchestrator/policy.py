"""Who may serve what. A model serves a request only if `request.tier <= model.max_tier`."""

from __future__ import annotations

from collections.abc import Callable

from llm_orchestrator.types import ModelSpec, Rejection, Role, Tier

# Whether a model's provider is configured (key or endpoint present). Missing = disabled.
Availability = Callable[[ModelSpec], bool]


def allows_tier(spec: ModelSpec, tier: Tier) -> bool:
    # Restricted text never reaches any model, whatever a spec claims.
    return tier < Tier.RESTRICTED and tier <= spec.max_tier


def rejection(
    spec: ModelSpec | None, role: Role, tier: Tier, available: Availability
) -> Rejection | None:
    """The first reason `spec` may not serve `role` at `tier`, or None when it may."""
    if spec is None:
        return Rejection.UNKNOWN_MODEL
    if not spec.enabled:
        return Rejection.DISABLED
    if not available(spec):
        return Rejection.NOT_CONFIGURED
    if not spec.serves(role):
        return Rejection.WRONG_ROLE
    if not allows_tier(spec, tier):
        return Rejection.UNDER_TIER
    return None


def eligible(spec: ModelSpec, role: Role, tier: Tier, available: Availability) -> bool:
    return rejection(spec, role, tier, available) is None
