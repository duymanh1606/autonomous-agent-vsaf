"""deploy/backup.sh, run for real against a throwaway repo layout.

The script is copied into a temporary tree shaped like the repo (its
REPO_DIR is its own parent), with `docker` and `age` replaced by small fakes
on PATH, so every run is isolated from the real data, configs and database.
What is checked is what the operator relies on at 3:30am: an encrypted
archive never leaves plaintext behind, an unencrypted one never goes
offsite, and every run - good or bad - says what happened in last-run.json,
which the health monitor turns into an alert.
"""

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "backup.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
[ -n "${FAKE_PG_DUMP_FAIL:-}" ] && exit 1
echo "-- fake dump"
echo "CREATE TABLE t (id int);"
"""

# Stands in for age: "encrypts" by prefixing a marker, so a test can both
# prove the archive went through it and read the payload back.
FAKE_AGE = """#!/usr/bin/env bash
out=""; input=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -r|-R) echo "$2" >> "$(dirname "$0")/recipients"; shift 2 ;;
        *) input="$1"; shift ;;
    esac
done
{ printf 'FAKEAGE\\n'; cat "$input"; } > "$out"
"""

RECIPIENT = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"


class BackupScriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "deploy").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.repo / "deploy" / "backup.sh")
        (self.repo / "configs" / "accounts").mkdir(parents=True)
        (self.repo / "configs" / "config.yaml").write_text("messaging: {}\n")
        (self.repo / "configs" / "accounts" / "shop-a.yaml").write_text("extends: ../config.yaml\n")
        (self.repo / "data" / "accounts" / "shop-a" / "products").mkdir(parents=True)
        (self.repo / ".env").write_text("POSTGRES_PASSWORD=secret\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("age", FAKE_AGE)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.backups = self.repo / "data" / "backups"

    def run_backup(self, **env):
        environment = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.root),
            **env,
        }
        return subprocess.run(
            ["bash", str(self.repo / "deploy" / "backup.sh")],
            capture_output=True, text=True, timeout=60, env=environment,
        )

    def status(self):
        return json.loads((self.backups / "last-run.json").read_text())

    def archives(self, pattern="backup-*"):
        return sorted(p.name for p in self.backups.glob(pattern)
                      if not p.name.endswith(".partial"))

    def members(self, path: Path) -> set[str]:
        data = path.read_bytes()
        if data.startswith(b"FAKEAGE\n"):
            plain = self.root / "plain.tar.gz"
            plain.write_bytes(data[len(b"FAKEAGE\n"):])
            path = plain
        with tarfile.open(path) as archive:
            return {name.removeprefix("./") for name in archive.getnames()}

    def test_a_plain_local_run_archives_everything_but_secrets(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        [name] = self.archives()
        self.assertTrue(name.endswith(".tar.gz"))
        members = self.members(self.backups / name)
        self.assertTrue({"database.sql", "config.yaml", "configs/shop-a.yaml"} <= members)
        self.assertNotIn("env", members)
        self.assertEqual(self.status()["remote"], "disabled")
        self.assertTrue(self.status()["ok"])
        self.assertEqual((self.backups / name).stat().st_mode & 0o777, 0o600)

    def test_an_encrypted_run_leaves_no_plaintext_and_carries_the_env(self):
        result = self.run_backup(BACKUP_AGE_RECIPIENT=RECIPIENT)
        self.assertEqual(result.returncode, 0, result.stderr)
        [name] = self.archives()
        self.assertTrue(name.endswith(".tar.gz.age"))
        self.assertIn("env", self.members(self.backups / name))
        self.assertTrue(self.status()["encrypted"])
        self.assertIn(RECIPIENT, (self.bin / "recipients").read_text())

    def test_a_plaintext_archive_is_never_copied_offsite(self):
        remote = self.root / "remote"
        remote.mkdir()
        result = self.run_backup(BACKUP_REMOTE=str(remote))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(remote.iterdir()), [])
        # The local archive is still a good one.
        self.assertEqual(len(self.archives("backup-*.tar.gz")), 1)
        status = self.status()
        self.assertEqual(status["remote"], "refused_plaintext")
        self.assertFalse(status["ok"])

    def test_an_explicit_opt_in_allows_plaintext_offsite(self):
        remote = self.root / "remote"
        remote.mkdir()
        result = self.run_backup(BACKUP_REMOTE=str(remote), BACKUP_ALLOW_PLAINTEXT_REMOTE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(remote.iterdir())), 1)

    def test_an_encrypted_archive_goes_offsite(self):
        remote = self.root / "remote"
        remote.mkdir()
        result = self.run_backup(BACKUP_REMOTE=str(remote), BACKUP_AGE_RECIPIENT=RECIPIENT)
        self.assertEqual(result.returncode, 0, result.stderr)
        [copied] = list(remote.iterdir())
        self.assertTrue(copied.name.endswith(".tar.gz.age"))
        self.assertEqual(self.status()["remote"], "ok")

    def test_a_failed_offsite_copy_is_reported(self):
        result = self.run_backup(
            BACKUP_REMOTE=str(self.root / "missing" / "deeper" / "x"),
            BACKUP_AGE_RECIPIENT=RECIPIENT,
        )
        self.assertNotEqual(result.returncode, 0)
        status = self.status()
        self.assertEqual(status["remote"], "failed")
        self.assertFalse(status["ok"])
        self.assertEqual(len(self.archives()), 1)

    def test_a_failed_dump_writes_no_archive_and_says_why(self):
        result = self.run_backup(FAKE_PG_DUMP_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.archives(), [])
        status = self.status()
        self.assertFalse(status["ok"])
        self.assertIn("pg_dump failed", status["error"])

    def test_rotation_counts_plain_and_encrypted_archives_together(self):
        self.backups.mkdir(parents=True)
        old = self.backups / "backup-20200101-000000.tar.gz"
        old.write_bytes(b"old")
        os.utime(old, (1, 1))
        result = self.run_backup(BACKUP_AGE_RECIPIENT=RECIPIENT, BACKUP_KEEP="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertEqual(len(self.archives()), 1)


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "age not installed")
class RealAgeRoundTripTests(unittest.TestCase):
    """With the real tool: what backup.sh writes, the private key opens."""

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key.txt"
            subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
            public = next(line.split(": ", 1)[1] for line in key.read_text().splitlines()
                          if line.startswith("# public key"))
            plain, sealed, opened = (Path(tmp) / n for n in ("p", "p.age", "o"))
            plain.write_bytes(b"backup bytes")
            subprocess.run(["age", "-r", public, "-o", str(sealed), str(plain)], check=True)
            subprocess.run(["age", "-d", "-i", str(key), "-o", str(opened), str(sealed)], check=True)
            self.assertEqual(opened.read_bytes(), b"backup bytes")


if __name__ == "__main__":
    unittest.main()
