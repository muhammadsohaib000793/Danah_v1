"""OpenAI implementation of `LLMProviderClient`.

The vendor differences this file exists to absorb (architecture §12.5):
  * the system prompt is a `role="system"` message, not a top-level parameter;
  * tool schemas are nested under `{"type": "function", "function": {...}}`;
  * tool-call arguments arrive as a JSON *string*, not a parsed object;
  * usage is reported as `prompt_tokens` / `completion_tokens`.

Retries, failover and structured-output validation belong to the gateway, not here.
"""

from __future__ import annotations

import json
from typing import Any, Final, cast

import openai
import structlog
from openai import AsyncOpenAI, omit
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionFunctionToolParam,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
)
from openai.types.shared_params import FunctionDefinition, ResponseFormatJSONObject

from app.config import Settings, get_settings
from app.enums import LLMProvider as ProviderName
from app.exceptions import LLMGatewayError, LLMNotConfiguredError
from app.services.llm.gateway import LLMProviderClient, LLMResult, TokenUsage, ToolCall

log = structlog.get_logger(__name__)

# APIStatusError covers every non-2xx; only 5xx is worth another attempt.
_SERVER_ERROR_FLOOR: Final[int] = 500

_JSON_OBJECT_FORMAT: Final[ResponseFormatJSONObject] = {"type": "json_object"}

# `json_object` mode only guarantees *syntactically* valid JSON — the shape must still be
# pinned in the prompt. It also hard-requires the literal word "JSON" somewhere in the
# messages, which this instruction supplies. The gateway does the parsing and repair.
_JSON_INSTRUCTION: Final[str] = (
    "Reply with a single JSON object that validates against this JSON Schema. "
    "Output only the JSON object: no prose, no markdown fences.\n\nJSON Schema:\n{schema}"
)

# A tool with no parameters still needs a schema OpenAI will accept.
_EMPTY_PARAMETERS: Final[dict[str, object]] = {"type": "object", "properties": {}}


class OpenAIProvider(LLMProviderClient):
    """Chat Completions client. One instance per gateway; the HTTP client is lazy."""

    name: ProviderName = ProviderName.OPENAI

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        api_key = self.settings.openai_api_key.get_secret_value().strip()
        if not api_key:
            # PENDING-CREDENTIALS: a 503 that names the fix, never a fabricated answer.
            raise LLMNotConfiguredError()
        self._api_key = api_key
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                timeout=float(self.settings.llm_timeout_seconds),
                # The gateway owns backoff, jitter and failover; a second retry loop inside
                # the SDK would multiply attempts and blow through the request deadline.
                max_retries=0,
            )
        return self._client

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
        request_messages = _build_messages(messages, system, json_schema)
        request_tools = _to_openai_tools(tools)

        response: ChatCompletion = await self.client.chat.completions.create(
            model=model,
            messages=request_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=request_tools or omit,
            response_format=_JSON_OBJECT_FORMAT if json_schema is not None else omit,
        )

        if not response.choices:
            # Content filtering and some safety stops can return zero choices; treat it as a
            # gateway failure rather than silently handing the caller an empty answer.
            raise LLMGatewayError(
                "The model returned no choices.",
                detail={"provider": self.name.value, "model": model},
            )

        choice = response.choices[0]
        message = choice.message
        usage = response.usage

        text = message.content or ""
        if message.refusal:
            # A refusal arrives on `.refusal` with `.content` empty. Anthropic returns its
            # refusals as an ordinary text block, so dropping this would hand the caller a
            # blank answer on one vendor and a spoken refusal on the other. Log the fact only:
            # the refusal text is the model's, but it can quote the prompt back.
            log.warning("llm_refusal", provider=self.name.value, model=model)
            text = text or message.refusal

        return LLMResult(
            text=text,
            tool_calls=_parse_tool_calls(message),
            usage=TokenUsage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
            ),
            model=response.model or model,
            provider=self.name.value,
            stop_reason=choice.finish_reason,
        )

    def is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, openai.RateLimitError):
            # Checked before APIStatusError: a 429 is retryable despite being below the 5xx floor.
            return True
        if isinstance(exc, openai.APIStatusError):
            return exc.status_code >= _SERVER_ERROR_FLOOR
        # APITimeoutError subclasses APIConnectionError. Both mean the request never produced
        # an answer, so replaying it cannot duplicate a completion we were billed for.
        return isinstance(exc, openai.APIConnectionError)

    def is_rate_limit(self, exc: Exception) -> bool:
        return isinstance(exc, openai.RateLimitError)

    def retry_after_seconds(self, exc: Exception) -> float | None:
        """Read OpenAI's `Retry-After` off the 429 so the wait matches when the quota reopens."""
        if not isinstance(exc, openai.APIStatusError) or exc.response is None:
            return None
        raw = exc.response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            # The header may be an HTTP-date rather than seconds. Rather than parse it, fall back
            # to the caller's exponential backoff.
            return None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


def _build_messages(
    messages: list[dict[str, Any]],
    system: str,
    json_schema: dict[str, Any] | None,
) -> list[ChatCompletionMessageParam]:
    """Prepend the system prompt as a message — OpenAI has no top-level `system` parameter."""
    system_text = system.strip()

    if json_schema is not None:
        instruction = _JSON_INSTRUCTION.format(schema=json.dumps(json_schema, ensure_ascii=False))
        system_text = f"{system_text}\n\n{instruction}" if system_text else instruction

    request_messages: list[ChatCompletionMessageParam] = []
    if system_text:
        request_messages.append({"role": "system", "content": system_text})
    # Callers speak the gateway's role/content dialect, which is already wire-shaped.
    request_messages.extend(cast(list[ChatCompletionMessageParam], messages))
    return request_messages


def _tool_name(tool: dict[str, Any]) -> str:
    return str(tool.get("name") or "").strip()


def _to_openai_tools(tools: list[dict[str, Any]] | None) -> list[ChatCompletionFunctionToolParam]:
    """Translate the gateway's tool schemas into OpenAI's nested function shape."""
    if not tools:
        return []

    converted: list[ChatCompletionFunctionToolParam] = []
    for index, tool in enumerate(tools):
        native = tool.get("function")
        if tool.get("type") == "function" and isinstance(native, dict):
            # Already OpenAI-shaped. It still has to clear the name check below, or OpenAI
            # rejects the whole request — a passthrough is not an excuse to skip validation.
            if not _tool_name(native):
                log.warning(
                    "llm_tool_schema_unnamed", provider=ProviderName.OPENAI.value, index=index
                )
                continue
            converted.append(cast(ChatCompletionFunctionToolParam, tool))
            continue

        name = _tool_name(tool)
        if not name:
            # OpenAI would reject the whole request with a 400; drop the malformed entry and
            # let the model answer with the tools that are usable.
            log.warning("llm_tool_schema_unnamed", provider=ProviderName.OPENAI.value, index=index)
            continue

        # `input_schema` is the Anthropic-native key; `parameters` is OpenAI's own.
        raw_parameters = tool.get("input_schema") or tool.get("parameters")
        parameters = (
            cast(dict[str, object], raw_parameters)
            if isinstance(raw_parameters, dict)
            else _EMPTY_PARAMETERS
        )

        definition: FunctionDefinition = {"name": name, "parameters": parameters}
        description = tool.get("description")
        if isinstance(description, str) and description:
            definition["description"] = description

        converted.append({"type": "function", "function": definition})

    return converted


def _parse_tool_calls(message: ChatCompletionMessage) -> list[ToolCall]:
    """Parse `message.tool_calls`, whose arguments arrive as an unvalidated JSON string."""
    parsed_calls: list[ToolCall] = []
    for raw in message.tool_calls or []:
        # The SDK models tool calls as a function|custom union; DANAH only ever registers
        # function tools, so anything else is a protocol surprise we decline to dispatch.
        if not isinstance(raw, ChatCompletionMessageFunctionToolCall):
            log.warning("llm_tool_call_unsupported", tool_call_id=raw.id, kind=raw.type)
            continue

        try:
            arguments = json.loads(raw.function.arguments or "{}")
        except json.JSONDecodeError:
            # A truncated or malformed argument string is a bad tool call, not a bad response:
            # skip it so the rest of the turn survives. Never log the arguments themselves.
            log.warning(
                "llm_tool_call_invalid_json",
                tool_call_id=raw.id,
                tool=raw.function.name,
                arg_chars=len(raw.function.arguments or ""),
            )
            continue

        if not isinstance(arguments, dict):
            log.warning(
                "llm_tool_call_not_an_object",
                tool_call_id=raw.id,
                tool=raw.function.name,
                json_type=type(arguments).__name__,
            )
            continue

        parsed_calls.append(
            ToolCall(
                id=raw.id,
                name=raw.function.name,
                arguments=cast(dict[str, Any], arguments),
            )
        )

    return parsed_calls
