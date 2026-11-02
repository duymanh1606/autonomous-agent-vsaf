"""Account-scoped PostgreSQL connection and checksum-verified migrations."""

import hashlib
import logging
import os
from pathlib import Path

from typing import Any

import psycopg
from psycopg.rows import dict_row

from models.account import require_account_id
from storage.tenancy import ensure_row_level_security, scope_to_account


logger = logging.getLogger(__name__)
DEFAULT_DSN = "postgresql://agent:agent@127.0.0.1:5432/agent"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
BASELINE_VERSION = "00000000_baseline"
MIGRATION_LOCK_NAME = "autonomous_agent_schema_migrations"


class SchemaMigrationError(RuntimeError):
    """The schema cannot be changed safely without operator intervention."""


def resolve_dsn(config=None) -> str:
    from_env = os.environ.get("AGENT_DATABASE_URL", "").strip()
    if from_env:
        return from_env
    if config is not None:
        configured = str(config.get("storage.database_url", "") or "").strip()
        if configured:
            return configured
    return DEFAULT_DSN


class SecondaryConnection:
    """A second connection to the same database, for a component that must
    neither share the agent's transaction nor wait for its event loop.

    `Database` is the agent's own handle: constructing it runs migrations and
    the destructive startup recovery (`requeue_interrupted_tasks`,
    `expire_interrupted_send_tasks`) that only the process owning the agent may
    run. A component that just needs to read and write a couple of rows must
    not pay that, and must not borrow the agent's connection either - two
    threads interleaving statements inside one transaction is how a takeover
    would end up committed as part of whatever the agent was doing.

    `autocommit=True` on purpose: a plain SELECT through here leaves no idle
    transaction holding locks, while the repositories' own
    `with conn.transaction()` blocks still group their writes.

    Exposes exactly what the repositories in `storage/` read off a database
    handle: `.conn` and `.account_id`.
    """

    def __init__(self, dsn: str | None = None, *, account_id: str, config=None):
        self.account_id = require_account_id(account_id)
        self.dsn = str(dsn) if dsn else resolve_dsn(config)
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    @property
    def conn(self):
        if self._conn is None or getattr(self._conn, "closed", True):
            conn = psycopg.connect(
                self.dsn, row_factory=dict_row, connect_timeout=5,
                autocommit=True,
            )
            try:
                scope_to_account(conn, self.account_id)
            except Exception:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not getattr(self._conn, "closed", True):
            self._conn.close()
        self._conn = None


def file_checksum(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def migration_files(schema_path):
    directory = Path(schema_path).resolve().parent / "migrations"
    return sorted(directory.glob("*.sql"), key=lambda item: item.name)


def _verify_checksum(version, expected, row):
    if row is not None and str(row["checksum"]) != expected:
        raise SchemaMigrationError(
            f"Migration {version} was modified after it was applied; "
            "restore the original file or create a new migration."
        )


def migrate(conn, schema_path) -> None:
    """Install a clean baseline, apply immutable migrations serially, and
    bring row-level security up to date (storage/tenancy.py).

    The advisory lock and all DDL for one startup live in a single short
    transaction. A checksum mismatch fails startup before modified SQL is
    executed. Legacy databases intentionally require the documented clean
    reinitialization instead of a silent, lossy shape conversion.
    """
    schema_path = Path(schema_path).resolve()
    baseline_sql = schema_path.read_text(encoding="utf-8")
    baseline_checksum = file_checksum(schema_path)

    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (MIGRATION_LOCK_NAME,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        row = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version = %s",
            (BASELINE_VERSION,),
        ).fetchone()
        _verify_checksum(BASELINE_VERSION, baseline_checksum, row)
        if row is None:
            legacy = conn.execute("SELECT to_regclass('accounts') AS name").fetchone()
            if legacy and legacy["name"] is not None:
                raise SchemaMigrationError(
                    "A pre-baseline database was detected. Back it up, then follow "
                    "DATABASE_DESIGN_CLEAN.md section 17 to recreate the PostgreSQL volume."
                )
            conn.execute(baseline_sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                (BASELINE_VERSION, baseline_checksum),
            )

        for path in migration_files(schema_path):
            version = path.stem
            checksum = file_checksum(path)
            applied = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version = %s", (version,)
            ).fetchone()
            _verify_checksum(version, checksum, applied)
            if applied is not None:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                (version, checksum),
            )
            logger.info("[DB] applied migration=%s", version)

        ensure_row_level_security(conn)


def prepare_database(dsn=None, schema_path=None, *, config=None) -> None:
    """Schema, migrations and RLS without opening any account - for the
    supervisor's boot, so its scoped readers never meet a database an agent
    has not prepared yet."""
    with psycopg.connect(str(dsn) if dsn else resolve_dsn(config), row_factory=dict_row) as conn:
        migrate(conn, schema_path or SCHEMA_PATH)


class Database:
    """The agent's own connection: migrated as the configured user, then
    scoped to one account (storage/tenancy.py) for everything it runs."""

    def __init__(self, dsn=None, schema_path=None, *, account_id, config=None):
        self.account_id = require_account_id(account_id)
        self.dsn = str(dsn) if dsn else resolve_dsn(config)
        self.conn = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            if schema_path:
                self.initialize(schema_path)
            scope_to_account(self.conn, self.account_id)
        except Exception:
            self.conn.close()
            raise

    def register_account(self, platform="tiktok_web", username="", display_name=""):
        self.conn.execute(
            "INSERT INTO accounts(id, platform, username, display_name, status) "
            "VALUES (%s, %s, %s, %s, 'active') "
            "ON CONFLICT(id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP",
            (self.account_id, platform, username, display_name),
        )
        self.conn.commit()

    _verify_checksum = staticmethod(_verify_checksum)

    def initialize(self, schema_path):
        migrate(self.conn, schema_path)
        self.register_account()

    def close(self):
        self.conn.close()


DataBase = Database
