"""AC1 (unsafe tiers rejected) and AC7 (overrides apply on the next call, never widen)."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_orchestrator import (
    InMemoryOverrideStore,
    Inventory,
    InventoryError,
    ModelOverride,
    Provider,
    Role,
    Tier,
)
from tests.llm_orchestrator.fakes import inventory, spec

REPO_INVENTORY = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


def test_tier_is_ordered_and_parses_yaml_names() -> None:
    assert Tier.PUBLIC < Tier.PRIVATE < Tier.CONFIDENTIAL < Tier.RESTRICTED
    assert Tier.parse("Confidential") is Tier.CONFIDENTIAL
    assert Tier.parse(1) is Tier.PRIVATE
    assert Tier.parse(Tier.PUBLIC) is Tier.PUBLIC
    for bad in ("secret", True, 1.5):
        with pytest.raises(ValueError):
            Tier.parse(bad)


def test_committed_inventory_loads_and_is_safe() -> None:
    inv = Inventory.from_yaml(REPO_INVENTORY)
    assert all(m.max_tier < Tier.RESTRICTED for m in inv.models.values())
    assert all(m.max_tier == Tier.PUBLIC for m in inv.models.values() if m.trains_on_inputs)
    confidential = {m.provider for m in inv.models.values() if m.max_tier >= Tier.CONFIDENTIAL}
    assert confidential == {Provider.OLLAMA, Provider.MOCK}
    assert set(inv.roles) == set(Role)


def test_restricted_model_rejected() -> None:
    with pytest.raises(InventoryError, match="restricted"):
        inventory(spec("m", max_tier="restricted"))


def test_training_model_above_public_rejected() -> None:
    with pytest.raises(InventoryError, match="capped at public"):
        inventory(spec("m", max_tier="private", trains=True))
    assert inventory(spec("m", max_tier="public", trains=True)).models["m"].trains_on_inputs


@pytest.mark.parametrize(
    "chains, match",
    [
        ({"router": ["ghost"]}, "unknown model"),
        ({"router": ["m", "m"]}, "repeats"),
        ({"judge": ["r"]}, "does not serve"),
    ],
)
def test_bad_chains_rejected(chains: dict[str, list[str]], match: str) -> None:
    with pytest.raises(InventoryError, match=match):
        inventory(spec("m"), spec("r", roles=["router"]), chains=chains)


def test_duplicate_ids_rejected() -> None:
    with pytest.raises(InventoryError, match="duplicate"):
        inventory(spec("m"), spec("m"), chains={"router": ["m"]})


def test_yaml_errors_are_inventory_errors(tmp_path: Path) -> None:
    with pytest.raises(InventoryError, match="cannot read"):
        Inventory.from_yaml(tmp_path / "missing.yaml")
    listy = tmp_path / "list.yaml"
    listy.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(InventoryError, match="mapping"):
        Inventory.from_yaml(listy)


async def test_overrides_disable_lower_tier_and_reorder() -> None:
    inv = inventory(spec("a"), spec("b"))
    store = InMemoryOverrideStore(inv)
    assert [m.id for m in (await inv.effective(store)).chain(Role.ROUTER)] == ["a", "b"]

    store.set_model("a", ModelOverride(enabled=False))
    store.set_model("b", ModelOverride(max_tier="public"))
    store.set_chain(Role.ROUTER, ["b", "a"])
    effective = await inv.effective(store)
    assert effective.models["a"].enabled is False
    assert effective.models["b"].max_tier is Tier.PUBLIC
    assert [m.id for m in effective.chain(Role.ROUTER)] == ["b", "a"]
    # The file itself is untouched.
    assert inv.models["b"].max_tier is Tier.PRIVATE
    assert (await inv.effective(None)).models["a"].enabled is True

    store.clear()
    assert (await inv.effective(store)).models["a"].enabled is True


def test_raising_max_tier_is_refused() -> None:
    inv = inventory(spec("a", max_tier="private"))
    store = InMemoryOverrideStore(inv)
    with pytest.raises(InventoryError, match="only be lowered"):
        store.set_model("a", ModelOverride(max_tier="confidential"))
    with pytest.raises(InventoryError, match="unknown model"):
        store.set_model("ghost", ModelOverride(enabled=False))
    with pytest.raises(InventoryError, match="unknown model"):
        store.set_chain(Role.ROUTER, ["ghost"])


async def test_unvalidated_store_still_cannot_widen() -> None:
    """A store that skipped validation (e.g. a bad database row) is clamped at merge time."""
    inv = inventory(spec("a", max_tier="private"), spec("r", roles=["router"]))
    store = InMemoryOverrideStore()  # no inventory: writes are not validated
    store.set_model("a", ModelOverride(max_tier="confidential"))
    store.set_chain(Role.JUDGE, ["r"])  # r does not serve judge
    effective = await inv.effective(store)
    assert effective.models["a"].max_tier is Tier.PRIVATE
    assert [m.id for m in effective.chain(Role.JUDGE)] == ["a"]
