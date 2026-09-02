from __future__ import annotations

from copy import deepcopy
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


def _describe_openai_default(schema: dict[str, Any], default: Any) -> None:
    rendered = json.dumps(default, ensure_ascii=False, separators=(",", ":"))
    instruction = f"When no alternative is intended, use {rendered}."
    existing = str(schema.get("description") or "").strip()
    schema["description"] = f"{existing} {instruction}".strip()


def _openai_strict_schema(raw_schema: dict[str, Any]) -> dict[str, Any]:
    """Compile canonical Pydantic JSON Schema for OpenAI strict tools.

    The canonical schema remains untouched for HTTP, MCP, and Ollama. OpenAI
    strict mode requires every object property to be required, represents
    optional values with null, and does not use Pydantic's default annotations.
    """

    def normalize(value: Any) -> Any:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value

        original_required = set(value.get("required", []))
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"default", "discriminator"}:
                continue
            normalized_key = "anyOf" if key == "oneOf" else key
            result[normalized_key] = normalize(item)

        properties = result.get("properties")
        if isinstance(properties, dict):
            source_properties = value.get("properties", {})
            for name, property_schema in properties.items():
                if name in original_required or not isinstance(property_schema, dict):
                    continue
                source = source_properties.get(name, {})
                if isinstance(source, dict) and "default" in source:
                    _describe_openai_default(property_schema, source["default"])
                elif property_schema.get("type") == "array":
                    _describe_openai_default(property_schema, [])
            result["required"] = list(properties)
            result["additionalProperties"] = False
        return result

    return normalize(deepcopy(raw_schema))


def openai_function_tools(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "type": "function",
        "name": row["name"],
        "description": row["description"],
        "parameters": _openai_strict_schema(row["input_schema"]),
        "strict": True,
    } for row in tool_schemas]


class OpenAIAdapter(ModelAdapter):
    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - configuration error
            raise RuntimeError("The openai package is required for OpenAI agent profiles") from exc
        self.client = OpenAI()

    def invoke(self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]], profile: AgentProfile) -> ModelTurn:
        request: dict[str, Any] = dict(
            model=profile.model,
            input=_openai_input(messages),
            tools=openai_function_tools(tool_schemas),
            parallel_tool_calls=False,
            max_output_tokens=profile.max_output_tokens_per_turn,
            store=False,
            timeout=profile.request_timeout_seconds,
        )
        if profile.temperature is not None:
            request["temperature"] = profile.temperature
        response = self.client.responses.create(**request)
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
        options: dict[str, Any] = {"num_predict": profile.max_output_tokens_per_turn}
        if profile.temperature is not None:
            options["temperature"] = profile.temperature
        response = self.client.chat(
            model=profile.model,
            messages=_ollama_messages(messages),
            tools=function_tools(tool_schemas),
            stream=False,
            options=options,
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
