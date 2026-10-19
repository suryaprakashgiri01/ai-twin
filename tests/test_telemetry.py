"""observability-alerts T1: one trace id per request on every log line and in `X-Trace-ID`;
presidio warns once, through our handler only."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from app.core.logging import OnceFilter, RateLimitFilter, quiet_presidio
from app.kb.ingestion.redaction import ner_configuration
from tests.conftest import make_app


async def test_trace_id_on_every_line_matches_header(capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        first = await c.get("/healthz")
        second = await c.get("/nope")
    trace_id = first.headers["X-Trace-ID"]
    assert len(trace_id) == 32 and int(trace_id, 16)
    assert second.headers["X-Trace-ID"] != trace_id
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    completed = [line for line in lines if line.get("event") == "request completed"]
    assert completed, "the 404 writes an access line at warning"
    for line in completed:
        assert line["trace_id"] == second.headers["X-Trace-ID"]
        assert len(line["span_id"]) == 16


async def test_request_id_still_echoed_with_trace_id() -> None:
    transport = httpx.ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/healthz", headers={"X-Request-ID": "req-9"})
    assert response.headers["X-Request-ID"] == "req-9"
    assert "X-Trace-ID" in response.headers


async def test_incoming_traceparent_is_not_trusted() -> None:
    chosen = "4bf92f3577b34da6a3ce929d0e0e4736"
    transport = httpx.ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get(
            "/healthz", headers={"traceparent": f"00-{chosen}-00f067aa0ba902b7-01"}
        )
    assert response.headers["X-Trace-ID"] != chosen


def test_presidio_loggers_use_root_handler_once(capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("presidio_analyzer")
    make_app()  # our logging config: one root handler, JSON lines
    quiet_presidio()
    quiet_presidio()  # idempotent: one filter, no handler of its own
    for name in ("presidio-analyzer", "decision_process"):
        target = logging.getLogger(name)
        assert all(isinstance(h, logging.NullHandler) for h in target.handlers)
        assert sum(isinstance(f, OnceFilter) for f in target.filters) == 1
    log = logging.getLogger("presidio-analyzer")
    for _ in range(3):
        log.warning("Entity GPE is not mapped to a Presidio entity")
    out = [line for line in capsys.readouterr().out.splitlines() if "not mapped" in line]
    assert len(out) == 1


def test_ner_configuration_ignores_every_label_but_person() -> None:
    pytest.importorskip("presidio_analyzer")
    ner = SimpleNamespace(labels=("PERSON", "GPE", "ORG", "DATE"))
    nlp = SimpleNamespace(pipe_names=["ner"], get_pipe=lambda _name: ner)
    config = ner_configuration(nlp)
    assert sorted(config.labels_to_ignore) == ["DATE", "GPE", "ORG"]
    assert config.model_to_presidio_entity_mapping == {"PERSON": "PERSON", "PER": "PERSON"}


def test_rate_limit_filter_lets_one_record_a_minute_through() -> None:
    now = [0.0]
    limiter = RateLimitFilter(60.0, clock=lambda: now[0])

    def record() -> logging.LogRecord:
        return logging.LogRecord("x", logging.WARNING, __file__, 1, "export failed", (), None)

    assert limiter.filter(record())
    now[0] = 30.0
    assert not limiter.filter(record())
    assert not limiter.filter(record())
    now[0] = 61.0
    passed = record()
    assert limiter.filter(passed)
    assert passed.getMessage() == "export failed (2 similar suppressed)"
