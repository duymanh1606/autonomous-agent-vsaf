"""Static audit: every account-scoped SQL statement must filter by account.

Point 23 of the isolation requirements asks for a sweep of the codebase for
queries that touch account data without an account scope. Grep cannot do it -
the SQL is built from adjacent string literals across several lines - so the
statements are recovered from the AST instead, where Python has already joined
them into one constant.

This is deliberately a *static* check: it runs without a database and catches
a forgotten `account_id` when the file is written rather than when two accounts
have already been mixed in production.
"""

import ast
import re
from pathlib import Path

from load_config import BASE_DIR


# Tables whose rows belong to one account. `accounts` itself is the registry;
# notifications may be queried globally by the supervisor.
ACCOUNT_SCOPED_TABLES = frozenset({
    "tasks",
    "actions",
    "task_checkpoints",
    "screen_transition_stats",
    "customer_states",
    "conversation_messages",
    "customer_profile",
    "customer_facts",
    "message_events",
    "outbound_messages",
    "message_escalations",
    "send_tasks",
    "conversations",
    "account_customers",
    "inbox_ledger",
    "message_request_snapshots",
    "message_translations",
    "conversation_control",
    "orders",
    "reply_timings",
})

# Marks a statement that is unfiltered *on purpose*: the tests proving that
# row-level security (storage/tenancy.py) still confines a query that forgot
# its account filter have to run exactly such a query. Never for runtime code.
RLS_PROBE = "/* rls-probe */"

# Directories that are not agent runtime code.
SKIPPED_DIRECTORIES = frozenset({
    ".venv", "node_modules", "__pycache__", ".git", "dashboard", "data",
})

_STATEMENT = re.compile(r"\b(SELECT|INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\b", re.I)
_TABLE = re.compile(
    r"\b(?:FROM|INTO|UPDATE|JOIN)\s+[\"']?(\w+)[\"']?", re.I
)


def statement_tables(sql: str) -> set[str]:
    """Account-scoped tables a statement reads or writes."""
    return {name.casefold() for name in _TABLE.findall(sql)} & ACCOUNT_SCOPED_TABLES


def is_scoped(sql: str) -> bool:
    """True when the statement names `account_id` at all.

    A coarse rule on purpose: it cannot prove the column is used correctly,
    but a statement that never mentions `account_id` is definitely unscoped,
    and that is the failure worth catching automatically.
    """
    return "account_id" in sql.casefold()


def audit_source(source: str, filename: str = "<string>") -> list[tuple[int, str]]:
    """Return `(line, sql)` for each account-scoped statement missing a scope."""
    tree = ast.parse(source, filename=filename)
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        sql = node.value
        if not _STATEMENT.search(sql) or not statement_tables(sql):
            continue
        if not is_scoped(sql) and RLS_PROBE not in sql:
            findings.append((node.lineno, " ".join(sql.split())))
    return findings


def audit_paths(paths) -> dict[str, list[tuple[int, str]]]:
    findings = {}
    for path in paths:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            result = audit_source(source, str(path))
        except SyntaxError:
            continue
        if result:
            findings[str(path)] = result
    return findings


def python_sources(root=None):
    root = Path(root or BASE_DIR)
    for path in sorted(root.rglob("*.py")):
        if SKIPPED_DIRECTORIES & set(path.relative_to(root).parts):
            continue
        yield path


def audit_project(root=None) -> dict[str, list[tuple[int, str]]]:
    return audit_paths(python_sources(root))


if __name__ == "__main__":
    found = audit_project()
    for filename, rows in found.items():
        for line, sql in rows:
            print(f"{filename}:{line}: {sql[:140]}")
    print(f"\n{sum(len(r) for r in found.values())} unscoped statement(s)")
