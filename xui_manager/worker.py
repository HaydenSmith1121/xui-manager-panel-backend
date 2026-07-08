from __future__ import annotations

import re
import threading
from typing import Any, Callable


def safe_error(exc: Exception) -> str:
    message = str(exc or "sync failed")
    message = re.sub(r"(?i)(password|secret|token|cookie|session)[^\s,;]*", "[redacted]", message)
    message = re.sub(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b",
        "[redacted]",
        message,
    )
    return message[:200]


def normalize_interval(value: Any, default: int = 300) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = default
    return max(60, min(86400, interval))


class PeriodicSyncWorker:
    def __init__(
        self,
        service,
        interval_provider: Callable[[], Any],
        stop_event: threading.Event | None = None,
    ):
        self.service = service
        self.interval_provider = interval_provider
        self.stop_event = stop_event or threading.Event()
        self.thread: threading.Thread | None = None

    def run_once(self) -> dict[str, Any]:
        try:
            return self.service.sync_all()
        except Exception as exc:  # noqa: BLE001
            return {"synced": 0, "disabled": 0, "errors": [{"error": safe_error(exc)}]}

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            summary = self.run_once()
            print(f"xui-manager sync: {summary}")
            interval = normalize_interval(self.interval_provider())
            self.stop_event.wait(interval)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run_forever, name="xui-manager-sync", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 5) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout)

    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())
