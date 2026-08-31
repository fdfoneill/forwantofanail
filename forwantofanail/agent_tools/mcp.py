from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from forwantofanail.core.database import create_session
from forwantofanail.core.models import AuthToken

from .registry import get_tool, invoke, list_tools
from .security import token_binding
from .services import ToolContext, ToolInvocationError


router = APIRouter()
PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"


def _error(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _bearer(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token.strip().strip('"')


def _tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": definition.name,
            "description": definition.description,
            "inputSchema": definition.input_model.model_json_schema(),
            "outputSchema": definition.output_model.model_json_schema(),
        }
        for definition in list_tools()
    ]


@router.post("/mcp")
def mcp_endpoint(
    envelope: dict[str, Any],
    authorization: str = Header(default=""),
    mcp_protocol_version: str | None = Header(default=None, alias="MCP-Protocol-Version"),
    mcp_method: str | None = Header(default=None, alias="Mcp-Method"),
):
    """Stateless JSON-response MCP endpoint backed by the canonical registry.

    The deployment dependency includes the official Python MCP SDK for host and
    client interoperability. This deliberately small ASGI adapter keeps the game
    facade's existing bearer sessions as the sole authentication authority and
    serves both modern stateless calls and legacy initialize/tools requests.
    """
    raw_token = _bearer(authorization)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    session = create_session()
    try:
        auth = session.get(AuthToken, token_hash)
        if auth is None or auth.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Invalid bearer token")
        request_id = envelope.get("id")
        method = envelope.get("method")
        if mcp_protocol_version == PROTOCOL_VERSION and mcp_method != method:
            return JSONResponse(
                status_code=400,
                content=_error(request_id, -32021, "MCP method header does not match the request body."),
            )
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": [PROTOCOL_VERSION],
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": "Commander tools are diegetic. Opaque references are tool-call handles and must never be quoted in letters.",
                "ttlMs": 60000,
                "cacheScope": "private",
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": "for-want-of-a-nail",
                        "version": "1.0.0",
                    }
                },
            }
        elif method == "initialize":
            result = {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "for-want-of-a-nail", "version": "1.0.0"},
                "instructions": "Commander tools are diegetic. Opaque references are tool-call handles and must never be quoted in letters.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {
                "resultType": "complete",
                "tools": _tool_list(),
                "ttlMs": 60000,
                "cacheScope": "private",
            }
        elif method == "tools/call":
            params = envelope.get("params") or {}
            name = str(params.get("name") or "")
            definition = get_tool(name)
            if definition is None:
                return JSONResponse(_error(request_id, -32601, "Unknown commander tool."))
            identity_payload = json.dumps(
                [request_id, name, params.get("arguments") or {}],
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            request_identity = "mcp-" + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
            try:
                value = invoke(
                    name,
                    params.get("arguments") or {},
                    ToolContext(
                        session=session,
                        commander_id=int(auth.commander_id),
                        session_binding=token_binding(raw_token),
                        request_identity=request_identity,
                    ),
                )
            except ToolInvocationError as exc:
                result = {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": exc.message}],
                    "structuredContent": {"error": exc.payload()},
                    "isError": True,
                }
            else:
                result = {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": json.dumps(value, default=str)}],
                    "structuredContent": value,
                    "isError": False,
                }
        elif method in {"notifications/initialized", "notifications/cancelled"}:
            return JSONResponse(status_code=202, content={})
        else:
            return JSONResponse(_error(request_id, -32601, "Method not found."))
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            headers={"MCP-Protocol-Version": mcp_protocol_version or PROTOCOL_VERSION},
        )
    finally:
        session.close()


@router.get("/mcp")
def mcp_get_not_supported():
    return JSONResponse(status_code=405, content={"error": "Stateless JSON-response MCP accepts POST requests."})
