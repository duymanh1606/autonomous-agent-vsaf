"""The Orders page's "Xuất Google Sheet" button: one sheet for every workspace,
configured once as GSHEET_URL in `.env`."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_api import main

SHEET = "https://docs.google.com/spreadsheets/d/1AbC-dEf_123/edit#gid=0"


class SheetLinkTests(unittest.TestCase):
    def link(self, value):
        with mock.patch.dict(os.environ, {"GSHEET_URL": value}):
            return main.sheet_link()["url"]

    def test_the_configured_sheet_is_handed_to_the_page(self):
        self.assertEqual(self.link(SHEET), SHEET)
        self.assertEqual(self.link(f"  {SHEET}\n"), SHEET)

    def test_unset_hides_the_button(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GSHEET_URL", None)
            self.assertEqual(main.sheet_link(), {"url": ""})

    def test_anything_but_a_google_sheet_hides_the_button(self):
        """The value becomes a link every user clicks."""
        for value in (
            "javascript:alert(1)",
            "https://evil.example/spreadsheets/d/x",
            "https://docs.google.com.evil.example/spreadsheets/d/x",
            "http://docs.google.com/spreadsheets/d/x",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.link(value), "")


if __name__ == "__main__":
    unittest.main()
