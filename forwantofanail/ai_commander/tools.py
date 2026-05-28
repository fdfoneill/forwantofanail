from __future__ import annotations

import json
from typing import Any, Callable

from .client import CommanderApiClient
from .models import CommanderApiError, ToolExecutionResult


ToolHandler = Callable[..., Any]


def _nullable_schema(base_type: str, **extra: Any) -> dict[str, Any]:
    return {"type": [base_type, "null"], **extra}


def _tool(
    *,
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


class CommanderToolRegistry:
    def __init__(self, client: CommanderApiClient):
        self.client = client
        self._handlers: dict[str, ToolHandler] = {
            "list_correspondents": self.client.list_correspondents,
            "list_messages": self.client.list_messages,
            "read_message": self.client.read_message,
            "send_message": self.client.send_message,
            "get_current_action": self.client.get_current_action,
            "cancel_action": self.client.cancel_action,
            "create_action": self.client.create_action,
            "plan_actions": self.client.plan_actions,
            "get_valid_next_destinations": self.client.get_valid_next_destinations,
            "get_valid_attack_targets": self.client.get_valid_attack_targets,
            "get_valid_besiege_targets": self.client.get_valid_besiege_targets,
            "get_standing_orders": self.client.get_standing_orders,
            "set_follow_road": self.client.set_follow_road,
            "set_forced_march": self.client.set_forced_march,
            "list_alerts": self.client.list_alerts,
            "get_border_roads": self.client.get_border_roads,
            "list_known_strongholds": self.client.list_known_strongholds,
            "get_stronghold_route": self.client.get_stronghold_route,
        }

    def get_tools(self) -> list[dict[str, Any]]:
        return [
            _tool(
                name="list_correspondents",
                description="List other commanders who can receive letters, including their faction.",
                properties={},
            ),
            _tool(
                name="list_messages",
                description="List delivered letters in the commander's inbox.",
                properties={
                    "unread_only": {"type": "boolean", "description": "If true, only return unread letters."},
                },
                required=["unread_only"],
            ),
            _tool(
                name="read_message",
                description="Read the full contents of a delivered letter by message ID.",
                properties={
                    "message_id": {"type": "string", "description": "Message ID such as msg_12."},
                },
                required=["message_id"],
            ),
            _tool(
                name="send_message",
                description="Send a letter to another commander.",
                properties={
                    "recipient_id": {"type": "string", "description": "Recipient commander ID such as cmd_3."},
                    "content": {"type": "string", "description": "Full letter body to send."},
                    "priority": _nullable_schema("string", description="Letter priority, usually normal or high."),
                },
                required=["recipient_id", "content", "priority"],
            ),
            _tool(
                name="get_current_action",
                description="Get the commander's current in-progress or queued action state.",
                properties={},
            ),
            _tool(
                name="cancel_action",
                description="Cancel an active or queued action by action ID.",
                properties={
                    "action_id": {"type": "string", "description": "Action ID such as act_5."},
                },
                required=["action_id"],
            ),
            _tool(
                name="create_action",
                description="Issue a direct order to move, forage, attack, or besiege.",
                properties={
                    "kind": {
                        "type": "string",
                        "enum": ["move", "forage", "attack", "besiege"],
                        "description": "Kind of order to issue.",
                    },
                    "destination_h3": _nullable_schema("string", description="Required for move orders."),
                    "target_h3": _nullable_schema("string", description="Required for attack orders."),
                    "target_army_id": _nullable_schema("string", description="Required for attack orders."),
                    "target_stronghold_id": _nullable_schema("string", description="Required for besiege orders."),
                },
                required=["kind", "destination_h3", "target_h3", "target_army_id", "target_stronghold_id"],
            ),
            _tool(
                name="plan_actions",
                description="Queue a march path, hold, or forage plan.",
                properties={
                    "kind": {
                        "type": "string",
                        "enum": ["march", "forage"],
                        "description": "Plan kind.",
                    },
                    "path": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered H3 path for march. Empty path means hold when kind is march.",
                    },
                },
                required=["kind", "path"],
            ),
            _tool(
                name="get_valid_next_destinations",
                description="List valid adjacent march destinations from the current location or a supplied origin.",
                properties={
                    "origin_h3": _nullable_schema("string", description="Optional origin H3 to validate from."),
                },
                required=["origin_h3"],
            ),
            _tool(
                name="get_valid_attack_targets",
                description="List valid adjacent or siege-context attack targets.",
                properties={
                    "origin_h3": _nullable_schema("string", description="Optional origin H3 to validate from."),
                },
                required=["origin_h3"],
            ),
            _tool(
                name="get_valid_besiege_targets",
                description="List valid adjacent strongholds that can currently be besieged.",
                properties={
                    "origin_h3": _nullable_schema("string", description="Optional origin H3 to validate from."),
                },
                required=["origin_h3"],
            ),
            _tool(
                name="get_standing_orders",
                description="Get the commander's current standing-order settings.",
                properties={},
            ),
            _tool(
                name="set_follow_road",
                description="Enable or disable the follow-road standing order.",
                properties={
                    "enabled": {"type": "boolean", "description": "True to enable, false to disable."},
                },
                required=["enabled"],
            ),
            _tool(
                name="set_forced_march",
                description="Enable or disable the forced-march standing order.",
                properties={
                    "enabled": {"type": "boolean", "description": "True to enable, false to disable."},
                },
                required=["enabled"],
            ),
            _tool(
                name="list_alerts",
                description="List delivered alerts and reports for the commander.",
                properties={
                    "limit": {"type": "integer", "description": "Maximum alerts to return."},
                    "unread_only": {"type": "boolean", "description": "If true, only return unread alerts."},
                },
                required=["limit", "unread_only"],
            ),
            _tool(
                name="get_border_roads",
                description="Return road cells just beyond a visible frontier of cells.",
                properties={
                    "cells": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Visible H3 cells to inspect for off-map road continuations.",
                    },
                },
                required=["cells"],
            ),
            _tool(
                name="list_known_strongholds",
                description="Look up named strongholds by ID, faction, region, or partial name search.",
                properties={
                    "stronghold_id": _nullable_schema("string", description="Optional exact stronghold ID such as sh_3."),
                    "faction": _nullable_schema("string", description="Optional controlling faction filter."),
                    "region": _nullable_schema("string", description="Optional region filter."),
                    "search": _nullable_schema("string", description="Optional partial stronghold name search."),
                },
                required=["stronghold_id", "faction", "region", "search"],
            ),
            _tool(
                name="get_stronghold_route",
                description="Find a route between two strongholds, optionally avoiding named strongholds.",
                properties={
                    "from_stronghold_id": {"type": "string", "description": "Origin stronghold ID such as sh_1."},
                    "to_stronghold_id": {"type": "string", "description": "Destination stronghold ID such as sh_4."},
                    "avoid_stronghold_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Intermediate stronghold IDs to avoid.",
                    },
                    "on_road": {"type": "boolean", "description": "If true, constrain the route to road and stronghold cells."},
                },
                required=["from_stronghold_id", "to_stronghold_id", "avoid_stronghold_ids", "on_road"],
            ),
        ]

    def dispatch(self, tool_name: str, arguments: dict[str, Any] | None = None) -> ToolExecutionResult:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return ToolExecutionResult(
                ok=False,
                tool_name=tool_name,
                error={
                    "error_code": "unknown_tool",
                    "http_status": None,
                    "message": f"Unknown tool: {tool_name}",
                    "retryable": False,
                    "raw_detail": {"tool_name": tool_name},
                },
            )
        args = arguments or {}
        try:
            result = handler(**args)
            return ToolExecutionResult(
                ok=True,
                tool_name=tool_name,
                data={
                    "result": result,
                    "raw": result,
                },
            )
        except CommanderApiError as exc:
            return ToolExecutionResult(
                ok=False,
                tool_name=tool_name,
                error=exc.to_dict(),
            )
        except TypeError as exc:
            return ToolExecutionResult(
                ok=False,
                tool_name=tool_name,
                error={
                    "error_code": "tool_argument_error",
                    "http_status": None,
                    "message": str(exc),
                    "retryable": False,
                    "raw_detail": {"arguments": args},
                },
            )

    def dispatch_json(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        return json.dumps(self.dispatch(tool_name, arguments).to_dict(), separators=(",", ":"), sort_keys=True)
