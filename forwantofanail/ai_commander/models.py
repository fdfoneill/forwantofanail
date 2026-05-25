from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    data: JsonValue


class CommanderTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: JsonValue = None,
        timeout: float | None = None,
    ) -> TransportResponse: ...


@dataclass(frozen=True)
class CommanderApiError(Exception):
    message: str
    endpoint: str
    method: str
    http_status: int | None = None
    raw_detail: JsonValue = None
    error_code: str = "api_error"
    retryable: bool = False

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "http_status": self.http_status,
            "message": self.message,
            "retryable": self.retryable,
            "endpoint": self.endpoint,
            "method": self.method,
            "raw_detail": self.raw_detail,
        }


@dataclass(frozen=True)
class CommanderTransportError(CommanderApiError):
    pass


@dataclass(frozen=True)
class ToolExecutionResult:
    ok: bool
    tool_name: str
    data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "tool_name": self.tool_name,
        }
        if self.ok:
            payload["data"] = self.data or {}
        else:
            payload["error"] = self.error or {}
        return payload
