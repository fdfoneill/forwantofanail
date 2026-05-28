from __future__ import annotations

import json
from typing import Any, Mapping
from urllib import error, parse, request

from .models import (
    CommanderApiError,
    CommanderTransport,
    CommanderTransportError,
    JsonValue,
    TransportResponse,
)


def _strip_trailing_slash(value: str) -> str:
    return value[:-1] if value.endswith("/") else value


class UrllibCommanderTransport:
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: JsonValue = None,
        timeout: float | None = None,
    ) -> TransportResponse:
        query_url = url
        if params:
            encoded = parse.urlencode(
                {
                    key: ",".join(str(item) for item in value) if isinstance(value, list) else value
                    for key, value in params.items()
                    if value is not None
                }
            )
            query_url = f"{url}?{encoded}"
        body: bytes | None = None
        final_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            final_headers.setdefault("Content-Type", "application/json")
        req = request.Request(query_url, data=body, headers=final_headers, method=method.upper())
        try:
            with request.urlopen(req, timeout=timeout) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
                data = json.loads(payload.decode("utf-8")) if "application/json" in content_type else payload.decode("utf-8")
                return TransportResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    data=data,
                )
        except error.HTTPError as exc:
            payload = exc.read()
            data: JsonValue
            try:
                data = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = payload.decode("utf-8", errors="replace")
            return TransportResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()),
                data=data,
            )
        except error.URLError as exc:
            raise CommanderTransportError(
                message=f"Network error calling {method.upper()} {url}: {exc.reason}",
                endpoint=parse.urlsplit(url).path or url,
                method=method.upper(),
                http_status=None,
                raw_detail={"reason": str(exc.reason)},
                error_code="transport_error",
                retryable=True,
            ) from exc


class CommanderApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        commander_name: str | None = None,
        token: str | None = None,
        transport: CommanderTransport | None = None,
        timeout: float = 30.0,
    ):
        if not base_url or not str(base_url).strip():
            raise ValueError("base_url is required")
        if bool(commander_name) == bool(token):
            raise ValueError("Provide exactly one of commander_name or token")
        self.base_url = _strip_trailing_slash(str(base_url).strip())
        self.timeout = float(timeout)
        self.transport = transport or UrllibCommanderTransport()
        self.commander_name = commander_name
        self.token = token
        if self.token is None and commander_name is not None:
            self.login(commander_name)

    def login(self, commander_name: str) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/v1/auth/login",
            json_body={"commander_name": commander_name},
            use_auth=False,
        )
        self.token = str(payload.get("token") or "")
        commander = payload.get("commander") or {}
        self.commander_name = str(commander.get("name") or commander_name)
        return payload

    def list_correspondents(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v1/correspondents")
        return list(response) if isinstance(response, list) else []

    def get_brief(self) -> dict[str, Any]:
        response = self._request("GET", "/v1/me/brief")
        return dict(response) if isinstance(response, dict) else {}

    def list_messages(self, *, unread_only: bool = False) -> list[dict[str, Any]]:
        response = self._request("GET", "/v1/me/messages", params={"unread_only": unread_only})
        return list(response) if isinstance(response, list) else []

    def read_message(self, message_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/v1/me/messages/{message_id}")
        return dict(response) if isinstance(response, dict) else {}

    def send_message(self, recipient_id: str, content: str, *, priority: str | None = "normal") -> dict[str, Any]:
        response = self._request(
            "POST",
            "/v1/me/messages",
            json_body={
                "recipient_id": recipient_id,
                "content": content,
                "priority": priority or "normal",
            },
        )
        return dict(response) if isinstance(response, dict) else {}

    def get_current_action(self) -> dict[str, Any] | None:
        response = self._request("GET", "/v1/me/actions/current")
        return dict(response) if isinstance(response, dict) else None

    def cancel_action(self, action_id: str) -> dict[str, Any]:
        response = self._request("POST", f"/v1/me/actions/{action_id}/cancel")
        return dict(response) if isinstance(response, dict) else {}

    def create_action(
        self,
        *,
        kind: str,
        destination_h3: str | None = None,
        target_h3: str | None = None,
        target_army_id: str | None = None,
        target_stronghold_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "kind": kind,
            "destination_h3": destination_h3,
            "target_h3": target_h3,
            "target_army_id": target_army_id,
            "target_stronghold_id": target_stronghold_id,
        }
        response = self._request("POST", "/v1/me/actions", json_body=payload)
        return dict(response) if isinstance(response, dict) else {}

    def plan_actions(self, *, kind: str, path: list[str]) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/v1/me/actions/plan",
            json_body={"kind": kind, "path": list(path)},
        )
        return dict(response) if isinstance(response, dict) else {}

    def get_valid_next_destinations(self, *, origin_h3: str | None = None) -> dict[str, Any]:
        response = self._request("GET", "/v1/me/actions/valid-next", params={"origin_h3": origin_h3})
        return dict(response) if isinstance(response, dict) else {}

    def get_valid_attack_targets(self, *, origin_h3: str | None = None) -> dict[str, Any]:
        response = self._request("GET", "/v1/me/actions/valid-attack", params={"origin_h3": origin_h3})
        return dict(response) if isinstance(response, dict) else {}

    def get_valid_besiege_targets(self, *, origin_h3: str | None = None) -> dict[str, Any]:
        response = self._request("GET", "/v1/me/actions/valid-besiege", params={"origin_h3": origin_h3})
        return dict(response) if isinstance(response, dict) else {}

    def get_standing_orders(self) -> dict[str, Any]:
        response = self._request("GET", "/v1/me/orders/standing")
        return dict(response) if isinstance(response, dict) else {}

    def set_follow_road(self, *, enabled: bool) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/v1/me/orders/standing/follow-road",
            json_body={"enabled": bool(enabled)},
        )
        return dict(response) if isinstance(response, dict) else {}

    def set_forced_march(self, *, enabled: bool) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/v1/me/orders/standing/forced-march",
            json_body={"enabled": bool(enabled)},
        )
        return dict(response) if isinstance(response, dict) else {}

    def list_alerts(self, *, limit: int = 25, unread_only: bool = False) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            "/v1/me/alerts",
            params={"limit": int(limit), "unread_only": unread_only},
        )
        return list(response) if isinstance(response, list) else []

    def get_border_roads(self, *, cells: list[str]) -> dict[str, Any]:
        response = self._request("GET", "/v1/me/roads/border", params={"cells": list(cells)})
        return dict(response) if isinstance(response, dict) else {}

    def list_known_strongholds(
        self,
        *,
        stronghold_id: str | None = None,
        faction: str | None = None,
        region: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/v1/me/geography/strongholds",
            params={
                "stronghold_id": stronghold_id,
                "faction": faction,
                "region": region,
                "search": search,
            },
        )
        return dict(response) if isinstance(response, dict) else {}

    def get_stronghold_route(
        self,
        *,
        from_stronghold_id: str,
        to_stronghold_id: str,
        avoid_stronghold_ids: list[str] | None = None,
        on_road: bool = True,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/v1/me/geography/route",
            params={
                "from_stronghold_id": from_stronghold_id,
                "to_stronghold_id": to_stronghold_id,
                "avoid_stronghold_ids": ",".join(avoid_stronghold_ids or []),
                "on_road": on_road,
            },
        )
        return dict(response) if isinstance(response, dict) else {}

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: JsonValue = None,
        use_auth: bool = True,
    ) -> JsonValue:
        headers: dict[str, str] = {"Accept": "application/json"}
        if use_auth:
            if not self.token:
                raise ValueError("Authenticated request attempted before login/token initialization")
            headers["Authorization"] = f"Bearer {self.token}"
        url = f"{self.base_url}{endpoint}"
        response = self.transport.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params,
            json_body=json_body,
            timeout=self.timeout,
        )
        if 200 <= response.status_code < 300:
            return response.data
        raise self._build_api_error(method=method.upper(), endpoint=endpoint, response=response)

    def _build_api_error(
        self,
        *,
        method: str,
        endpoint: str,
        response: TransportResponse,
    ) -> CommanderApiError:
        detail = response.data
        raw_detail = detail
        message = f"{method} {endpoint} failed"
        if isinstance(detail, dict) and "detail" in detail:
            raw_detail = detail["detail"]
        else:
            raw_detail = detail
        if isinstance(raw_detail, dict):
            raw_message = raw_detail.get("message")
            if raw_message:
                message = str(raw_message)
            else:
                message = json.dumps(raw_detail, sort_keys=True)
        elif isinstance(raw_detail, str) and raw_detail.strip():
            message = raw_detail.strip()
        error_code = {
            400: "invalid_request",
            401: "authentication_error",
            404: "not_found",
            409: "conflict",
            422: "unprocessable_entity",
        }.get(response.status_code, "api_error")
        retryable = response.status_code in {408, 429, 502, 503, 504} or response.status_code >= 500
        return CommanderApiError(
            message=message,
            endpoint=endpoint,
            method=method,
            http_status=response.status_code,
            raw_detail=raw_detail,
            error_code=error_code,
            retryable=retryable,
        )
