#!/usr/bin/env python3
"""Run storage.maintenance.DatabaseMaintenance for every registered account.

`DatabaseMaintenance` has existed since the retention rules were written, but
nothing ever called it outside tests - `actions`/`tasks`/`notifications` only
grew. Meant to run nightly via `deploy/install_maintenance_timer.sh`; safe to
run by hand too (defaults to `--dry-run` so a manual run never deletes
anything by accident).

Connects once per account (mirrors how the rest of this codebase scopes a
`Database` - see storage/database.py), so one account's connection failure
cannot lose the report for the others.
"""

import argparse
import logging
import sys
from pathlib import Path

# Run as `python scripts/run_maintenance.py`, not `-m`, so the repo root
# (where dashboard_api/, storage/, load_config.py live) is not on sys.path
# by default - add it, matching how main.py is always invoked
# from the repo root and rely on the cwd for the same imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard_api.accounts import AccountRegistry
from load_config import load_config
from storage.database import Database, resolve_dsn
from storage.maintenance import DatabaseMaintenance

logger = logging.getLogger("maintenance")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--older-than-days", type=int, default=30,
        help="delete rows older than this many days (default: 30)",
    )
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="report counts without deleting anything (default)",
    )
    parser.add_argument(
        "--execute", dest="dry_run", action="store_false",
        help="actually delete the rows the dry run reported",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = load_config()
    dsn = resolve_dsn(config)
    registry = AccountRegistry()
    accounts = registry.list()
    if not accounts:
        logger.info("no accounts registered - nothing to do")
        return 0

    failures = 0
    for account in accounts:
        try:
            database = Database(dsn, account_id=account.id, config=config)
        except Exception:
            logger.exception("account=%s: could not connect", account.id)
            failures += 1
            continue
        try:
            report = DatabaseMaintenance(database, account_id=account.id).run(
                older_than_days=args.older_than_days, dry_run=args.dry_run,
            )
        except Exception:
            logger.exception("account=%s: maintenance run failed", account.id)
            failures += 1
            continue
        finally:
            database.conn.close()
        mode = "would delete" if report.dry_run else "deleted"
        logger.info(
            "account=%s %s: actions=%d tasks=%d notifications=%d",
            account.id, mode, report.actions, report.tasks, report.notifications,
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
