import threading
import time
import unittest

from xui_manager.worker import PeriodicSyncWorker, normalize_interval


class FakeSyncService:
    def __init__(self, *, fail=False, message="cycle failed"):
        self.fail = fail
        self.message = message
        self.sync_calls = 0

    def sync_all(self):
        self.sync_calls += 1
        if self.fail:
            raise RuntimeError(self.message)
        return {"synced": 1, "errors": [], "disabled": 0}


class WorkerTests(unittest.TestCase):
    def test_normalize_interval_is_clamped(self):
        self.assertEqual(normalize_interval("1"), 60)
        self.assertEqual(normalize_interval("300"), 300)
        self.assertEqual(normalize_interval("999999"), 86400)
        self.assertEqual(normalize_interval("bad"), 300)

    def test_worker_runs_one_cycle(self):
        service = FakeSyncService()
        worker = PeriodicSyncWorker(service, interval_provider=lambda: 300)

        result = worker.run_once()

        self.assertEqual(service.sync_calls, 1)
        self.assertEqual(result["synced"], 1)

    def test_worker_survives_cycle_errors(self):
        service = FakeSyncService(fail=True)
        worker = PeriodicSyncWorker(service, interval_provider=lambda: 300)

        result = worker.run_once()

        self.assertEqual(service.sync_calls, 1)
        self.assertEqual(result["errors"][0]["error"], "cycle failed")

    def test_worker_sanitizes_cycle_errors(self):
        service = FakeSyncService(
            fail=True,
            message="token=abc password=secret id=11111111-2222-4333-8444-555555555555",
        )
        worker = PeriodicSyncWorker(service, interval_provider=lambda: 300)

        result = worker.run_once()

        error = result["errors"][0]["error"]
        self.assertNotIn("abc", error)
        self.assertNotIn("secret", error)
        self.assertNotIn("11111111-2222-4333-8444-555555555555", error)

    def test_worker_start_and_stop(self):
        service = FakeSyncService()
        stop = threading.Event()
        worker = PeriodicSyncWorker(service, interval_provider=lambda: 60, stop_event=stop)

        worker.start()
        deadline = time.time() + 2
        while service.sync_calls == 0 and time.time() < deadline:
            time.sleep(0.01)
        worker.stop()

        self.assertGreaterEqual(service.sync_calls, 1)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
