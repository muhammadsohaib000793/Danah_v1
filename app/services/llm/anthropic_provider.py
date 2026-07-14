"""Anthropic Messages API provider.

Implements `LLMProviderClient` against the official `anthropic` SDK. Everything policy-level
(retries, cross-vendor fallback, structured-output parsing and its repair retry, usage
accounting) belongs to the gateway; this module is a faithful translation between DANAH's
provider-agnostic shapes and Anthropic's wire format, and nothing more.
"""

from __future__ import annotations

import json
from typing import Any, Final, cast

import anthropic
import structlog
from anthropic import AsyncAnthropic, Omit, omit
from anthropic.types import (
    Message,
    MessageParam,
    TextBlock,
    ToolParam,
    ToolUnionParam,
    ToolUseBlock,
)

from app.config import Settings
from app.enums import LLMProvider as ProviderName
from app.exceptions import LLMNotConfiguredError
from app.logging import get_request_id
from app.services.llm.gateway import LLMProviderClient, LLMResult, TokenUsage, ToolCall

log = structlog.get_logger(__name__)

# Anthropic has no native JSON mode, so the schema is pinned to the system prompt. The gateway
# still parses and, if needed, repairs — this is a strong steer, not a guarantee we rely on.
_JSON_MODE_INSTRUCTION = (
    "Reply with ONLY a single JSON object that validates against the JSON Schema below. "
    "Emit no prose, no preamble and no markdown code fences: the first character of your reply "
    "must be '{' and the last must be '}'.\n\nJSON Schema:\n"
)

# Non-streaming responses always carry a stop_reason; `None` only occurs mid-stream.
_DEFAULT_STOP_REASON = "end_turn"

# 5xx means the failure is on Anthropic's side and another attempt may land elsewhere.
_SERVER_ERROR_STATUS = 500

# A tool with no parameters still needs a schema Anthropic will accept.
_EMPTY_INPUT_SCHEMA: Final[dict[str, object]] = {"type": "object", "properties": {}}


class AnthropicProvider(LLMProviderClient):
    """Anthropic Claude via the Messages API."""

    name = ProviderName.ANTHROPIC

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncAnthropic | None = None

    # -- client --------------------------------------------------------------
    def _get_client(self) -> AsyncAnthropic:
        """Build the SDK client on first use so importing this module never needs a key."""
        if self._client is None:
            api_key = self._settings.anthropic_api_key.get_secret_value().strip()
            if not api_key:
                raise LLMNotConfiguredError()
            self._client = AsyncAnthropic(
                api_key=api_key,
                timeout=float(self._settings.llm_timeout_seconds),
                # The gateway owns the retry policy (backoff, jitter, failover). Leaving the
                # SDK's own retries on would silently multiply every attempt by (1 + max_retries)
                # and blow past the gateway's timeout budget.
                max_retries=0,
            )
        return self._client

    # -- the call ------------------------------------------------------------
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
    ) -> LLMResult:
        client = self._get_client()

        response: Message = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            # Anthropic takes the system prompt as a top-level parameter, NOT as a message with
            # role="system" — such a message is rejected outright.
            system=self._build_system(system, json_schema),
            messages=cast("list[MessageParam]", messages),
            tools=self._build_tools(tools),
        )

        result = self._to_result(response)
        log.debug(
            "anthropic_completion",
            model=model,
            stop_reason=result.stop_reason,
            tokens_in=result.usage.input_tokens,
            tokens_out=result.usage.output_tokens,
            tool_calls=len(result.tool_calls),
            request_id=get_request_id(),
        )
        return result

    # -- request translation -------------------------------------------------
    @staticmethod
    def _build_system(system: str, json_schema: dict[str, Any] | None) -> str | Omit:
        if json_schema is None:
            return system or omit
        instruction = _JSON_MODE_INSTRUCTION + json.dumps(json_schema, indent=2)
        return f"{system}\n\n{instruction}" if system else instruction

    @staticmethod
    def _build_tools(tools: list[dict[str, Any]] | None) -> list[ToolUnionParam] | Omit:
        """Translate DANAH tool schemas into Anthropic blocks.

        The gateway hands both providers the same untyped `list[dict[str, Any]]`, so this must
        accept every dialect its OpenAI sibling accepts (`_to_openai_tools`) — otherwise the same
        tool registration succeeds on one vendor and hard-fails on the other, and a failover
        silently changes which tools the model can see. Three shapes are in play: the
        Anthropic-native `input_schema`, OpenAI's flat `parameters`, and OpenAI's nested
        `{"type": "function", "function": {...}}`. A malformed entry is dropped with a warning
        rather than raised: a bare KeyError here reaches the caller as an opaque 502 that names
        neither the tool nor the problem.
        """
        if not tools:
            return omit

        blocks: list[ToolUnionParam] = []
        for index, tool in enumerate(tools):
            # OpenAI-shaped registrations nest the definition; unwrap to the flat form.
            nested = tool.get("function")
            raw: dict[str, Any] = (
                nested if tool.get("type") == "function" and isinstance(nested, dict) else tool
            )

            name = str(raw.get("name") or "").strip()
            if not name:
                # Anthropic would reject the whole request with a 400; drop the malformed entry
                # and let the model answer with the tools that are usable. Never log the schema
                # itself — tool descriptions can quote OFFICIAL content.
                log.warning(
                    "llm_tool_schema_unnamed",
                    provider=ProviderName.ANTHROPIC.value,
                    index=index,
                )
                continue

            schema = raw.get("input_schema") or raw.get("parameters")
            block: ToolParam = {
                "name": name,
                "input_schema": cast("dict[str, object]", schema)
                if isinstance(schema, dict)
                else _EMPTY_INPUT_SCHEMA,
            }
            description = raw.get("description")
            if isinstance(description, str) and description:
                block["description"] = description
            blocks.append(block)

        # Every entry was malformed. `tools: []` is not a valid Anthropic request, so fall back
        # to omitting the parameter entirely rather than sending an empty array.
        return blocks or omit

    # -- response translation ------------------------------------------------
    def _to_result(self, response: Message) -> LLMResult:
        texts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if isinstance(block, TextBlock):
                texts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )

        return LLMResult(
            text="".join(texts),
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
            model=str(response.model),
            provider=self.name.value,
            stop_reason=response.stop_reason or _DEFAULT_STOP_REASON,
        )

    # -- retry classification ------------------------------------------------
    def is_retryable(self, exc: Exception) -> bool:
        """Transient only. Auth failures and 400s are deterministic — retrying just burns money."""
        # RateLimitError is itself an APIStatusError (429), so it must be matched before the
        # generic status check below, which only admits 5xx.
        if isinstance(exc, anthropic.RateLimitError):
            return True
        # APITimeoutError subclasses APIConnectionError; both mean the request never got a
        # verdict from the server, so a fresh attempt is safe.
        if isinstance(exc, anthropic.APIConnectionError | anthropic.APITimeoutError):
            return True
        if isinstance(exc, anthropic.APIStatusError):
            return exc.status_code >= _SERVER_ERROR_STATUS
        return False

    def is_rate_limit(self, exc: Exception) -> bool:
        return isinstance(exc, anthropic.RateLimitError)

    def retry_after_seconds(self, exc: Exception) -> float | None:
        """Read Anthropic's `Retry-After` off the 429 so the wait matches when the quota reopens."""
        if not isinstance(exc, anthropic.APIStatusError) or exc.response is None:
            return None
        raw = exc.response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            # An HTTP-date form is legal here; leave it to the caller's exponential backoff.
            return None

    # -- lifecycle -----------------------------------------------------------
    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
