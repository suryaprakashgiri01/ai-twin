"""AC2 (tier filter, no call when nothing is eligible) and AC4 (privileged overrides)."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from structlog.testing import capture_logs

from llm_orchestrator import (
    InMemoryOverrideStore,
    Inventory,
    ModelOverride,
    NoEligibleModelError,
    OverrideRejectedError,
    Rejection,
    Role,
    Tier,
)
from llm_orchestrator.policy import allows_tier
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec

MESSAGES = [HumanMessage(content="hello")]


def _three() -> tuple[Factory, Inventory]:
    inv = inventory(
        spec("cloud-pub", max_tier="public", tags=["fast"]),
        spec("cloud-priv", max_tier="private", tags=["quality"]),
        spec("local", max_tier="confidential", tags=["local"]),
    )
    return Factory({"cloud-pub": ["a"], "cloud-priv": ["b"], "local": ["c"]}), inv


async def test_confidential_only_reaches_confidential_models() -> None:
    factory, inv = _three()
    orch = orchestrator(inv, factory)
    selection = await orch.select(Role.GENERATOR, Tier.CONFIDENTIAL)
    assert [m.id for m in selection.models] == ["local"]
    result = await orch.chat(Role.GENERATOR, Tier.CONFIDENTIAL, MESSAGES)
    assert result.model_id == "local"
    assert factory.built == ["local"]


async def test_confidential_with_no_eligible_model_makes_no_call() -> None:
    inv = inventory(spec("cloud", max_tier="private"))
    factory = Factory({"cloud": ["x"]})
    orch = orchestrator(inv, factory)
    with pytest.raises(NoEligibleModelError) as info:
        await orch.chat(Role.GENERATOR, Tier.CONFIDENTIAL, MESSAGES)
    assert info.value.tier is Tier.CONFIDENTIAL
    assert factory.built == []
    assert factory.calls("cloud") == 0


async def test_restricted_never_reaches_any_model() -> None:
    factory, inv = _three()
    with pytest.raises(NoEligibleModelError):
        await orchestrator(inv, factory).chat(Role.ROUTER, Tier.RESTRICTED, MESSAGES)
    assert factory.built == []


def test_allows_tier_is_a_ceiling() -> None:
    _, inv = _three()
    local = inv.models["local"]
    assert allows_tier(local, Tier.PUBLIC)
    assert allows_tier(local, Tier.CONFIDENTIAL)
    assert not allows_tier(local, Tier.RESTRICTED)


async def test_public_keeps_chain_order_and_prefer_reorders() -> None:
    factory, inv = _three()
    orch = orchestrator(inv, factory)
    default = await orch.select(Role.ROUTER, Tier.PUBLIC)
    assert [m.id for m in default.models] == ["cloud-pub", "cloud-priv", "local"]
    local_first = await orch.select(Role.ROUTER, Tier.PUBLIC, prefer="local", privileged=True)
    assert [m.id for m in local_first.models] == ["local", "cloud-pub", "cloud-priv"]


async def test_privileged_model_override_pins_one_model() -> None:
    factory, inv = _three()
    orch = orchestrator(inv, factory)
    result = await orch.chat(
        Role.ROUTER, Tier.PUBLIC, MESSAGES, model="cloud-priv", privileged=True
    )
    assert result.model_id == "cloud-priv"
    assert factory.built == ["cloud-priv"]


@pytest.mark.parametrize(
    "override, tier, reason",
    [
        ("off", Tier.PUBLIC, Rejection.DISABLED),
        ("judge-only", Tier.PUBLIC, Rejection.WRONG_ROLE),
        ("pub", Tier.PRIVATE, Rejection.UNDER_TIER),
        ("ghost", Tier.PUBLIC, Rejection.UNKNOWN_MODEL),
    ],
)
async def test_bad_overrides_are_rejected_without_fallback(
    override: str, tier: Tier, reason: Rejection
) -> None:
    inv = inventory(
        spec("ok", max_tier="private"),
        spec("off", enabled=False),
        spec("judge-only", roles=["judge"]),
        spec("pub", max_tier="public"),
    )
    factory = Factory({"ok": ["fine"]})
    orch = orchestrator(inv, factory)
    with pytest.raises(OverrideRejectedError) as info:
        await orch.chat(Role.ROUTER, tier, MESSAGES, model=override, privileged=True)
    assert info.value.reason is reason
    assert factory.built == []  # never silently fell back to "ok"


async def test_store_disable_makes_override_rejected() -> None:
    factory, inv = _three()
    store = InMemoryOverrideStore(inv)
    store.set_model("cloud-priv", ModelOverride(enabled=False))
    orch = orchestrator(inv, factory, store=store)
    with pytest.raises(OverrideRejectedError, match="disabled"):
        await orch.select(Role.ROUTER, Tier.PUBLIC, model="cloud-priv", privileged=True)


async def test_unprivileged_override_is_ignored_and_logged() -> None:
    factory, inv = _three()
    orch = orchestrator(inv, factory)
    with capture_logs() as logs:
        result = await orch.chat(Role.ROUTER, Tier.PUBLIC, MESSAGES, model="local", prefer="local")
    assert result.model_id == "cloud-pub"
    ignored = [e for e in logs if e["event"] == "override ignored for unprivileged caller"]
    assert ignored and ignored[0]["override_model"] == "local"


async def test_unprivileged_bad_override_does_not_raise() -> None:
    factory, inv = _three()
    result = await orchestrator(inv, factory).chat(
        Role.ROUTER, Tier.PUBLIC, MESSAGES, model="ghost"
    )
    assert result.model_id == "cloud-pub"


async def test_store_changes_apply_on_next_call() -> None:
    factory, inv = _three()
    store = InMemoryOverrideStore(inv)
    orch = orchestrator(inv, factory, store=store)
    assert (await orch.chat(Role.ROUTER, Tier.PUBLIC, MESSAGES)).model_id == "cloud-pub"

    store.set_chain(Role.ROUTER, ["local", "cloud-pub"])
    assert (await orch.chat(Role.ROUTER, Tier.PUBLIC, MESSAGES)).model_id == "local"

    store.set_model("cloud-priv", ModelOverride(max_tier="public"))
    with pytest.raises(OverrideRejectedError) as info:
        await orch.select(Role.ROUTER, Tier.PRIVATE, model="cloud-priv", privileged=True)
    assert info.value.reason is Rejection.UNDER_TIER
