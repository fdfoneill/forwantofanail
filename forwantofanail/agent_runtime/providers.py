from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from .context import AgentProfile


@dataclass(frozen=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    content: str = ""
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str | None = None
    continuation: Any = None


class ModelAdapter:
    def invoke(self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]], profile: AgentProfile) -> ModelTurn:
        raise NotImplementedError


def function_tools(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "type": "function",
        "function": {
            "name": row["name"],
            "description": row["description"],
            "parameters": row["input_schema"],
        },
    } for row in tool_schemas]


class OpenAIAdapter(ModelAdapter):
    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - configuration error
            raise RuntimeError("The openai package is required for OpenAI agent profiles") from exc
        self.client = OpenAI()

    def invoke(self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]], profile: AgentProfile) -> ModelTurn:
        response = self.client.responses.create(
            model=profile.model,
            input=_openai_input(messages),
            tools=[{
                "type": "function", "name": row["name"], "description": row["description"],
                "parameters": row["input_schema"], "strict": True,
            } for row in tool_schemas],
            parallel_tool_calls=False,
            max_output_tokens=profile.max_output_tokens_per_turn,
            temperature=profile.temperature,
            store=False,
            timeout=profile.request_timeout_seconds,
        )
        calls: list[ModelToolCall] = []
        for item in response.output:
            if getattr(item, "type", None) == "function_call":
                raw = getattr(item, "arguments", "{}") or "{}"
                calls.append(ModelToolCall(
                    call_id=str(getattr(item, "call_id", getattr(item, "id", "call"))),
                    name=str(item.name), arguments=json.loads(raw),
                ))
        usage = getattr(response, "usage", None)
        return ModelTurn(
            content=str(getattr(response, "output_text", "") or ""),
            tool_calls=calls,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            finish_reason=str(getattr(response, "status", "") or "") or None,
        )


def _openai_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        kind = message.get("kind", "message")
        if kind == "tool_call":
            result.append({
                "type": "function_call", "call_id": message["call_id"],
                "name": message["name"], "arguments": json.dumps(message.get("arguments", {})),
            })
        elif kind == "tool_result":
            result.append({
                "type": "function_call_output", "call_id": message["call_id"],
                "output": json.dumps(message.get("result"), ensure_ascii=False),
            })
        else:
            result.append({"role": message.get("role", "user"), "content": message.get("content", "")})
    return result


class OllamaAdapter(ModelAdapter):
    def __init__(self, host: str | None = None, timeout: float = 300) -> None:
        try:
            from ollama import Client
        except ImportError as exc:  # pragma: no cover - configuration error
            raise RuntimeError("The ollama package is required for Ollama agent profiles") from exc
        self.client = Client(host=host, timeout=timeout)

    def invoke(self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]], profile: AgentProfile) -> ModelTurn:
        response = self.client.chat(
            model=profile.model,
            messages=_ollama_messages(messages),
            tools=function_tools(tool_schemas),
            stream=False,
            options={"temperature": profile.temperature, "num_predict": profile.max_output_tokens_per_turn},
        )
        message = response.message
        calls = [ModelToolCall(
            call_id=f"ollama-{index}-{call.function.name}",
            name=str(call.function.name), arguments=dict(call.function.arguments or {}),
        ) for index, call in enumerate(message.tool_calls or [])]
        return ModelTurn(
            content=str(message.content or ""), tool_calls=calls,
            input_tokens=int(getattr(response, "prompt_eval_count", 0) or 0),
            output_tokens=int(getattr(response, "eval_count", 0) or 0),
            finish_reason=str(getattr(response, "done_reason", "") or "") or None,
        )


def _ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        kind = message.get("kind", "message")
        if kind == "tool_call":
            result.append({
                "role": "assistant", "content": "", "tool_calls": [{
                    "type": "function", "function": {
                        "name": message["name"], "arguments": message.get("arguments", {})
                    },
                }],
            })
        elif kind == "tool_result":
            result.append({
                "role": "tool", "tool_name": message["name"],
                "content": json.dumps(message.get("result"), ensure_ascii=False),
            })
        else:
            result.append({"role": message.get("role", "user"), "content": message.get("content", "")})
    return result


def adapter_for(profile: AgentProfile) -> ModelAdapter:
    if profile.provider == "openai":
        return OpenAIAdapter()
    if profile.provider == "ollama":
        import os
        return OllamaAdapter(
            os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            timeout=profile.request_timeout_seconds,
        )
    raise RuntimeError(f"Unsupported model provider {profile.provider!r}")
