from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .billing import usage_totals
from .vless import replace_vless_uuid


SHARE_LINK_RE = re.compile(r"(?im)\b(vless|vmess|trojan|ss)://[^\s]+")


@dataclass
class Response:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)


def build_clash_subscription(db: Any, token: str) -> Response:
    user = db.get_user_by_token(token)
    if not user:
        return Response(404, "not found\n", {"Content-Type": "text/plain; charset=utf-8"})
    if user["status"] != "active":
        return subscription_response([], 0, 0, user.get("quota_bytes", 0), user.get("expire_at", 0), "Account inactive")

    totals = usage_totals(db, user["id"])
    used = totals["upload"] + totals["download"]
    quota = int(user.get("quota_bytes", 0) or 0)
    expire_at = int(user.get("expire_at", 0) or 0)
    if quota and used >= quota:
        return subscription_response([], totals["upload"], totals["download"], quota, expire_at, "Traffic exhausted")
    if expire_at and expire_at < int(time.time()):
        return subscription_response([], totals["upload"], totals["download"], quota, expire_at, "Expired")

    plan = db.get_plan(user["plan_id"])
    allowed_tags = set(plan["allowed_tags"] if plan else [])
    nodes = []
    for node in db.list_nodes(enabled_only=True):
        if allowed_tags and not (allowed_tags & set(node["tags"])):
            continue
        client_uuid = None
        if node.get("mode") == "managed":
            managed = db.get_managed_client_for_target(user["id"], node["panel_id"], node["inbound_id"])
            if not managed or managed["state"] != "provisioned" or not managed["desired_enabled"]:
                continue
            client_uuid = managed["client_uuid"]
        proxy = node_to_proxy(node, client_uuid=client_uuid)
        if proxy:
            nodes.append(proxy)
    return subscription_response(nodes, totals["upload"], totals["download"], quota, expire_at, subscription_title(db, user["email"]))


def build_base64_subscription(db: Any, token: str) -> Response:
    context = subscription_context(db, token)
    if context is None:
        return Response(404, "not found\n", {"Content-Type": "text/plain; charset=utf-8"})
    links = context["links"] if context["available"] else []
    encoded = base64.b64encode("\n".join(link for _, link in links).encode("utf-8")).decode("ascii")
    return Response(
        200,
        encoded,
        subscription_headers(
            context["upload"], context["download"], context["quota"], context["expire_at"], context["title"]
        ) | {"Content-Type": "text/plain; charset=utf-8"},
    )


def build_singbox_subscription(db: Any, token: str) -> Response:
    context = subscription_context(db, token)
    if context is None:
        return Response(404, "not found\n", {"Content-Type": "text/plain; charset=utf-8"})
    outbounds = []
    if context["available"]:
        for name, link in context["links"]:
            proxy = share_link_to_proxy(link)
            if proxy:
                proxy["name"] = name
                outbound = proxy_to_singbox(proxy)
                if outbound:
                    outbounds.append(outbound)
    tags = [item["tag"] for item in outbounds]
    payload = {
        "log": {"level": "info"},
        "outbounds": [
            {"type": "selector", "tag": "Proxy", "outbounds": tags + ["direct"], "default": tags[0] if tags else "direct"},
            *outbounds,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"final": "Proxy", "auto_detect_interface": True},
    }
    return Response(
        200,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        subscription_headers(
            context["upload"], context["download"], context["quota"], context["expire_at"], context["title"]
        ) | {"Content-Type": "application/json; charset=utf-8"},
    )


def subscription_context(db: Any, token: str) -> dict[str, Any] | None:
    user = db.get_user_by_token(token)
    if not user:
        return None
    totals = usage_totals(db, user["id"])
    quota = int(user.get("quota_bytes", 0) or 0)
    expire_at = int(user.get("expire_at", 0) or 0)
    used = totals["upload"] + totals["download"]
    available = user["status"] == "active"
    available = available and not (quota and used >= quota)
    available = available and not (expire_at and expire_at < int(time.time()))
    return {
        "user": user,
        "upload": totals["upload"],
        "download": totals["download"],
        "quota": quota,
        "expire_at": expire_at,
        "title": subscription_title(db, user["email"]),
        "available": available,
        "links": eligible_share_links(db, user) if available else [],
    }


def eligible_share_links(db: Any, user: dict[str, Any]) -> list[tuple[str, str]]:
    plan = db.get_plan(user["plan_id"]) if user.get("plan_id") else None
    allowed_tags = set(plan["allowed_tags"] if plan else [])
    links: list[tuple[str, str]] = []
    for node in db.list_nodes(enabled_only=True):
        if allowed_tags and not (allowed_tags & set(node["tags"])):
            continue
        extracted = extract_links(node["source_url"].strip())
        if not extracted:
            continue
        link = extracted[0]
        if node.get("mode") == "managed":
            managed = db.get_managed_client_for_target(user["id"], node["panel_id"], node["inbound_id"])
            if not managed or managed["state"] != "provisioned" or not managed["desired_enabled"]:
                continue
            link = replace_vless_uuid(link, managed["client_uuid"])
        links.append((node["name"], link))
    return links


def subscription_headers(upload: int, download: int, quota: int, expire_at: int, title: str) -> dict[str, str]:
    return {
        "Subscription-Userinfo": f"upload={upload}; download={download}; total={quota}; expire={expire_at}",
        "Profile-Title": base64.b64encode(title.encode("utf-8")).decode("ascii"),
        "Profile-Update-Interval": "12",
        "Cache-Control": "no-store",
    }


def subscription_title(db: Any, fallback: str) -> str:
    title = str(db.get_setting("subscription_title", "") or "").strip()
    return title or fallback


def subscription_response(
    nodes: list[dict[str, Any]],
    upload: int,
    download: int,
    quota: int,
    expire_at: int,
    title: str,
) -> Response:
    group_name = "Proxy"
    names = [node["name"] for node in nodes]
    payload = {
        "name": title,
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "proxies": nodes,
        "proxy-groups": [{"name": group_name, "type": "select", "proxies": names + ["DIRECT"]}],
        "rules": [f"MATCH,{group_name}"],
    }
    title_b64 = base64.b64encode(title.encode("utf-8")).decode("ascii")
    return Response(
        200,
        dump_yaml(payload) + "\n",
        {
            "Content-Type": "text/yaml; charset=utf-8",
            "Subscription-Userinfo": f"upload={upload}; download={download}; total={quota}; expire={expire_at}",
            "Profile-Title": title_b64,
            "Profile-Update-Interval": "12",
            "Cache-Control": "no-store",
        },
    )


def dump_yaml(value: Any, indent: int = 0) -> str:
    return "\n".join(yaml_lines(value, indent))


def yaml_lines(value: Any, indent: int = 0) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, dict):
                if item:
                    lines.append(f"{pad}{key}:")
                    lines.extend(yaml_lines(item, indent + 2))
                else:
                    lines.append(f"{pad}{key}: {{}}")
            elif isinstance(item, list):
                if item:
                    lines.append(f"{pad}{key}:")
                    lines.extend(yaml_lines(item, indent + 2))
                else:
                    lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            prefix = f"{pad}-"
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{prefix} {{}}")
                    continue
                first = True
                for key, child in item.items():
                    item_pad = " " * (indent + 2)
                    line_prefix = f"{prefix} {key}:" if first else f"{item_pad}{key}:"
                    if isinstance(child, dict):
                        if child:
                            lines.append(line_prefix)
                            lines.extend(yaml_lines(child, indent + 4))
                        else:
                            lines.append(f"{line_prefix} {{}}")
                    elif isinstance(child, list):
                        if child:
                            lines.append(line_prefix)
                            lines.extend(yaml_lines(child, indent + 4))
                        else:
                            lines.append(f"{line_prefix} []")
                    else:
                        lines.append(f"{line_prefix} {yaml_scalar(child)}")
                    first = False
            elif isinstance(item, list):
                lines.append(prefix)
                lines.extend(yaml_lines(item, indent + 2))
            else:
                lines.append(f"{prefix} {yaml_scalar(item)}")
        return lines
    return [f"{pad}{yaml_scalar(value)}"]


def yaml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def node_to_proxy(node: dict[str, Any], client_uuid: str | None = None) -> dict[str, Any] | None:
    source = node["source_url"].strip()
    links = extract_links(source)
    if not links:
        return None
    link = replace_vless_uuid(links[0], client_uuid) if client_uuid else links[0]
    proxy = share_link_to_proxy(link)
    if proxy:
        proxy["name"] = node["name"]
    return proxy


def extract_links(text: str) -> list[str]:
    if "://" in text:
        return [match.group(0) for match in SHARE_LINK_RE.finditer(text)]
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", errors="replace")
    except Exception:
        return []
    return [match.group(0) for match in SHARE_LINK_RE.finditer(decoded)]


def share_link_to_proxy(link: str) -> dict[str, Any] | None:
    if link.startswith("vless://"):
        return vless_to_proxy(link)
    if link.startswith("trojan://"):
        return trojan_to_proxy(link)
    return None


def proxy_to_singbox(proxy: dict[str, Any]) -> dict[str, Any] | None:
    proxy_type = proxy.get("type")
    if proxy_type not in {"vless", "trojan"}:
        return None
    outbound: dict[str, Any] = {
        "type": proxy_type,
        "tag": proxy.get("name") or proxy_type,
        "server": proxy.get("server"),
        "server_port": int(proxy.get("port") or 443),
    }
    if proxy_type == "vless":
        outbound["uuid"] = proxy.get("uuid")
    else:
        outbound["password"] = proxy.get("password")
    if proxy.get("tls") or proxy_type == "trojan":
        outbound["tls"] = {
            "enabled": True,
            "server_name": proxy.get("servername") or proxy.get("sni") or proxy.get("server"),
        }
    if proxy.get("network") == "ws":
        ws = proxy.get("ws-opts") or {}
        outbound["transport"] = {
            "type": "ws",
            "path": ws.get("path") or "/",
            "headers": ws.get("headers") or {},
        }
    return {key: value for key, value in outbound.items() if value not in (None, "", {}, [])}


def vless_to_proxy(link: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(link)
    query = urllib.parse.parse_qs(parsed.query)
    security = (first(query, "security") or "").lower()
    network = first(query, "type") or first(query, "network") or "tcp"
    proxy: dict[str, Any] = {
        "name": urllib.parse.unquote(parsed.fragment) or parsed.hostname or "vless",
        "type": "vless",
        "server": parsed.hostname or "",
        "port": parsed.port or 443,
        "uuid": urllib.parse.unquote(parsed.username or ""),
        "network": network,
        "udp": True,
    }
    if security and security != "none":
        proxy["tls"] = security in {"tls", "reality"}
    sni = first(query, "sni") or first(query, "servername")
    if sni:
        proxy["servername"] = sni
    if network == "ws":
        host = first(query, "host")
        path = urllib.parse.unquote(first(query, "path") or "/")
        proxy["ws-opts"] = {"path": path}
        if host:
            proxy["ws-opts"]["headers"] = {"Host": host}
    return {k: v for k, v in proxy.items() if v not in ("", None, {}, [])}


def trojan_to_proxy(link: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(link)
    query = urllib.parse.parse_qs(parsed.query)
    return {
        "name": urllib.parse.unquote(parsed.fragment) or parsed.hostname or "trojan",
        "type": "trojan",
        "server": parsed.hostname or "",
        "port": parsed.port or 443,
        "password": urllib.parse.unquote(parsed.username or ""),
        "sni": first(query, "sni") or parsed.hostname or "",
        "udp": True,
    }


def first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None
