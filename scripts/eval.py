"""Evaluate the public assistant with real models: `make eval`.

Runs every case (the red-team fixtures in tests/redteam/*.yaml and the golden set in
evals/golden.yaml) through the real public pipeline (guards, scope router, kb_public retrieval,
generator, output guard), scores each with `app.evals.persona`, prints a report, writes JSON to
vault/evals/ and exits 1 when a threshold is missed. Needs the stack (`make up`) and the models.

Each case runs in a fresh visitor conversation that is soft-deleted afterwards, with the answer
cache off. With Langfuse on, each case is one trace tagged `eval` with eval_* scores.

ARGS: --only redteam|golden  --category NAME  --limit N
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.auth.visitor import Visitor
from app.core.config import Settings
from app.core.llm import build_llm
from app.core.resources import open_resources
from app.evals.persona import (
    MAX_FALSE_POSITIVES,
    MIN_EXPECTED,
    MIN_VOICE,
    Case,
    CaseResult,
    Reply,
    score,
    summarise,
)
from app.guardrails.identifiers import verhoeff_check_digit
from app.kb.embedding import FastEmbedder
from app.observability.tracing import build_tracing, install, tracing
from app.services.model_overrides import OrchestratorOverrides
from app.services.public_chat import PublicChatRequest, build_public_chat
from app.services.resume import resume_provider
from scripts._common import fail, info, ok, warn

ROOT = Path(__file__).resolve().parents[1]
REDTEAM = ROOT / "tests" / "redteam"
GOLDEN = ROOT / "evals" / "golden.yaml"
REPORTS = ROOT / "vault" / "evals"


def load_cases(only: str | None = None, category: str | None = None) -> list[Case]:
    files = []
    if only in (None, "redteam"):
        files += [(p, "redteam") for p in sorted(REDTEAM.glob("*.yaml"))]
    if only in (None, "golden"):
        files.append((GOLDEN, "golden"))
    cases = []
    for path, source in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for raw in data["cases"]:
            case = Case(
                id=str(raw["id"]),
                category=str(data["category"]),
                message=str(raw["message"]),
                expect=raw.get("expect", "answered"),
                source=source,
            )
            if category is None or case.category == category:
                cases.append(case)
    return cases


def planted() -> dict[str, str]:
    """Stand-ins for the fixtures' placeholders: a fresh canary-like marker, a valid Aadhaar."""
    body = "2345 6789 012"
    return {
        "{canary}": f"CANARY-{secrets.token_hex(8).upper()}",
        "{aadhaar}": body + verhoeff_check_digit(body.replace(" ", "")),
    }


def fill(message: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        message = message.replace(key, value)
    return message


async def run_case(
    service: Any, conversations: Any, case: Case, message: str, run_id: str
) -> Reply:
    visitor = Visitor(hash=f"eval-{run_id}-{case.id}", forwarded=False)
    prepared = await service.prepare(PublicChatRequest(message=message), visitor)
    try:
        response = await service.answer(prepared)
    finally:
        await conversations.drain()
        await conversations.soft_delete(prepared.conversation_id)
    return Reply(intent=response.intent, answer=response.answer, sources=len(response.sources))


def report(results: list[CaseResult]) -> int:
    summary = summarise(results)
    info(f"{summary.total} cases · outcomes {summary.outcomes}")
    for category, (passed, total) in summary.by_category.items():
        (ok if passed == total else warn)(f"{category}: {passed}/{total}")
    checks = [
        (
            summary.expected_rate >= MIN_EXPECTED,
            f"expected outcomes {summary.expected_rate:.0%} (min {MIN_EXPECTED:.0%})",
        ),
        (
            summary.false_positive_rate <= MAX_FALSE_POSITIVES,
            f"guard false positives {summary.false_positive_rate:.0%} "
            f"(max {MAX_FALSE_POSITIVES:.0%})",
        ),
        (
            summary.voice_rate >= MIN_VOICE,
            f"voice checks {summary.voice_rate:.0%} (min {MIN_VOICE:.0%})",
        ),
    ]
    for passed, line in checks:
        (ok if passed else fail)(line)
    if summary.voice_failures:
        warn(f"voice misses: {summary.voice_failures}")
    for r in summary.failures[:20]:
        warn(
            f"{r.case.id}: {r.note or 'voice ' + ', '.join(k for k, v in r.voice.items() if not v)}"
        )
    return 0 if summary.ok else 1


def write_json(results: list[CaseResult], started: datetime) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"eval-{started:%Y%m%d-%H%M%S}.json"
    summary = summarise(results)
    body = {
        "started": started.isoformat(),
        "summary": {k: v for k, v in asdict(summary).items() if k != "failures"},
        "cases": [
            {
                **asdict(r.case),
                "outcome": r.outcome,
                "passed": r.passed,
                "voice": r.voice,
                "note": r.note,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


async def run(args: argparse.Namespace) -> int:
    cases = load_cases(args.only, args.category)[: args.limit or None]
    if not cases:
        fail("no cases matched")
        return 1
    settings = Settings().model_copy(update={"public_answer_cache_ttl_seconds": 0})
    owner = settings.public_owner_name
    if not owner:
        warn("PUBLIC_OWNER_NAME is not set: the v1 prompt is used, third-person checks skipped")
    install(build_tracing(settings))
    started = datetime.now(UTC)
    run_id = started.strftime("%Y%m%d%H%M%S")
    results: list[CaseResult] = []
    async with open_resources(settings) as resources:
        llm = build_llm(settings, OrchestratorOverrides(lambda: resources.overrides))
        embedder = FastEmbedder(settings.models_dir / "fastembed")
        resume = resume_provider(settings, resources.profile, resources.blobs)
        service = build_public_chat(settings, resources, llm, embedder, resume)
        prompt_lines = [line for line in _prompt(settings).splitlines() if len(line) > 40]
        for n, case in enumerate(cases, 1):
            values = planted()
            message = fill(case.message, values)
            began = time.perf_counter()
            with tracing().run(
                "eval-case", "public", input=case.id, tags=["eval", case.source, case.category]
            ) as trace:
                reply = await run_case(service, resources.conversations, case, message, run_id)
                result = score(
                    case, reply, owner_name=owner, secrets=[*values.values(), *prompt_lines]
                )
                trace.score("eval_pass", result.passed)
                trace.score("eval_outcome", result.outcome, data_type="CATEGORICAL")
                if result.voice:
                    trace.score("eval_voice", result.voice_ok)
            results.append(result)
            mark = "✓" if result.passed and (not result.voice or result.voice_ok) else "✗"
            print(
                f"  {mark} {n}/{len(cases)} {case.id}: {result.outcome} "
                f"({time.perf_counter() - began:.1f}s)",
                flush=True,
            )
    tracing().shutdown()
    code = report(results)
    info(f"report: {write_json(results, started).relative_to(ROOT)}")
    return code


def _prompt(settings: Settings) -> str:
    from app.agents.prompts.public import generate_prompt

    return generate_prompt(settings.public_owner_name)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the public assistant with real models")
    parser.add_argument("--only", choices=["redteam", "golden"])
    parser.add_argument("--category")
    parser.add_argument("--limit", type=int, default=0)
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
