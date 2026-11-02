"""The admin "Log" page: tailing a workspace's log files.

`logs.tail` reads backwards from the end so a tens-of-MB runtime.log costs a
few blocks per refresh; the route is admin-only and may open nothing but the
fixed set of per-workspace log files.
"""

import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from dashboard_api import auth, logs, main


def _record(n: int, level: str = "INFO", message: str = "") -> str:
    return f"2026-09-24 15:24:{n % 60:02d},000 {level} core.graph: {message or f'line {n}'}\n"


class TailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "runtime.log"

    def _write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_returns_the_last_entries_oldest_first(self):
        self._write("".join(_record(n) for n in range(10)))

        result = logs.tail(self.path, limit=3)

        self.assertEqual(
            [entry["text"] for entry in result["entries"]],
            [_record(n).rstrip("\n") for n in (7, 8, 9)],
        )
        self.assertFalse(result["truncated"])

    def test_a_traceback_stays_with_its_error_line(self):
        self._write(
            _record(1)
            + _record(2, "ERROR", "Action failed")
            + "Traceback (most recent call last):\n"
            + '  File "x.py", line 1, in f\n'
            + "RuntimeError: boom\n"
            + _record(3)
        )

        result = logs.tail(self.path, min_level="ERROR")

        self.assertEqual(len(result["entries"]), 1)
        entry = result["entries"][0]
        self.assertEqual(entry["level"], "ERROR")
        self.assertTrue(entry["text"].endswith("RuntimeError: boom"))
        self.assertIn("Traceback", entry["text"])

    def test_warning_filter_keeps_warning_and_above(self):
        self._write(
            _record(1, "INFO") + _record(2, "WARNING") + _record(3, "ERROR")
            + _record(4, "DEBUG")
        )

        levels = [e["level"] for e in logs.tail(self.path, min_level="warning")["entries"]]

        self.assertEqual(levels, ["WARNING", "ERROR"])

    def test_query_is_case_insensitive_and_keeps_vietnamese(self):
        self._write(_record(1, message="open Nông Thị Xuân Mai") + _record(2, message="other"))

        entries = logs.tail(self.path, query="nông thị")["entries"]

        self.assertEqual(len(entries), 1)
        self.assertIn("Xuân Mai", entries[0]["text"])

    def test_lines_before_the_first_record_form_their_own_entry(self):
        self._write("some import-time warning\n  continued\n" + _record(1))

        entries = logs.tail(self.path)["entries"]

        self.assertEqual(entries[0], {"level": None, "text": "some import-time warning\n  continued"})
        self.assertEqual(entries[1]["level"], "INFO")

    def test_records_spanning_block_boundaries_are_read_whole(self):
        # Well past one 64 KiB block, with multi-byte text so a split can
        # land inside a UTF-8 sequence.
        records = [_record(n, message=f"Nông Thị {n} " + "x" * 300) for n in range(2000)]
        self._write("".join(records))

        result = logs.tail(self.path, limit=2000)

        self.assertEqual(
            [entry["text"] for entry in result["entries"]],
            [record.rstrip("\n") for record in records],
        )

    def test_scan_cap_reports_truncation(self):
        self._write("".join(_record(n) for n in range(5000)))

        result = logs.tail(self.path, query="never-there", max_scan_bytes=64 * 1024)

        self.assertEqual(result["entries"], [])
        self.assertTrue(result["truncated"])


class LogRouteTests(unittest.TestCase):
    ACCOUNT = "ws-logs"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = Path(self.tmp.name)
        (self.log_dir / "runtime.log").write_text(
            _record(1) + _record(2, "ERROR", "boom"), encoding="utf-8"
        )
        self.user = {"id": 1, "email": "a@x", "is_admin": True, "is_active": True}

        patches = [
            mock.patch.object(main, "_log_dir", return_value=self.log_dir),
            mock.patch.object(
                main.registry, "get",
                side_effect=lambda account_id: object() if account_id == self.ACCOUNT else None,
            ),
            mock.patch.object(main, "adopt_running_processes", return_value={}),
            mock.patch.object(main.sleep_wake, "start"),
            mock.patch.object(main.sleep_wake, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.health_monitor, "start"),
            mock.patch.object(main.health_monitor, "stop", new=mock.AsyncMock()),
            mock.patch.object(main.auth, "decode_session", return_value=(1, 0)),
            mock.patch.object(main.users_store, "get_by_id", side_effect=lambda _id: self.user),
            mock.patch.object(main.users_store, "accessible_account_ids", return_value=[self.ACCOUNT]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)
        self.client.cookies.set(auth.SESSION_COOKIE, "probe")

    def test_admin_reads_a_workspace_log(self):
        response = self.client.get(f"/api/logs/{self.ACCOUNT}", params={"level": "ERROR"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([e["level"] for e in body["entries"]], ["ERROR"])
        self.assertEqual([f["name"] for f in body["files"]], ["runtime"])

    def test_a_granted_non_admin_is_refused(self):
        self.user = {**self.user, "is_admin": False}

        response = self.client.get(f"/api/logs/{self.ACCOUNT}")

        self.assertEqual(response.status_code, 403)

    def test_only_known_log_files_can_be_named(self):
        response = self.client.get(f"/api/logs/{self.ACCOUNT}", params={"file": "../../.env"})

        self.assertEqual(response.status_code, 400)

    def test_unknown_workspace_is_a_404(self):
        response = self.client.get("/api/logs/ws-nope")

        self.assertEqual(response.status_code, 404)

    def test_a_missing_file_is_empty_not_an_error(self):
        response = self.client.get(f"/api/logs/{self.ACCOUNT}", params={"file": "chrome"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entries"], [])


if __name__ == "__main__":
    unittest.main()
