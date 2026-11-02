import tempfile
import unittest
from pathlib import Path

from load_config import load_config


class ConfigValidationTests(unittest.TestCase):
    def config(self, body):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_burst_cap_must_exceed_quiet_period(self):
        path = self.config("""
messaging:
  auto_check:
    reply_quiet_period_seconds: 10
    max_burst_wait_seconds: 10
""")
        with self.assertRaisesRegex(ValueError, "greater than"):
            load_config(path)

    def test_request_reservation_cannot_exceed_slots(self):
        path = self.config("""
messaging:
  auto_check:
    max_conversations_per_scan: 2
    request_slot_reservation: 3
""")
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            load_config(path)

    def test_video_ceiling_cannot_exceed_sixty_seconds(self):
        path = self.config("""
messaging:
  media:
    max_dm_video_duration_seconds: 61
""")
        with self.assertRaisesRegex(ValueError, "<= 60"):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
