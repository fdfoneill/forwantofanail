from __future__ import annotations

import json
import os
from typing import Any

from forwantofanail.ai_commander import CommanderApiClient, CommanderToolRegistry


def execute_response_function_calls(response: Any, registry: CommanderToolRegistry) -> list[dict[str, str]]:
    outputs: list[dict[str, str]] = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "function_call":
            continue
        name = str(getattr(item, "name", "") or "")
        arguments = json.loads(getattr(item, "arguments", "{}") or "{}")
        outputs.append(
            {
                "type": "function_call_output",
                "call_id": str(getattr(item, "call_id")),
                "output": registry.dispatch_json(name, arguments),
            }
        )
    return outputs


def build_example_prompt(brief_context: str) -> str:
    return (
        "You are an AI commander in For Want of a Nail.\n"
        "Your commander brief is supplied below as context, not as a callable tool.\n"
        "Use tools only when you need more detail or must take an action.\n\n"
        f"Commander brief:\n{brief_context}\n\n"
        "Decide what information you need next or what order to issue."
    )


def run_single_turn(
    *,
    openai_client: Any,
    model: str,
    registry: CommanderToolRegistry,
    brief_context: str,
    user_instruction: str,
) -> tuple[Any, list[dict[str, str]], Any | None]:
    tools = registry.get_tools()
    response = openai_client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"{build_example_prompt(brief_context)}\n\nInstruction: {user_instruction}",
                    }
                ],
            }
        ],
        tools=tools,
    )
    tool_outputs = execute_response_function_calls(response, registry)
    final_response = None
    if tool_outputs:
        final_response = openai_client.responses.create(
            model=model,
            previous_response_id=response.id,
            input=tool_outputs,
            tools=tools,
        )
    return response, tool_outputs, final_response


def main() -> None:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("Install the `openai` package to run this example.") from exc

    base_url = os.environ.get("FWOAN_BASE_URL", "http://127.0.0.1:8000")
    commander_name = os.environ.get("FWOAN_COMMANDER_NAME", "Sofonisba")
    brief_context = os.environ.get("FWOAN_BRIEF_CONTEXT", "{}")
    model = os.environ.get("OPENAI_MODEL", "gpt-5")
    instruction = os.environ.get(
        "FWOAN_USER_INSTRUCTION",
        "Review correspondence options and determine whether you should send or read any letters first.",
    )

    fw_client = CommanderApiClient(base_url=base_url, commander_name=commander_name)
    registry = CommanderToolRegistry(fw_client)
    openai_client = OpenAI()

    initial_response, tool_outputs, final_response = run_single_turn(
        openai_client=openai_client,
        model=model,
        registry=registry,
        brief_context=brief_context,
        user_instruction=instruction,
    )

    print("Initial response:")
    print(getattr(initial_response, "output_text", ""))
    if tool_outputs:
        print("\nTool outputs returned to model:")
        print(json.dumps(tool_outputs, indent=2))
    if final_response is not None:
        print("\nFinal response:")
        print(getattr(final_response, "output_text", ""))


if __name__ == "__main__":
    main()
