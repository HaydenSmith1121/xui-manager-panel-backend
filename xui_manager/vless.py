from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit, urlunsplit


@dataclass(frozen=True)
class VlessTemplate:
    link: str
    flow: str
    host: str
    port: int


def parse_vless_template(source: str) -> VlessTemplate:
    try:
        parsed = urlsplit(source)
    except ValueError as exc:
        raise ValueError("malformed VLESS template") from exc

    if parsed.scheme.lower() != "vless":
        raise ValueError("VLESS template must use vless scheme")
    if not parsed.username:
        raise ValueError("VLESS template requires user")
    if not parsed.hostname:
        raise ValueError("VLESS template requires host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("VLESS template has invalid port") from exc
    if port is None:
        raise ValueError("VLESS template requires port")
    if port <= 0:
        raise ValueError("VLESS template requires positive port")

    flow = ""
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key == "flow":
            flow = value
            break
    return VlessTemplate(link=source, flow=flow, host=parsed.hostname, port=port)


def replace_vless_uuid(source: str, client_uuid: str) -> str:
    try:
        rewritten_uuid = str(uuid.UUID(client_uuid))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid uuid") from exc

    parse_vless_template(source)
    parsed = urlsplit(source)
    _, host_and_port = parsed.netloc.rsplit("@", 1)
    return urlunsplit((parsed.scheme, f"{rewritten_uuid}@{host_and_port}", parsed.path, parsed.query, parsed.fragment))


def validate_target_nodes(nodes: Sequence[Mapping[str, Any]]) -> tuple[float, str]:
    if not nodes:
        raise ValueError("managed target requires at least one node")

    expected_rate: float | None = None
    expected_flow: str | None = None
    expected_target: tuple[int, int] | None = None

    for item in nodes:
        if item.get("mode") != "managed":
            raise ValueError("managed target requires mode='managed'")
        panel_id = positive_int(item.get("panel_id"), "panel_id")
        inbound_id = positive_int(item.get("inbound_id"), "inbound_id")
        target = (panel_id, inbound_id)
        if expected_target is None:
            expected_target = target
        elif target != expected_target:
            raise ValueError("managed nodes must belong to the same target")

        rate = positive_finite_float(item.get("rate"), "multiplier")
        flow = parse_vless_template(str(item.get("source_url") or "")).flow
        if expected_rate is None:
            expected_rate = rate
        elif rate != expected_rate:
            raise ValueError("managed nodes in the same target must use the same multiplier")
        if expected_flow is None:
            expected_flow = flow
        elif flow != expected_flow:
            raise ValueError("managed nodes in the same target must use the same flow")

    return expected_rate or 0, expected_flow or ""


def eligible_managed_nodes(nodes, allowed_tags) -> list[dict[str, Any]]:
    allowed = _tag_set(allowed_tags)
    include_all = not allowed or "all" in allowed
    eligible: list[dict[str, Any]] = []
    for item in nodes:
        if item.get("mode") != "managed" or not bool(item.get("enabled")):
            continue
        item_tags = _tag_set(item.get("tags"))
        if include_all or item_tags & allowed:
            eligible.append(dict(item))
    return eligible


def group_managed_targets(nodes) -> dict[tuple[int, int], list[dict[str, Any]]]:
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for item in nodes:
        panel_id = positive_int(item.get("panel_id"), "panel_id")
        inbound_id = positive_int(item.get("inbound_id"), "inbound_id")
        grouped.setdefault((panel_id, inbound_id), []).append(dict(item))
    for target_nodes in grouped.values():
        validate_target_nodes(target_nodes)
    return grouped


def _tag_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {tag.strip() for tag in value.split(",") if tag.strip()}
    return {str(tag).strip() for tag in value if str(tag).strip()}


def positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} is required")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{name} must be positive")
        number = int(value)
    else:
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} is required") from exc
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def positive_finite_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be positive") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be positive")
    return number
