"""deploy/check_env.sh, and the two lines of deploy wiring that depend on each
other for workspaces to survive a supervisor restart.

The check runs before `source .env` in deploy.sh, so it has two ways to fail:
let through a line bash would execute (the service then dies with a bare
`command not found` behind a 502), or reject a line that sources fine today
(a working deployment then refuses to start). Both are covered below, each
case also checked against bash itself so the test cannot drift from what
`source` really does.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "deploy" / "check_env.sh"

# Lines `source` assigns exactly as written - must pass.
GOOD = [
    "PLAIN=abc",
    "EMPTY=",
    "SINGLE='abcd efgh ijkl mnop'",
    'DOUBLE="a b \\"c\\""',
    "export EXPORTED=value",
    "URL=postgresql://agent:pw@127.0.0.1:25432/agent",
    "COMMENTED=value   # trailing comment",
    "BRACKETS=a[b]c",
    "HASH=ab#cd",
]

# Lines `source` runs something for, or cannot parse - must be rejected.
BAD = [
    "APP_PASSWORD=abcd efgh ijkl mnop",
    "UNTERMINATED='abc",
    "SEMICOLON=a;b",
    "AMPERSAND=https://x.test/?a=1&b=2",
    "PIPE=a|b",
    "not an assignment",
]


def _run_check(text: str) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write(text)
    try:
        return subprocess.run(
            [str(CHECK), f.name], capture_output=True, text=True, timeout=10,
        )
    finally:
        Path(f.name).unlink()


def _bash_sources_cleanly(line: str) -> bool:
    result = subprocess.run(
        ["bash", "-c", f"set -e; {line}\n"],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0 and not result.stderr


class CheckEnvTest(unittest.TestCase):
    def test_accepts_every_line_bash_assigns(self):
        for line in GOOD:
            with self.subTest(line=line):
                self.assertTrue(_bash_sources_cleanly(line))
                self.assertEqual(_run_check(line + "\n").returncode, 0)

    def test_rejects_lines_bash_would_misread(self):
        for line in BAD:
            with self.subTest(line=line):
                self.assertEqual(_run_check(line + "\n").returncode, 1)

    def test_reports_line_and_name_but_never_the_value(self):
        result = _run_check("# c\nOK=1\nSMTP_PASSWORD=abcd efgh secretword\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("dòng 3 (SMTP_PASSWORD)", result.stderr)
        self.assertNotIn("secretword", result.stderr)
        self.assertNotIn("efgh", result.stderr)

    def test_tolerates_crlf_comments_and_missing_final_newline(self):
        self.assertEqual(_run_check("# x\r\n\r\nA=1\r\nB='c d'").returncode, 0)

    def test_env_example_passes(self):
        self.assertEqual(
            subprocess.run([str(CHECK), str(ROOT / ".env.example")],
                           capture_output=True, timeout=10).returncode,
            0,
        )


class SupervisorRestartWiringTest(unittest.TestCase):
    """KillMode=process only keeps workspaces alive if deploy.sh execs the
    supervisor: under that mode systemd signals the main process alone, and a
    bash parent dies without passing SIGTERM on, orphaning a supervisor that
    still holds :28080."""

    def test_unit_kills_only_the_main_process(self):
        installer = (ROOT / "deploy" / "install_systemd_service.sh").read_text()
        self.assertIn("\nKillMode=process\n", installer)

    def test_deploy_execs_the_supervisor(self):
        lines = [
            line for line in (ROOT / "deploy.sh").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(lines[-1], "exec uv run python -m dashboard_api")

    def test_deploy_checks_env_before_sourcing_it(self):
        text = (ROOT / "deploy.sh").read_text()
        self.assertLess(text.index("deploy/check_env.sh"), text.index("source .env"))


if __name__ == "__main__":
    unittest.main()
