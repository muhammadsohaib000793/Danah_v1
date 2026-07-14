"""Provider-agnostic LLM gateway.

Everything above this line (agents, chat, orchestrator) speaks only `LLMGateway`. Everything
below it (Anthropic, OpenAI) implements `LLMProvider`. Swapping vendors — or pointing at a
sovereign endpoint — changes one env var, not one line of business logic (architecture §12.5).

Responsibilities that live here, once, rather than in each provider:
  * retries with exponential backoff on 429/5xx
  * optional cross-provider fallback
  * structured output: validate against a Pydantic schema, with a single repair retry
  * usage accounting: one `api_usage` row and one metrics observation per call
  * redaction: never log prompt text at OFFICIAL or above
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.enums import LLMProvider as ProviderName
from app.enums import UsagePurpose
from app.exceptions import LLMGatewayError, LLMNotConfiguredError
from app.logging import get_request_id

log = structlog.get_logger(__name__)

# Ceiling on any retry wait. Long enough to outlast a per-minute token quota, short enough that
# a misreported header cannot park a pipeline step for the whole job timeout.
_MAX_RETRY_AFTER_SECONDS = 65.0

# First wait after a rate limit, doubled per attempt (20s → 40s → capped). Sized against a
# 60-second token window: with the default 3 attempts the last one lands after the window has
# rolled, which is the only wait length that can actually succeed.
_RATE_LIMIT_FLOOR_SECONDS = 20.0

TModel = TypeVar("TModel", bound=BaseModel)


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    stop_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMProviderClient(ABC):
    """What a concrete vendor must implement. Deliberately tiny."""

    name: ProviderName

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str,
        tools: list[dict[str, Any]] | None,
        model: str,
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
    ) -> LLMResult: ...

    @abstractmethod
    def is_retryable(self, exc: Exception) -> bool:
        """True for transient failures (429, 5xx, timeouts, connection resets)."""

    def retry_after_seconds(self, exc: Exception) -> float | None:
        """How long the provider says to wait, from the 429's `Retry-After` header."""
        return None

    def is_rate_limit(self, exc: Exception) -> bool:
        """True when the provider refused because a quota window is full, not because we erred."""
        return False

    @abstractmethod
    async def aclose(self) -> None: ...


class LLMGateway(Protocol):
    """The interface the rest of the application depends on (and that tests fake)."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = ...,
        tools: list[dict[str, Any]] | None = ...,
        model: str | None = ...,
        max_tokens: int = ...,
        temperature: float = ...,
        json_schema: dict[str, Any] | None = ...,
        purpose: str = ...,
        user_id: uuid.UUID | None = ...,
    ) -> LLMResult: ...


class DefaultLLMGateway:
    """The production gateway: retries, fallback, structured output, usage accounting."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        primary: LLMProviderClient | None = None,
        fallback: LLMProviderClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._primary = primary
        self._fallback = fallback
        self._usage_sink: Any = None

    # -- provider resolution -------------------------------------------------
    def _build_primary(self) -> LLMProviderClient:
        cfg = self.settings
        if not cfg.has_llm_credentials:
            # PENDING-CREDENTIALS: a 503 that names the fix, never a fabricated answer.
            raise LLMNotConfiguredError()

        if cfg.llm_provider is ProviderName.ANTHROPIC:
            from app.services.llm.anthropic_provider import AnthropicProvider

            return AnthropicProvider(cfg)

        from app.services.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(cfg)

    def _build_fallback(self) -> LLMProviderClient | None:
        cfg = self.settings
        if not cfg.llm_fallback_enabled:
            return None
        if cfg.llm_provider is ProviderName.ANTHROPIC:
            if not cfg.openai_api_key.get_secret_value():
                return None
            from app.services.llm.openai_provider import OpenAIProvider

            return OpenAIProvider(cfg)
        if not cfg.anthropic_api_key.get_secret_value():
            return None
        from app.services.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)

    @property
    def primary(self) -> LLMProviderClient:
        if self._primary is None:
            self._primary = self._build_primary()
        return self._primary

    @property
    def fallback(self) -> LLMProviderClient | None:
        if self._fallback is None:
            self._fallback = self._build_fallback()
        return self._fallback

    def model_for(self, tier: str, override: str | None = None) -> str:
        """`tier` is 'primary' (judgment) or 'fast' (triage/memory) — the cost-control lever."""
        if override:
            return override
        cfg = self.settings
        if cfg.llm_provider is ProviderName.ANTHROPIC:
            return cfg.llm_model_primary if tier == "primary" else cfg.llm_model_fast
        return cfg.openai_model_primary if tier == "primary" else cfg.openai_model_fast

    # -- the call ------------------------------------------------------------
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict[str, Any] | None = None,
        purpose: str = UsagePurpose.CHAT.value,
        user_id: uuid.UUID | None = None,
    ) -> LLMResult:
        cfg = self.settings
        resolved_model = model or self.model_for("primary")
        max_tokens = max_tokens if max_tokens is not None else cfg.llm_max_tokens_default
        temperature = temperature if temperature is not None else cfg.llm_temperature_default

        result = await self._complete_with_retries(
            self.primary,
            messages,
            system=system,
            tools=tools,
            model=resolved_model,
            max_tokens=max_tokens,
            temperature=temperature,
            json_schema=json_schema,
            purpose=purpose,
            user_id=user_id,
        )
        return result

    async def _complete_with_retries(
        self,
        provider: LLMProviderClient,
        messages: list[dict[str, Any]],
        *,
        system: str,
        tools: list[dict[str, Any]] | None,
        model: str,
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
        purpose: str,
        user_id: uuid.UUID | None,
    ) -> LLMResult:
        cfg = self.settings
        attempts = max(1, cfg.llm_max_retries)
        last_exc: Exception | None = None

        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    provider.complete(
                        messages,
                        system=system,
                        tools=tools,
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        json_schema=json_schema,
                    ),
                    timeout=cfg.llm_timeout_seconds,
                )
            except (TimeoutError, Exception) as exc:
                last_exc = exc
                latency_ms = int((time.perf_counter() - started) * 1000)
                retryable = isinstance(exc, TimeoutError) or provider.is_retryable(exc)
                await self._record(
                    provider=provider.name.value,
                    model=model,
                    purpose=purpose,
                    usage=TokenUsage(),
                    latency_ms=latency_ms,
                    user_id=user_id,
                    outcome="error",
                )
                log.warning(
                    "llm_call_failed",
                    provider=provider.name.value,
                    model=model,
                    purpose=purpose,
                    attempt=attempt + 1,
                    max_attempts=attempts,
                    retryable=retryable,
                    error_type=type(exc).__name__,
                    request_id=get_request_id(),
                )
                if not retryable or attempt == attempts - 1:
                    break
                # Exponential backoff with full jitter — avoids a thundering herd of retries
                # when a provider rate-limits the whole fleet at once.
                delay = min(2**attempt, 8) * (0.5 + random.random())  # noqa: S311 - jitter, not crypto

                if provider.is_rate_limit(exc):
                    # A token quota is measured over a *minute*, so a retry has to be able to
                    # outlast one. Both the provider's own `Retry-After` (observed at 2s, then
                    # 7s) and this exponential curve are far shorter than that — every attempt
                    # lands while the window is still closed, and the step fails ~10s in.
                    #
                    # The window is usually full of *our own* traffic: the analysis agents fan
                    # out in parallel, and OpenAI charges `max_tokens` against the quota at
                    # request time, so a run reserves far more than it spends. The agent that
                    # runs last (Briefing) is the one that pays for it.
                    #
                    # So take the longest of the three — what the provider asked for, the
                    # exponential curve, and a floor that doubles into the next window — and
                    # jitter it so a parallel fan-out does not resume in lockstep.
                    stated = provider.retry_after_seconds(exc) or 0.0
                    floor = _RATE_LIMIT_FLOOR_SECONDS * (2**attempt)
                    delay = max(stated, delay, floor)

                delay = min(delay, _MAX_RETRY_AFTER_SECONDS) + random.random()  # noqa: S311
                log.info(
                    "llm_retry_scheduled",
                    provider=provider.name.value,
                    attempt=attempt + 1,
                    delay_seconds=round(delay, 1),
                    rate_limited=provider.is_rate_limit(exc),
                    request_id=get_request_id(),
                )
                await asyncio.sleep(delay)
                continue

            latency_ms = int((time.perf_counter() - started) * 1000)
            result.latency_ms = latency_ms
            result.provider = provider.name.value
            result.model = model
            await self._record(
                provider=provider.name.value,
                model=model,
                purpose=purpose,
                usage=result.usage,
                latency_ms=latency_ms,
                user_id=user_id,
                outcome="success",
            )
            log.info(
                "llm_call",
                provider=provider.name.value,
                model=model,
                purpose=purpose,
                tokens_in=result.usage.input_tokens,
                tokens_out=result.usage.output_tokens,
                latency_ms=latency_ms,
                # Prompt text is never logged: it may carry OFFICIAL-SENSITIVE content (§12).
                messages=len(messages),
                tools=len(tools or []),
            )
            return result

        # Primary exhausted. Try the other vendor if configured.
        fallback = self.fallback
        if fallback is not None and provider is not fallback:
            log.warning("llm_failover", to=fallback.name.value, purpose=purpose)
            return await self._complete_with_retries(
                fallback,
                messages,
                system=system,
                tools=tools,
                # The model name is vendor-specific; re-resolve for the fallback vendor.
                model=self._fallback_model(model),
                max_tokens=max_tokens,
                temperature=temperature,
                json_schema=json_schema,
                purpose=purpose,
                user_id=user_id,
            )

        raise LLMGatewayError(
            "The language model provider is unavailable after retries.",
            detail={"provider": provider.name.value, "model": model, "purpose": purpose},
        ) from last_exc

    def _fallback_model(self, primary_model: str) -> str:
        """Map a model on the primary vendor to the equivalent tier on the fallback vendor."""
        cfg = self.settings
        is_fast = primary_model in (cfg.llm_model_fast, cfg.openai_model_fast)
        if cfg.llm_provider is ProviderName.ANTHROPIC:
            return cfg.openai_model_fast if is_fast else cfg.openai_model_primary
        return cfg.llm_model_fast if is_fast else cfg.llm_model_primary

    # -- structured output ---------------------------------------------------
    async def complete_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: type[TModel],
        system: str = "",
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        purpose: str = UsagePurpose.AGENT.value,
        user_id: uuid.UUID | None = None,
    ) -> tuple[TModel, LLMResult]:
        """Return a validated Pydantic instance, with exactly one repair attempt.

        A single repair is deliberate: if a model cannot produce the schema when handed its own
        output and the validation errors, a third try rarely helps and the caller (an agent
        step) is better off failing loudly and marking its step `failed`.
        """
        json_schema = schema.model_json_schema()

        result = await self.complete(
            messages,
            system=system,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            json_schema=json_schema,
            purpose=purpose,
            user_id=user_id,
        )

        try:
            return self._parse(result.text, schema), result
        except (ValidationError, json.JSONDecodeError, ValueError) as first_error:
            log.warning(
                "llm_structured_output_invalid",
                schema=schema.__name__,
                error=str(first_error)[:400],
                attempt="repair",
            )

        repair_messages = [
            *messages,
            {"role": "assistant", "content": result.text},
            {
                "role": "user",
                "content": (
                    "Your previous reply did not conform to the required JSON schema and could "
                    "not be parsed. Reply again with ONLY a single JSON object that validates "
                    "against this schema. No prose, no markdown fences.\n\n"
                    f"Schema:\n{json.dumps(json_schema, indent=2)}"
                ),
            },
        ]
        repaired = await self.complete(
            repair_messages,
            system=system,
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,  # determinism helps the model hit the schema on the retry
            json_schema=json_schema,
            purpose=purpose,
            user_id=user_id,
        )

        try:
            parsed = self._parse(repaired.text, schema)
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            raise LLMGatewayError(
                "The model did not return output matching the required schema.",
                detail={"schema": schema.__name__, "error": str(exc)[:400]},
            ) from exc

        # Bill both attempts — the repair cost real tokens.
        repaired.usage.input_tokens += result.usage.input_tokens
        repaired.usage.output_tokens += result.usage.output_tokens
        return parsed, repaired

    @staticmethod
    def _parse(text: str, schema: type[TModel]) -> TModel:
        """Parse a model reply into `schema`, tolerating markdown fences and surrounding prose."""
        candidate = text.strip()

        fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", candidate, re.DOTALL)
        if fenced:
            candidate = fenced.group(1).strip()

        if not candidate.startswith(("{", "[")):
            # Fall back to the outermost {...} span.
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start != -1 and end > start:
                candidate = candidate[start : end + 1]

        return schema.model_validate_json(candidate)

    # -- accounting ----------------------------------------------------------
    async def _record(
        self,
        *,
        provider: str,
        model: str,
        purpose: str,
        usage: TokenUsage,
        latency_ms: int,
        user_id: uuid.UUID | None,
        outcome: str,
    ) -> None:
        from app.metrics import record_llm_call
        from app.services.llm.usage_tracker import compute_cost_usd, record_usage

        cost = compute_cost_usd(self.settings, model, usage.input_tokens, usage.output_tokens)

        record_llm_call(
            provider=provider,
            model=model,
            purpose=purpose,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            cost_usd=float(cost),
            latency_ms=latency_ms,
            outcome=outcome,
        )

        if outcome != "success":
            return

        await record_usage(
            provider=provider,
            model=model,
            purpose=purpose,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            request_id=get_request_id(),
            user_id=user_id,
        )

    async def aclose(self) -> None:
        if self._primary is not None:
            await self._primary.aclose()
        if self._fallback is not None:
            await self._fallback.aclose()


_gateway: DefaultLLMGateway | None = None


def get_gateway() -> LLMGateway:
    """FastAPI dependency. Tests override this to inject `FakeLLMGateway`."""
    global _gateway
    if _gateway is None:
        _gateway = DefaultLLMGateway()
    return _gateway


def reset_gateway() -> None:
    global _gateway
    _gateway = None
