"""Groq chat-completions wrapper.

Groq is used for one job only: reading the request, choosing tools, and
narrating what the deterministic layer returned. It never supplies policy
numbers of its own.

`llama-3.3-70b-versatile` is the default because it supports parallel tool
calling, is fast enough for a support console (sub-second first token on Groq),
and follows the "call the evaluator, don't do the maths" instruction reliably.
Override with GROQ_MODEL.
"""

from __future__ import annotations

from typing import Any, Protocol

from .config import settings


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        ...


class GroqClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or settings.groq_api_key
        self.model = model or settings.groq_model
        if not self.api_key:
            raise LLMError(
                "GROQ_API_KEY is not set. Create a key at https://console.groq.com/keys and export it."
            )
        try:
            from groq import Groq  # imported lazily so the data layer works without the SDK
        except ImportError as exc:  # pragma: no cover
            raise LLMError("The 'groq' package is not installed. Run: pip install -r requirements.txt") from exc
        self._client = Groq(api_key=self.api_key, timeout=settings.groq_timeout_s)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": settings.groq_temperature,
            "max_tokens": settings.groq_max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # network / rate limit / model errors
            raise LLMError(f"Groq request failed: {type(exc).__name__}: {exc}") from exc

        choice = response.choices[0].message
        return {
            "role": "assistant",
            "content": choice.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in (choice.tool_calls or [])
            ],
            "usage": {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", None),
                "completion_tokens": getattr(response.usage, "completion_tokens", None),
            },
            "model": response.model,
        }


def build_default_client() -> LLMClient | None:
    if not settings.llm_enabled:
        return None
    return GroqClient()
