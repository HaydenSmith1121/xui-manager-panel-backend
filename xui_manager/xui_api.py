from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Mapping


class XuiApiError(RuntimeError):
    pass


class XuiClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_tls: bool = True,
        timeout: int = 15,
        opener: Any | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.context = None if verify_tls else ssl._create_unverified_context()
        self.cookies = CookieJar()
        self.opener = opener or self._build_opener()
        self.csrf_token = ""

    def __repr__(self) -> str:
        return f"XuiClient(base_url={self.base_url!r}, username={self.username!r}, timeout={self.timeout!r})"

    def login(self) -> None:
        self.csrf_token = self._fetch_csrf_token()
        body = urllib.parse.urlencode({"username": self.username, "password": self.password}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        response = self._request("login", data=body, headers=headers)
        self._parse_payload(response, "x-ui login failed")

    def list_inbounds(self) -> list[dict[str, Any]]:
        response = self._request("panel/api/inbounds/list")
        payload = self._parse_payload(response, "x-ui inbounds list failed")
        items = payload.get("obj") or []
        return items if isinstance(items, list) else []

    def get_inbound(self, inbound_id: int) -> dict[str, Any]:
        response = self._request(f"panel/api/inbounds/get/{int(inbound_id)}")
        payload = self._parse_payload(response, "x-ui inbound lookup failed")
        item = payload.get("obj") or {}
        if isinstance(item, Mapping):
            return dict(item)
        if isinstance(item, list):
            for inbound in item:
                if isinstance(inbound, Mapping) and int(inbound.get("id") or 0) == int(inbound_id):
                    return dict(inbound)
        return {}

    def find_client(self, inbound: Mapping[str, Any], email: str) -> dict[str, Any] | None:
        target = email.strip().lower()
        for client in self._inbound_clients(inbound):
            if str(client.get("email") or "").strip().lower() == target:
                return dict(client)
        return None

    def add_vless_client(
        self,
        *,
        inbound_id: int,
        client_uuid: str,
        email: str,
        flow: str,
        expire_at: int,
    ) -> dict[str, Any]:
        client = self._vless_client(client_uuid, email, flow, expire_at, True)
        return self._mutate_vless_client("panel/api/inbounds/addClient", inbound_id, client, email, modern_action="add")

    def update_vless_client(
        self,
        *,
        inbound_id: int,
        client_uuid: str,
        email: str,
        flow: str,
        expire_at: int,
        enabled: bool,
    ) -> dict[str, Any]:
        client = self._vless_client(client_uuid, email, flow, expire_at, enabled)
        return self._mutate_vless_client(
            f"panel/api/inbounds/updateClient/{client_uuid}",
            inbound_id,
            client,
            email,
            modern_action="update",
        )

    def delete_vless_client(self, *, inbound_id: int, client_uuid: str, email: str) -> bool:
        inbound = self.get_inbound(inbound_id)
        existing = self.find_client(inbound, email)
        if not existing:
            return True
        if existing.get("id") != client_uuid:
            raise XuiApiError("x-ui client deletion conflict")
        quoted_uuid = urllib.parse.quote(client_uuid, safe="")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            response = self._request(
                f"panel/api/inbounds/{int(inbound_id)}/delClient/{quoted_uuid}",
                data=b"",
                headers=headers,
            )
        except XuiApiError as exc:
            if "HTTP 404" not in str(exc):
                raise
            quoted_email = urllib.parse.quote(email, safe="")
            response = self._request(f"panel/api/clients/del/{quoted_email}", data=b"", headers=headers)
        self._parse_payload(response, "x-ui client deletion failed", allow_empty=True)
        if self.find_client(self.get_inbound(inbound_id), email):
            raise XuiApiError("x-ui client deletion could not be verified")
        return True

    def client_traffic(self, email: str) -> dict[str, int]:
        target = email.strip().lower()
        traffic = {"up": 0, "down": 0}
        for inbound in self.list_inbounds():
            for stat in inbound.get("clientStats") or []:
                if not isinstance(stat, Mapping):
                    continue
                if str(stat.get("email") or "").strip().lower() != target:
                    continue
                traffic["up"] += int(stat.get("up") or 0)
                traffic["down"] += int(stat.get("down") or 0)
        return traffic

    def _request(self, path: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> str:
        url = urllib.parse.urljoin(self.base_url, path)
        request_headers = dict(headers or {})
        if data is not None and self.csrf_token and not any(key.lower() == "x-csrf-token" for key in request_headers):
            request_headers["X-CSRF-Token"] = self.csrf_token
        request = urllib.request.Request(url, data=data, headers=request_headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            if exc.reason:
                detail = f"{detail} {self._sanitize(str(exc.reason))}"
            raise XuiApiError(f"x-ui request failed: {detail}") from exc
        except urllib.error.URLError as exc:
            raise XuiApiError("x-ui request failed: URL error") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise XuiApiError("x-ui request timed out") from exc
        except OSError as exc:
            raise XuiApiError("x-ui request failed: transport error") from exc

    def _build_opener(self) -> Any:
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self.cookies)]
        if self.context:
            handlers.append(urllib.request.HTTPSHandler(context=self.context))
        return urllib.request.build_opener(*handlers)

    def _fetch_csrf_token(self) -> str:
        try:
            response = self._request("csrf-token")
            payload = self._parse_payload(response, "x-ui csrf token failed")
        except XuiApiError:
            return ""
        token = payload.get("obj") or payload.get("csrfToken") or payload.get("token") or ""
        return str(token) if token else ""

    def _parse_payload(self, response: str, failure_message: str, allow_empty: bool = False) -> dict[str, Any]:
        if not response.strip():
            if allow_empty:
                return {}
            raise XuiApiError(failure_message)
        try:
            payload = json.loads(response)
        except json.JSONDecodeError as exc:
            raise XuiApiError(f"{failure_message}: invalid JSON response") from exc
        if not isinstance(payload, dict):
            raise XuiApiError(f"{failure_message}: unexpected response")
        if payload.get("success") is False:
            message = str(payload.get("msg") or failure_message)
            raise XuiApiError(self._sanitize(message))
        return payload

    def _mutate_vless_client(
        self,
        path: str,
        inbound_id: int,
        client: dict[str, Any],
        email: str,
        *,
        modern_action: str,
    ) -> dict[str, Any]:
        form = urllib.parse.urlencode(
            {
                "id": str(int(inbound_id)),
                "settings": json.dumps({"clients": [client]}, separators=(",", ":")),
            }
        ).encode("utf-8")
        try:
            response = self._request(path, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
        except XuiApiError as exc:
            if "HTTP 404" not in str(exc):
                raise
            return self._mutate_vless_client_modern(modern_action, inbound_id, client, email)
        self._parse_payload(response, "x-ui client mutation failed", allow_empty=True)
        return self._verified_client(inbound_id, client, email)

    def _mutate_vless_client_modern(
        self,
        action: str,
        inbound_id: int,
        client: dict[str, Any],
        email: str,
    ) -> dict[str, Any]:
        modern_client = self._modern_client_payload(client)
        if action == "add":
            path = "panel/api/clients/add"
            payload: dict[str, Any] = {"client": modern_client, "inboundIds": [int(inbound_id)]}
        elif action == "update":
            quoted_email = urllib.parse.quote(email, safe="")
            query = urllib.parse.urlencode({"inboundIds": str(int(inbound_id))})
            path = f"panel/api/clients/update/{quoted_email}?{query}"
            payload = modern_client
        else:
            raise XuiApiError("unsupported x-ui client mutation")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = self._request(path, data=body, headers={"Content-Type": "application/json"})
        self._parse_payload(response, "x-ui client mutation failed", allow_empty=True)
        return self._verified_client(inbound_id, client, email)

    def _verified_client(self, inbound_id: int, client: dict[str, Any], email: str) -> dict[str, Any]:
        inbound = self.get_inbound(inbound_id)
        stored = self.find_client(inbound, email)
        if not stored or stored.get("id") != client["id"]:
            raise XuiApiError("x-ui client mutation could not be verified")
        return stored

    def _modern_client_payload(self, client: dict[str, Any]) -> dict[str, Any]:
        modern = dict(client)
        modern["tgId"] = int(modern.get("tgId") or 0)
        return modern

    def _vless_client(self, client_uuid: str, email: str, flow: str, expire_at: int, enabled: bool) -> dict[str, Any]:
        return {
            "id": client_uuid,
            "email": email,
            "flow": flow,
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": int(expire_at) * 1000 if expire_at else 0,
            "enable": bool(enabled),
            "tgId": "",
            "subId": "",
            "reset": 0,
        }

    def _inbound_clients(self, inbound: Mapping[str, Any]) -> list[dict[str, Any]]:
        settings = inbound.get("settings") or {}
        if isinstance(settings, str):
            if not settings.strip():
                settings = {}
            else:
                try:
                    settings = json.loads(settings)
                except json.JSONDecodeError as exc:
                    raise XuiApiError("x-ui inbound settings contain invalid JSON") from exc
        if isinstance(settings, Mapping):
            clients = settings.get("clients") or []
        else:
            clients = []
        return [dict(client) for client in clients if isinstance(client, Mapping)]

    def _sanitize(self, message: str) -> str:
        sanitized = message
        sanitized = re.sub(r"(?im)\b(?:set-cookie|cookie)\s*:[^\r\n]*", "[redacted]", sanitized)
        sanitized = re.sub(r"(?i)\b(?:cookie|session|set-cookie)\s*=\s*[^,;\s]+", "[redacted]", sanitized)
        if self.password:
            sanitized = sanitized.replace(self.password, "[redacted]")
        sanitized = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b", "[redacted]", sanitized)
        return sanitized


def sync_usage_from_xui(db: Any) -> dict[str, Any]:
    users = [user for user in db.list_users() if user.get("role") == "user"]
    users_by_email = {user["email"].lower(): user for user in users}
    nodes_by_panel = group_nodes(db.list_nodes(), "panel_id")
    updated = 0
    errors: list[str] = []

    for panel in db.list_panels():
        if not panel.get("enabled") or panel["id"] not in nodes_by_panel:
            continue
        try:
            client = XuiClient(panel["base_url"], panel.get("username", ""), panel.get("password", ""), bool(panel.get("verify_tls", True)))
            client.login()
            inbounds = client.list_inbounds()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{panel['name']}: {exc}")
            continue

        nodes = nodes_by_panel[panel["id"]]
        for node in nodes:
            inbound = find_inbound(inbounds, node)
            if not inbound:
                continue
            for stat in inbound.get("clientStats") or []:
                email = str(stat.get("email") or "").strip().lower()
                user = users_by_email.get(email)
                if not user:
                    continue
                db.record_usage(user["id"], node["id"], int(stat.get("up") or 0), int(stat.get("down") or 0))
                updated += 1

    return {"updated": updated, "errors": errors}


def group_nodes(nodes: list[dict[str, Any]], key: str) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for node in nodes:
        value = node.get(key)
        if value:
            grouped.setdefault(int(value), []).append(node)
    return grouped


def find_inbound(inbounds: list[dict[str, Any]], node: dict[str, Any]) -> dict[str, Any] | None:
    inbound_id = int(node.get("inbound_id") or 0)
    if inbound_id:
        for inbound in inbounds:
            if int(inbound.get("id") or 0) == inbound_id:
                return inbound
    node_name = str(node.get("name") or "").strip()
    for inbound in inbounds:
        if str(inbound.get("remark") or "").strip() == node_name:
            return inbound
    return None
