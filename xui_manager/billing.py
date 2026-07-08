from __future__ import annotations

from typing import Iterable, Mapping


GB = 1024 * 1024 * 1024


def bytes_from_gb(value: float | int) -> int:
    return int(float(value) * GB)


def calculate_billable_usage(items: Iterable[Mapping[str, object]]) -> int:
    total = 0.0
    for item in items:
        upload = int(item.get("upload", 0) or 0)
        download = int(item.get("download", 0) or 0)
        rate = float(item.get("rate", 1) or 1)
        total += (upload + download) * rate
    return int(total)


def calculate_legacy_static_usage(items: Iterable[Mapping[str, object]]) -> dict[str, int]:
    upload = 0.0
    download = 0.0
    for item in items:
        if item.get("mode") == "managed":
            continue
        rate = float(item.get("rate", 1) or 1)
        upload += int(item.get("upload", 0) or 0) * rate
        download += int(item.get("download", 0) or 0) * rate
    return {"upload": int(upload), "download": int(download)}


def usage_totals(db, user_id: int) -> dict[str, int]:
    legacy = calculate_legacy_static_usage(db.usage_for_user(user_id))
    managed = db.managed_usage_totals(user_id)
    return {
        "upload": int(legacy["upload"]) + int(managed["upload"]),
        "download": int(legacy["download"]) + int(managed["download"]),
    }
