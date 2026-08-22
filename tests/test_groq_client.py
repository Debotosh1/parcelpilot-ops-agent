"""Groq wiring: response parsing and the tool-call round trip.

Uses a stub in place of the SDK client so the shape of what we send and what we
read back is pinned without spending a request.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm import GroqClient, LLMError


class StubCompletions:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make_client(response) -> tuple[GroqClient, StubCompletions]:
    client = GroqClient.__new__(GroqClient)  # bypass SDK construction
    client.api_key = "test"
    client.model = "llama-3.3-70b-versatile"
    completions = StubCompletions(response)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


def response_with(content="", tool_calls=()):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=list(tool_calls)))],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45),
        model="llama-3.3-70b-versatile",
    )


def test_plain_answer_is_normalised():
    client, stub = make_client(response_with(content="No fee applies."))
    out = client.chat([{"role": "user", "content": "hi"}])
    assert out["content"] == "No fee applies."
    assert out["tool_calls"] == []
    assert out["usage"]["prompt_tokens"] == 120
    assert stub.kwargs["temperature"] == 0.1
    assert "tools" not in stub.kwargs  # no tools passed -> no tool_choice


def test_tool_calls_are_normalised_to_the_loop_format():
    tool_call = SimpleNamespace(
        id="call_abc",
        function=SimpleNamespace(name="evaluate_cancellation", arguments='{"order_id": "ORD-1001"}'),
    )
    client, stub = make_client(response_with(content=None, tool_calls=[tool_call]))
    out = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "x"}}])
    assert out["content"] == ""
    assert out["tool_calls"] == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "evaluate_cancellation", "arguments": '{"order_id": "ORD-1001"}'},
        }
    ]
    assert stub.kwargs["tool_choice"] == "auto"


def test_transport_errors_become_llm_errors():
    client, _ = make_client(RuntimeError("429 rate limit"))
    with pytest.raises(LLMError) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    assert "rate limit" in str(exc.value)


def test_tool_specs_are_valid_function_schemas():
    from app.tools.registry import tool_specs

    specs = tool_specs()
    assert len(specs) == 13
    for spec in specs:
        assert spec["type"] == "function"
        fn = spec["function"]
        assert fn["name"] and len(fn["description"]) > 40
        params = fn["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"])


def test_every_spec_has_a_handler_and_vice_versa():
    from app.tools.registry import HANDLERS, TOOL_FAMILY, tool_specs

    names = {s["function"]["name"] for s in tool_specs()}
    assert names == set(HANDLERS) == set(TOOL_FAMILY)
