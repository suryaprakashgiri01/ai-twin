"""Core types: tiers, roles, model specs, call results and the error taxonomy."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Tier(IntEnum):
    """Data sensitivity, ordered so that policy is a single comparison."""

    PUBLIC = 0
    PRIVATE = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3

    @classmethod
    def parse(cls, value: object) -> Tier:
        """Accept a Tier, its lowercase name (as written in YAML) or its integer value."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError:
                raise ValueError(f"unknown tier {value!r}") from None
        if isinstance(value, int) and not isinstance(value, bool):
            return cls(value)
        raise ValueError(f"unknown tier {value!r}")

    @property
    def label(self) -> str:
        return self.name.lower()


class Role(StrEnum):
    ROUTER = "router"
    CLASSIFIER = "classifier"
    GENERATOR = "generator"
    JUDGE = "judge"
    # Prompt-injection scoring of untrusted text; the reply is a 0..1 score, not prose.
    GUARD = "guard"


class Provider(StrEnum):
    GROQ = "groq"
    CEREBRAS = "cerebras"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"
    MOCK = "mock"
    # Paid APIs; `openai` also serves any OpenAI-compatible endpoint through `base_url`.
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


# Providers that run on the owner's machine; every other one is a cloud API.
LOCAL_PROVIDERS = frozenset({Provider.OLLAMA, Provider.MOCK})


# What a model can do beyond plain text. Every model has `text`.
Capability = Literal["text", "tools", "reasoning"]

Prefer = Literal["fast", "quality", "local", "local-quality", "local-max", "paid"]


class FreeTierLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None


class Cost(BaseModel):
    """USD per million tokens; zero for free tiers and local models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_mtok: float = Field(default=0.0, ge=0)
    output_per_mtok: float = Field(default=0.0, ge=0)


class ModelSpec(BaseModel):
    """One model in the inventory. Validation enforces the tier ceiling rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    provider: Provider
    model: str = Field(min_length=1)
    roles: frozenset[Role]
    max_tier: Tier
    context_window: int = Field(gt=0)
    trains_on_inputs: bool
    free_tier: FreeTierLimits | None = None
    cost: Cost = Cost()
    enabled: bool = True
    # Ordering hints for `prefer`: "fast", "quality", "local".
    tags: frozenset[str] = frozenset()
    base_url: str | None = None
    # Declared, not probed: what the model can do, and a rough strength rank (1 weakest, 5
    # strongest) the gateway uses to fit a model to a request's complexity.
    capabilities: frozenset[Capability] = frozenset({"text"})
    quality: int = Field(default=3, ge=1, le=5)

    @field_validator("capabilities", mode="after")
    @classmethod
    def _with_text(cls, value: frozenset[Capability]) -> frozenset[Capability]:
        return value | {"text"}

    @field_validator("max_tier", mode="before")
    @classmethod
    def _parse_tier(cls, value: object) -> Tier:
        return Tier.parse(value)

    @model_validator(mode="after")
    def _tier_ceiling(self) -> ModelSpec:
        if self.max_tier >= Tier.RESTRICTED:
            raise ValueError(f"{self.id}: no model may serve the restricted tier")
        if self.trains_on_inputs and self.max_tier > Tier.PUBLIC:
            raise ValueError(f"{self.id}: a model that may train on inputs is capped at public")
        if self.provider not in LOCAL_PROVIDERS and self.max_tier > Tier.PRIVATE:
            raise ValueError(
                f"{self.id}: cloud models are capped at private; confidential text is local only"
            )
        return self

    def serves(self, role: Role) -> bool:
        return role in self.roles


class Rejection(StrEnum):
    """Why a model cannot serve a request (or an override was refused)."""

    UNKNOWN_MODEL = "unknown_model"
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"  # provider key or endpoint missing
    WRONG_ROLE = "wrong_role"
    UNDER_TIER = "under_tier"


AttemptOutcome = Literal["ok", "transient_error", "permanent_error", "invalid_output"]


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0


class Attempt(BaseModel):
    """One provider call. Carries no prompt or completion content."""

    model_config = ConfigDict(frozen=True)

    model_id: str
    number: int
    outcome: AttemptOutcome
    latency_ms: float
    error_type: str | None = None
    status_code: int | None = None


class CallResult(BaseModel):
    text: str
    model_id: str
    attempts: list[Attempt]
    fallback_used: bool
    usage: TokenUsage = TokenUsage()
    # A reasoning model's thinking, kept apart from `text`; never logged.
    thinking: str = ""


class StructuredResult[T: BaseModel](BaseModel):
    value: T
    model_id: str
    attempts: list[Attempt]
    fallback_used: bool


# --- errors -------------------------------------------------------------------------------


class OrchestratorError(Exception):
    """Base for every error this package raises."""


class InventoryError(OrchestratorError, ValueError):
    """The inventory file or an override is invalid."""


class NoEligibleModelError(OrchestratorError):
    """No enabled, configured model may serve this role at this tier."""

    def __init__(self, role: Role, tier: Tier) -> None:
        super().__init__(f"no eligible model for role={role.value} tier={tier.label}")
        self.role = role
        self.tier = tier


class OverrideRejectedError(OrchestratorError):
    """A privileged caller asked for a model that may not serve this call."""

    def __init__(self, model_id: str, reason: Rejection) -> None:
        super().__init__(f"override to {model_id!r} rejected: {reason.value}")
        self.model_id = model_id
        self.reason = reason


class AllModelsFailedError(OrchestratorError):
    """Every eligible model failed; `attempts` says how each one did."""

    def __init__(self, role: Role, tier: Tier, attempts: list[Attempt]) -> None:
        summary = ", ".join(f"{a.model_id}#{a.number}:{a.outcome}" for a in attempts)
        super().__init__(f"all models failed for role={role.value} tier={tier.label}: {summary}")
        self.role = role
        self.tier = tier
        self.attempts = attempts


class ProviderError(OrchestratorError):
    """A provider call failed in a way the caller has already classified."""

    def __init__(
        self, message: str, *, transient: bool, status_code: int | None = None, **extra: Any
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.status_code = status_code
        self.extra = extra
