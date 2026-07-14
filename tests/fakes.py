"""Fake LLM gateway and embedder.

These substitute at the *interface* the production code depends on, so agents, the
orchestrator, the retriever, the composer and the API layer all run their real code in tests.
Nothing here is importable from `app/` — it exists only under `tests/`.

The embedder is deterministic and *semantically meaningful*: vectors are derived from a bag of
words, so a query about "data residency" genuinely scores higher against the chunk that
mentions data residency than against one about digital skills. That makes retrieval tests real
tests rather than tautologies.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

_TOKEN_RE = re.compile(r"[a-z0-9؀-ۿ]+")


def _tokenise(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class FakeEmbedder:
    """Deterministic hashing embedder with real cosine-similarity behaviour."""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _tokenise(text)
        if not tokens:
            # A zero vector would make cosine distance undefined in pgvector.
            vec[0] = 1.0
            return vec
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.calls.append([text])
        return self._vector(text)

    @property
    def dimension(self) -> int:
        return self.dim

    @property
    def model(self) -> str:
        return "fake-embedder"

    @property
    def provider(self) -> str:
        return "fake"


@dataclass
class _Scripted:
    """A queued response, optionally matched to a substring of the prompt."""

    payload: Any
    match: str | None = None
    used: bool = False


@dataclass
class FakeLLMGateway:
    """Records calls and returns scripted or heuristic responses.

    Default behaviour, when nothing is scripted:
      * If a JSON schema is requested, synthesise a minimal valid instance of it. This lets
        agent tests run end-to-end without hand-writing a payload for every agent.
      * Otherwise, answer from the numbered sources in the prompt, citing `[1]`. If the prompt
        contains no sources, abstain — mirroring the grounding contract, so the
        "out-of-corpus → abstain" acceptance criterion is genuinely exercised.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)
    scripted: list[_Scripted] = field(default_factory=list)
    fail_times: int = 0
    _failures: int = 0

    ABSTAIN = "The provided sources do not contain information to answer this question."

    # -- scripting ---------------------------------------------------------
    def push(self, payload: Any, *, match: str | None = None) -> None:
        self.scripted.append(_Scripted(payload=payload, match=match))

    def _take(self, prompt: str, *, structured: bool) -> Any | None:
        """Hand back the next scripted payload whose *shape* fits this call.

        An agent turn is not one call. `BaseAgent` first runs a tool-use loop — a plain
        `complete()` with tools and no schema — and only then makes the structured call. A queue
        that serves the next payload to whoever asks first lets that tool-loop call swallow the
        payload scripted for the structured call: the agent then falls back to a synthesised
        empty output, and the test fails claiming the pipeline produced no insight when in truth
        the fake mis-delivered the one it was given. So a dict payload answers only a structured
        call, and a string payload only a text call.
        """
        for item in self.scripted:
            if item.used:
                continue
            if item.match is not None and item.match.lower() not in prompt.lower():
                continue
            if structured != (not isinstance(item.payload, str)):
                continue
            item.used = True
            return item.payload
        return None

    # -- gateway interface -------------------------------------------------
    async def complete_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: Any,
        system: str = "",
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        purpose: str = "agent",
        user_id: Any = None,
    ) -> tuple[Any, Any]:
        """Mirror `DefaultLLMGateway.complete_structured`: validated instance + raw result.

        The agents call this, not `complete()`, so the fake must honour the same contract — parse
        the reply into the Pydantic schema and let a malformed reply raise, exactly as production
        does after its repair attempt fails.
        """
        from pydantic import ValidationError

        from app.exceptions import LLMGatewayError

        result = await self.complete(
            messages,
            system=system,
            model=model,
            max_tokens=max_tokens if max_tokens is not None else 2048,
            temperature=temperature if temperature is not None else 0.2,
            json_schema=schema.model_json_schema(),
            purpose=purpose,
            user_id=user_id,
        )

        try:
            parsed = schema.model_validate_json(result.text)
        except (ValidationError, ValueError) as exc:
            raise LLMGatewayError(
                "The model did not return output matching the required schema.",
                detail={"schema": schema.__name__, "error": str(exc)[:300]},
            ) from exc

        return parsed, result

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        json_schema: dict[str, Any] | None = None,
        purpose: str = "chat",
        user_id: Any = None,
    ) -> Any:
        from app.services.llm.gateway import LLMResult, TokenUsage

        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        self.calls.append(
            {
                "messages": messages,
                "system": system,
                "tools": tools,
                "model": model,
                "json_schema": json_schema,
                "purpose": purpose,
                "prompt": prompt,
            }
        )

        if self._failures < self.fail_times:
            self._failures += 1
            from app.exceptions import LLMGatewayError

            raise LLMGatewayError("Simulated provider failure")

        scripted = self._take(prompt, structured=json_schema is not None)
        if scripted is not None:
            text = scripted if isinstance(scripted, str) else json.dumps(scripted)
        elif json_schema is not None:
            text = json.dumps(_synthesise(json_schema, prompt))
        else:
            text = self._answer(prompt)

        return LLMResult(
            text=text,
            tool_calls=[],
            usage=TokenUsage(
                input_tokens=max(1, len(prompt) // 4),
                output_tokens=max(1, len(text) // 4),
            ),
            model=model or "fake-model",
            provider="fake",
            latency_ms=1,
            stop_reason="end_turn",
        )

    def _answer(self, prompt: str) -> str:
        if "[1]" not in prompt:
            return self.ABSTAIN
        # Echo a sentence from the first numbered source and cite it, so citation
        # extraction is genuinely tested.
        match = re.search(r"\[1\]\s*(?:[^\n]*\n)?(.+)", prompt)
        snippet = (match.group(1).strip()[:160] if match else "the corpus").rstrip()
        return f"According to the available sources, {snippet} [1]"


def _synthesise(schema: dict[str, Any], prompt: str, root: dict[str, Any] | None = None) -> Any:
    """Build a minimal instance satisfying a JSON schema (objects, arrays, scalars, enums).

    `$ref` and `anyOf` must be resolved, not skipped. Pydantic emits a `$ref` into `$defs` for
    every nested model and an `anyOf` for every `Optional[...]`, so a synthesiser that ignores
    them quietly emits a string where an object belongs — which then fails schema validation and
    surfaces as a bogus `llm_gateway_error` from the agent, hiding whatever the test meant to
    assert. `RiskOutput.insights: list[RiskInsight]` is exactly that case.
    """
    root = root if root is not None else schema

    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        target = root.get("$defs", {}).get(name)
        if target is None:
            raise KeyError(f"FakeLLMGateway cannot resolve {ref!r}; is it defined in $defs?")
        return _synthesise(target, prompt, root)

    # Optional[X] / unions: take the first branch that is not the null arm.
    for key in ("anyOf", "oneOf"):
        if key in schema:
            branches = [b for b in schema[key] if b.get("type") != "null"]
            chosen = branches[0] if branches else schema[key][0]
            return _synthesise(chosen, prompt, root)

    kind = schema.get("type")

    if "enum" in schema:
        enum_values = schema["enum"]
        return enum_values[0]

    if kind == "object":
        # Every property, not just the required ones. Pydantic marks any field with a default as
        # optional, and the agent outputs lean on defaults heavily — `RiskOutput.insights` and
        # `DraftInsight.citations` both default to []. Synthesising only the required fields
        # therefore yields a *valid but empty* insight set, and the pipeline tests see a run that
        # succeeds while producing nothing. An empty draft is indistinguishable from an honest
        # "no risk found", so the failure looks like a product bug rather than a hollow fake.
        props: dict[str, Any] = schema.get("properties", {})
        return {name: _synthesise(sub, prompt, root) for name, sub in props.items()}

    if kind == "array":
        items = schema.get("items", {"type": "string"})
        count = max(1, int(schema.get("minItems", 1)))
        return [_synthesise(items, prompt, root) for _ in range(count)]

    if kind == "integer":
        lo = int(schema.get("minimum", 1))
        hi = int(schema.get("maximum", max(lo, 5)))
        return max(lo, min(hi, 3))

    if kind == "number":
        lo = float(schema.get("minimum", 0.0))
        hi = float(schema.get("maximum", 1.0))
        mid = (lo + hi) / 2
        return round(mid, 2)

    if kind == "boolean":
        return True

    if kind == "null":
        return None

    # `format` is part of the contract, not decoration: a field typed as a UUID rejects the
    # description text the fallback would otherwise return. Where the schema wants an id, reuse
    # one from the prompt if it is there — the Signal agent triages the item ids it was given,
    # so a random id would parse and then silently match nothing.
    fmt = schema.get("format")
    if fmt == "uuid":
        found = _UUID_RE.search(prompt)
        return found.group(0) if found else str(uuid.uuid4())
    if fmt == "date-time":
        return datetime.now(UTC).isoformat()
    if fmt == "date":
        return datetime.now(UTC).date().isoformat()
    if fmt == "email":
        return "analyst@ministry.gov"
    if fmt in {"uri", "url"}:
        return "https://example.gov/source"

    return schema.get("description", "synthesised") or "synthesised"
