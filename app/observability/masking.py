"""What may leave the machine in a Langfuse trace, decided per channel.

The Langfuse client calls `Masker` on every input, output and metadata value before it becomes
a span attribute, in the caller's context, so the channel of the run in progress (a context
variable set by `Tracing.run`) picks the policy:

- **Owner channels** (owner chat, intake, ingestion, title refresh): every value becomes
  `[owner content masked: N chars]`. `LANGFUSE_OWNER_CONTENT=true` (self-hosted Langfuse only)
  lets it through.
- **Public:** visitor text and answers are public-tier, so they are kept with emails, phone
  numbers and Indian identifiers replaced by `[EMAIL]`, `[PHONE]`, ... `LANGFUSE_PUBLIC_CONTENT`
  false masks them like owner content.
- **Leads:** always masked, and lead fields (name, email, message, ...) are masked wherever they
  appear, the public channel included.
- **No channel** (anything traced outside a run): masked, so a new call site fails closed.

Metadata survives: a mapping keeps the values of known metadata keys (model, role, tier, route,
LangGraph node names) when they are short scalars, and masks every other value one by one, so a
trace shows its structure, models, timings and tokens without the text.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from app.guardrails.identifiers import OUTPUT_RULES, rule_spans

Channel = Literal["owner", "intake", "public", "ingestion", "leads"]
OWNER_CHANNELS: frozenset[str] = frozenset({"owner", "intake", "ingestion"})

# Lead form and capture fields: masked in every channel, whatever the policy.
LEAD_FIELDS = frozenset({"name", "email", "phone", "message", "company", "lead", "draft"})
LEAD_MASK = "[lead field masked]"

# Metadata keys whose short scalar values are structure, not content.
SAFE_KEYS = frozenset(
    {
        "channel",
        "route",
        "route_source",
        "intent",
        "max_tier",
        "tier",
        "model",
        "model_id",
        "fallback_used",
        "cache_hit",
        "env",
        "version",
        "service_version",
        "verdicts",
        "rules",
        "outcome",
        "status",
        "kind",
        "category",
        "source",
        "triage",
        "seq",
        "turns",
        "chunks",
        "incomplete",
        "prompt_version",
        "mode",
        "job_id",
        "document_id",
        "conversation_id",
    }
)
SAFE_PREFIXES = ("ls_", "langgraph_", "llm_", "gen_ai.")
SAFE_STRING_MAX = 120

# International numbers for tracing only: 8+ digits with the usual separators. The identifier
# rules cover Indian mobiles; this catches the rest.
_PHONE = re.compile(r"(?<![\w+])\+?\d[\d\s().-]{6,}\d(?!\w)")
_PLACEHOLDER = {"EMAIL_ADDRESS": "EMAIL", "PHONE_NUMBER": "PHONE"}

_channel: ContextVar[str | None] = ContextVar("langfuse_channel", default=None)


@dataclass(frozen=True)
class MaskPolicy:
    owner_content: bool = False
    public_content: bool = True


def current_channel() -> str | None:
    return _channel.get()


def set_channel(channel: str | None) -> Any:
    """Set the channel for the current context; returns the token to reset it with."""
    return _channel.set(channel)


def reset_channel(token: Any) -> None:
    try:
        _channel.reset(token)
    except ValueError:
        # A streamed run can be closed from another context (client gone, garbage collected);
        # that context ends with it, so nothing is left to restore.
        _channel.set(None)


@contextmanager
def channel_scope(channel: Channel) -> Iterator[None]:
    """Trace a block under another channel's policy, e.g. lead extraction inside a public run."""
    token = set_channel(channel)
    try:
        yield
    finally:
        reset_channel(token)


def mask_identifiers(text: str) -> str:
    """`text` with emails, phone numbers and Indian identifiers replaced by typed placeholders."""
    spans = sorted(rule_spans(text, OUTPUT_RULES), key=lambda s: (s[0], -s[1]))
    out: list[str] = []
    cursor = 0
    for start, end, entity, _score in spans:
        if start < cursor:
            continue
        out.append(text[cursor:start])
        out.append(f"[{_PLACEHOLDER.get(entity, entity.removeprefix('IN_'))}]")
        cursor = end
    out.append(text[cursor:])
    return _PHONE.sub(_phone_placeholder, "".join(out))


def _phone_placeholder(match: re.Match[str]) -> str:
    digits = sum(ch.isdigit() for ch in match.group(0))
    return "[PHONE]" if digits >= 8 else match.group(0)


def text_length(data: Any) -> int:
    """Characters of text in `data`, however nested."""
    if data is None:
        return 0
    if isinstance(data, str):
        return len(data)
    if isinstance(data, Mapping):
        return sum(text_length(v) for v in data.values())
    if isinstance(data, list | tuple | set):
        return sum(text_length(v) for v in data)
    return len(str(data))


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, bool | int | float)


def _safe(key: object, value: Any) -> bool:
    if not isinstance(key, str) or not (key in SAFE_KEYS or key.startswith(SAFE_PREFIXES)):
        return False
    if _is_scalar(value):
        return True
    if isinstance(value, str):
        return len(value) <= SAFE_STRING_MAX
    if isinstance(value, list | tuple):
        return all(
            _is_scalar(v) or (isinstance(v, str) and len(v) <= SAFE_STRING_MAX) for v in value
        )
    return False


class Masker:
    """Langfuse's `mask=` hook: `mask(data=...)` returns what may be sent."""

    def __init__(self, policy: MaskPolicy) -> None:
        self._policy = policy

    def __call__(self, *, data: Any, **_: Any) -> Any:
        channel = current_channel()
        if channel == "public" and self._policy.public_content:
            return self._public(data)
        if channel in OWNER_CHANNELS and self._policy.owner_content:
            return self._passthrough(data)
        label = "public" if channel == "public" else "lead" if channel == "leads" else "owner"
        return self._masked(data, label)

    def _masked(self, data: Any, label: str) -> Any:
        if data is None or _is_scalar(data):
            return data
        if isinstance(data, Mapping):
            return {k: v if _safe(k, v) else self._masked_value(v, label) for k, v in data.items()}
        return self._placeholder(data, label)

    def _masked_value(self, value: Any, label: str) -> Any:
        return (
            self._masked(value, label)
            if isinstance(value, Mapping)
            else (self._placeholder(value, label))
        )

    @staticmethod
    def _placeholder(value: Any, label: str) -> Any:
        if value is None or _is_scalar(value):
            return value
        return f"[{label} content masked: {text_length(value)} chars]"

    def _public(self, data: Any) -> Any:
        if isinstance(data, str):
            return mask_identifiers(data)
        if isinstance(data, Mapping):
            return {
                k: LEAD_MASK if k in LEAD_FIELDS and data[k] is not None else self._public(v)
                for k, v in data.items()
            }
        if isinstance(data, list | tuple):
            return [self._public(v) for v in data]
        if data is None or _is_scalar(data):
            return data
        return mask_identifiers(str(data))

    def _passthrough(self, data: Any) -> Any:
        # Even with owner content on, a lead's fields stay masked.
        if isinstance(data, Mapping):
            return {
                k: LEAD_MASK if k in LEAD_FIELDS and data[k] is not None else self._passthrough(v)
                for k, v in data.items()
            }
        if isinstance(data, list | tuple):
            return [self._passthrough(v) for v in data]
        return data
