"""Row-level security: the database itself keeps each shop to its own rows.

Every account-scoped statement already filters by `account_id`, and
`storage/audit.py` checks that statically. This is the layer under that: a
connection opened for one account runs as the `agent_tenant` role with
`app.account_id` set, and a policy on every table that has an `account_id`
column lets it see and write that account's rows only. A query that forgets
its filter - built dynamically, outside `storage/`, anywhere the audit cannot
see - returns this account's rows and nothing else, instead of every shop's.

Why `SET ROLE` on a connection opened as the configured user, rather than a
second login: the configured user owns the schema and runs migrations, backups
(`pg_dump`) and the few deliberately global reads (notifications, LLM usage
totals, the supervisor's access table). Superusers and table owners bypass RLS
unless forced, and a superuser always does - so the scoped connection must
*become* a role that does not. The threat this guards against is a buggy
query, not hostile code: a connection that wanted to could `RESET ROLE`.

Fail-closed: a scoped connection whose `app.account_id` is missing sees no
rows at all, because the policy compares against NULL.

`ensure_row_level_security` is idempotent and runs at every agent and
supervisor start, not as a one-off migration: a table created later (by a
migration or by a supervisor store) is covered on the next start, and a
database restored from a `pg_dump --no-privileges` onto a new server - where
the role does not exist yet - gets the role and its grants back by itself.
"""

from psycopg import pq

from models.account import require_account_id


TENANT_ROLE = "agent_tenant"
ACCOUNT_SETTING = "app.account_id"
POLICY_NAME = "account_isolation"

# Tables with an account_id column that are read across accounts on purpose,
# only ever through an unscoped connection:
#   notifications       - the supervisor's installation-wide alert feed
#   llm_usage           - token counters summed per account for the admin
#   user_account_access - which dashboard user may open which workspace; the
#                         permission check itself has to read it unscoped
UNSCOPED_TABLES = ("notifications", "llm_usage", "user_account_access")

_ENSURE_SQL = f"""
DO $rls$
DECLARE
    target record;
    schema_name text := current_schema();
BEGIN
    -- Several agents start together; their CREATE ROLE / CREATE POLICY must
    -- not race each other.
    PERFORM pg_advisory_xact_lock(hashtext('autonomous_agent_rls'));

    BEGIN
        CREATE ROLE {TENANT_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;
    IF NOT pg_has_role(current_user, '{TENANT_ROLE}', 'MEMBER') THEN
        EXECUTE format('GRANT {TENANT_ROLE} TO %I', current_user);
    END IF;

    EXECUTE format('GRANT USAGE ON SCHEMA %I TO {TENANT_ROLE}', schema_name);
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO {TENANT_ROLE}',
        schema_name);
    EXECUTE format(
        'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA %I TO {TENANT_ROLE}',
        schema_name);

    FOR target IN
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_attribute a ON a.attrelid = c.oid
                             AND a.attname = 'account_id' AND NOT a.attisdropped
         WHERE n.nspname = schema_name
           AND c.relkind IN ('r', 'p')
           AND c.relname <> ALL (%(unscoped)s::text[])
    LOOP
        -- Checked first: ALTER TABLE takes an exclusive lock even when there
        -- is nothing to change, and this runs on every start.
        IF NOT target.relrowsecurity THEN
            EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
                           schema_name, target.relname);
        END IF;
        IF NOT target.relforcerowsecurity THEN
            EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY',
                           schema_name, target.relname);
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
             WHERE schemaname = schema_name AND tablename = target.relname
               AND policyname = '{POLICY_NAME}'
        ) THEN
            EXECUTE format(
                'CREATE POLICY {POLICY_NAME} ON %I.%I '
                'USING (account_id = current_setting(''{ACCOUNT_SETTING}'', true)) '
                'WITH CHECK (account_id = current_setting(''{ACCOUNT_SETTING}'', true))',
                schema_name, target.relname);
        END IF;
    END LOOP;
END
$rls$;
"""


def ensure_row_level_security(conn) -> None:
    """Create the role, grants and policies that are missing. Idempotent.

    Runs in the caller's transaction; the caller commits.
    """
    unscoped = "{" + ",".join(UNSCOPED_TABLES) + "}"
    conn.execute(_ENSURE_SQL.replace("%(unscoped)s", f"'{unscoped}'"))


def scope_to_account(conn, account_id: str) -> None:
    """From now on, `conn` runs as the tenant role, limited to one account.

    Committed on its own: a plain `SET` made inside a transaction that is
    later rolled back is undone with it, which would silently hand the
    connection back to the unrestricted user.
    """
    account_id = require_account_id(account_id)
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise RuntimeError("scope_to_account needs a connection with no open transaction")
    previous = conn.autocommit
    conn.autocommit = True
    try:
        conn.execute(f"SET ROLE {TENANT_ROLE}")
        set_account(conn, account_id)
    finally:
        conn.autocommit = previous


def set_account(conn, account_id: str) -> None:
    """Point an already scoped, autocommit connection at another account.

    For the supervisor's stores that serve every workspace through one
    connection: called before each operation, under the store's lock.
    """
    account_id = require_account_id(account_id)
    row = conn.execute(
        "SELECT set_config(%s, %s, false), current_user",
        (ACCOUNT_SETTING, account_id),
    ).fetchone()
    role = row["current_user"] if isinstance(row, dict) else row[1]
    if role != TENANT_ROLE:
        # Never run account-scoped work on an unrestricted connection.
        raise RuntimeError(f"connection is not scoped (current_user={role})")
