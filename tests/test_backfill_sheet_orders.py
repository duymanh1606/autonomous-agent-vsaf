"""The sheet backfill also re-sends orders, so a cleared sheet can be rebuilt."""

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "backfill_sheet_export", ROOT / "scripts" / "backfill_sheet_export.py",
)
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list(self, account_id):
        return [row for row in self.rows if row["account_id"] == account_id]


class OrdersOldestFirstTests(unittest.TestCase):
    def test_latest_saved_order_is_sent_last_and_orphans_are_skipped(self):
        """One row per conversation: the last order sent is what it shows,
        and that must be the most recently saved one, as it is live."""
        t = datetime(2026, 9, 1)
        store = _Store([
            # listed newest-created first, as OrderStore.list returns them
            {"id": 3, "account_id": "a", "conversation_id": "c1",
             "created_at": t + timedelta(days=2), "updated_at": t + timedelta(days=2)},
            {"id": 2, "account_id": "a", "conversation_id": None,
             "created_at": t + timedelta(days=1), "updated_at": t + timedelta(days=1)},
            {"id": 1, "account_id": "a", "conversation_id": "c1",
             "created_at": t, "updated_at": t + timedelta(days=5)},  # edited later
            {"id": 4, "account_id": "b", "conversation_id": "c9",
             "created_at": t, "updated_at": t},
        ])
        ordered = backfill.orders_oldest_first(store, "a")
        self.assertEqual([row["id"] for row in ordered], [3, 1])


if __name__ == "__main__":
    unittest.main()
