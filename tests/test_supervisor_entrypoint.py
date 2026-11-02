"""The supervisor runs from exactly one copy of dashboard_api/main.py.

Started as `python -m dashboard_api.main`, main.py ran as `__main__` while the
routes in `routes/` imported it again as `dashboard_api.main` - a second
account registry, loaded once, so every workspace created afterwards got
"unknown account" from the product, order, chat media, translation and push
routes.
"""

import runpy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from dashboard_api import main as supervisor
from dashboard_api.routes import chat_media, orders, products, push, translations

ROOT = Path(__file__).resolve().parents[1]


class SupervisorEntrypointTest(unittest.TestCase):
    def test_package_entry_runs_the_imported_module(self):
        with mock.patch.object(supervisor, "main") as run:
            runpy.run_module("dashboard_api", run_name="__main__")
        run.assert_called_once_with()

    def test_routes_share_the_module_the_entry_runs(self):
        for routes in (chat_media, orders, products, push, translations):
            with self.subTest(routes=routes.__name__):
                self.assertIs(routes._main(), supervisor)
                self.assertIs(routes._main().registry, supervisor.registry)

    def test_running_main_as_a_script_is_refused(self):
        result = subprocess.run(
            [sys.executable, "-m", "dashboard_api.main"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python -m dashboard_api", result.stderr)


if __name__ == "__main__":
    unittest.main()
