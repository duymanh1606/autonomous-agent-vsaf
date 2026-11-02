"""`python -m dashboard_api.create_admin` must read .env like the supervisor.

It used to import `dashboard_api.auth` and build a `UserStore` before
anything loaded .env - so DASHBOARD_SECRET_KEY was unset and, worse, the
database DSN fell back to PostgreSQL's stock port 5432 instead of the 25432
in .env, failing with "connection refused" on a machine where the server was
up the whole time. This runs the real module as a subprocess against a
throwaway .env and checks that both values were seen.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class CreateAdminEnvTests(unittest.TestCase):
    def _run(self, env_text: str):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(env_text, encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("DASHBOARD_SECRET_KEY", "AGENT_DATABASE_URL")}
            env["AGENT_ENV_FILE"] = str(env_path)
            return subprocess.run(
                [sys.executable, "-m", "dashboard_api.create_admin",
                 "probe@example.test", "long-enough-password"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=30,
            )

    def test_env_file_is_loaded_before_anything_needs_it(self):
        # A DSN that can never connect but is unmistakably ours: the failure
        # message then proves which host/port the CLI actually dialled.
        result = self._run(
            "DASHBOARD_SECRET_KEY=from-the-env-file\n"
            "AGENT_DATABASE_URL=postgresql://agent:agent@127.0.0.1:1/agent\n"
        )
        combined = result.stdout + result.stderr
        self.assertNotIn("DASHBOARD_SECRET_KEY is not set", combined)
        self.assertIn("port 1 ", combined)
        self.assertNotIn("port 5432", combined)


if __name__ == "__main__":
    unittest.main()
