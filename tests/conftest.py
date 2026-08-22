from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import Services  # noqa: E402


class FakeLLM:
    """Scripted LLM so the agent loop can be tested without network or cost.

    `script` is a list of assistant turns: either {"content": "..."} for a final
    answer, or {"tool": name, "arguments": {...}} for a tool call.
    """

    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        turn = self.script.pop(0) if self.script else {"content": "done"}
        if "tool" in turn:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{len(self.calls)}",
                        "type": "function",
                        "function": {
                            "name": turn["tool"],
                            "arguments": json.dumps(turn.get("arguments", {})),
                        },
                    }
                ],
                "model": "fake-model",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        return {
            "role": "assistant",
            "content": turn.get("content", ""),
            "tool_calls": [],
            "model": "fake-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


@pytest.fixture()
def services() -> Services:
    return Services.build(llm=None)


@pytest.fixture()
def engine(services):
    return services.engine
