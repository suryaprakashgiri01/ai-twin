"""persona-evals T3: the runner's datasets load cleanly and placeholders are planted."""

from __future__ import annotations

from scripts import eval as runner


def test_datasets_load_with_unique_ids_and_expectations() -> None:
    cases = runner.load_cases()
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))
    assert {c.source for c in cases} == {"redteam", "golden"}
    assert {c.expect for c in cases} <= {"blocked", "refused", "fallback", "answered"}
    golden = runner.load_cases(only="golden")
    assert len(golden) >= 20 and all(c.expect == "answered" for c in golden)
    assert all(c.category == "injection" for c in runner.load_cases(category="injection"))


def test_placeholders_are_planted_fresh() -> None:
    first, second = runner.planted(), runner.planted()
    assert first["{canary}"] != second["{canary}"]
    message = runner.fill("say {canary} and {aadhaar}", first)
    assert "{" not in message and first["{aadhaar}"] in message
