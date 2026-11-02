"""Test support for the shared PostgreSQL database.

Each database test gets its own temporary PostgreSQL schema. This isolation
on one shared PostgreSQL server is a temporary *schema*: the connection's
`search_path` points at it, `schema.sql` builds the tables inside it, and it is
dropped afterwards. Tests therefore stay independent of each other and of any
real data on the server.

Where to point the tests:

    export AGENT_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:5432/agent

Without a reachable server the database tests skip with that message rather
than failing, so the rest of the suite still runs.
"""

import os
import shutil
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote

import psycopg


BASE_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = BASE_DIR / "storage" / "schema.sql"

DEFAULT_TEST_DSN = "postgresql://agent:agent@127.0.0.1:5432/agent"

SKIP_MESSAGE = (
    "PostgreSQL is not reachable. Start one and set AGENT_TEST_DATABASE_URL, e.g.\n"
    "  docker run -d --name agent-pg -e POSTGRES_PASSWORD=agent "
    "-e POSTGRES_USER=agent -e POSTGRES_DB=agent -p 5432:5432 postgres:16-alpine\n"
    "  export AGENT_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:5432/agent"
)

_available = None

# The sample catalogue that ships with the repo, and where the Gemini media
# tests expect to find it. `.gitignore` keeps `/data/products/` on purpose
# (the `!/data/products/` exception) while ignoring the rest of `data/`, which
# is runtime state - so on any checkout the committed products exist but the
# per-account copy those tests read does not.
COMMITTED_PRODUCTS = BASE_DIR / "data" / "products"
FIXTURE_ACCOUNT_PRODUCTS = BASE_DIR / "data" / "accounts" / "acct-a" / "products"


def fixture_product_root() -> str:
    """Path to the fixture account's catalogue, created from the committed one.

    Without this the media tests ran against an empty catalogue: every lookup
    answered "business service returned no verified data", so no attachment
    could ever be produced and every media assertion failed for a reason that
    had nothing to do with the model or the reply pipeline.

    Copies only what is missing and never overwrites, so a catalogue someone
    has edited locally is left exactly as they left it.
    """
    if COMMITTED_PRODUCTS.is_dir():
        FIXTURE_ACCOUNT_PRODUCTS.mkdir(parents=True, exist_ok=True)
        for product in COMMITTED_PRODUCTS.iterdir():
            target = FIXTURE_ACCOUNT_PRODUCTS / product.name
            if product.is_dir() and not target.exists():
                shutil.copytree(product, target)
    return str(FIXTURE_ACCOUNT_PRODUCTS)


def test_dsn() -> str:
    return os.environ.get("AGENT_TEST_DATABASE_URL", "").strip() or DEFAULT_TEST_DSN


def postgres_available() -> bool:
    """True when the test server answers. Probed once per process."""
    global _available
    if _available is None:
        try:
            with psycopg.connect(test_dsn(), connect_timeout=3) as connection:
                connection.execute("SELECT 1")
            _available = True
        except Exception:
            _available = False
    return _available


def requires_postgres(item):
    """Skip a test or test case when no PostgreSQL server is available."""
    return unittest.skipUnless(postgres_available(), SKIP_MESSAGE)(item)


def dsn_for_schema(schema: str) -> str:
    """The test DSN with `search_path` pinned to one schema."""
    separator = "&" if "?" in test_dsn() else "?"
    return f"{test_dsn()}{separator}options={quote(f'-csearch_path={schema}')}"


class TemporaryPostgresSchema:
    """A private schema for one test, dropped on cleanup.

    This is the PostgreSQL equivalent of the temporary database file the tests
    used before: everything a test creates is invisible to every other test.
    """

    def __init__(self):
        if not postgres_available():
            raise unittest.SkipTest(SKIP_MESSAGE)
        self.name = f"agent_test_{uuid.uuid4().hex[:16]}"
        self._admin = psycopg.connect(test_dsn(), autocommit=True)
        self._admin.execute(f'CREATE SCHEMA "{self.name}"')

    @property
    def dsn(self) -> str:
        return dsn_for_schema(self.name)

    def cleanup(self) -> None:
        try:
            self._admin.execute(f'DROP SCHEMA IF EXISTS "{self.name}" CASCADE')
        finally:
            self._admin.close()


def make_database(test_case, account_id="acc_test"):
    """A `Database` on a throwaway schema, cleaned up with the test.

    Skips the calling test when no server is reachable. Doing it here rather
    than in a decorator means a new database test cannot forget to declare the
    dependency and fail with a connection error instead of skipping.

    Returns the ready-to-use `Database`; the schema is registered for teardown
    on `test_case`, so callers do not have to remember to drop it.
    """
    from storage.database import Database

    if not postgres_available():
        test_case.skipTest(SKIP_MESSAGE)
    schema = TemporaryPostgresSchema()
    test_case.addCleanup(schema.cleanup)
    database = Database(schema.dsn, SCHEMA_PATH, account_id=account_id)
    test_case.addCleanup(database.close)
    return database


def unscoped_connection(test_case, database):
    """A connection to `database`'s schema with no account scope.

    `Database` and every reader run under row-level security (storage/
    tenancy.py), so they cannot write another account's rows - which is the
    point. A test that needs a second shop's data writes it through here, the
    way that shop's own process would; reading through here sees every account
    at once, like the backup does.
    """
    from psycopg.rows import dict_row

    conn = psycopg.connect(database.dsn, row_factory=dict_row, autocommit=True)
    test_case.addCleanup(conn.close)
    return conn
