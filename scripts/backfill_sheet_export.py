#!/usr/bin/env python3
"""Push every known customer and order of every account to the Google Sheet.

The live export is event-driven (storage/conversation_context.py's
export_to_sheet runs after each write), so customers already in the database
before that hook existed - or before the sheet was set up - never reach the
sheet until something happens in their thread. Run this once to backfill
them; the same export_to_sheet is used, so rows look exactly like the ones
the agent produces afterwards.

Orders go after the customers, oldest first per workspace: a conversation
has one row, so like the live export the most recently saved order is the
one that row ends up showing. Orders with no conversation are skipped, as
they are live (nothing on the sheet to match them to).

Safe to re-run: the sheet upserts by conversation id. To rebuild the sheet
from scratch, delete its data rows (keep row 1) and run this.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from load_config import load_config, load_env_file

# integrations/sheet_export.py reads GSHEET_WEBHOOK_URL/TOKEN from the
# environment when it sends, so .env must be loaded before that.
load_env_file()

from dashboard_api.accounts import AccountRegistry  # noqa: E402
from dashboard_api.orders import OrderStore  # noqa: E402
from integrations import sheet_export  # noqa: E402
from storage.conversation_context import ConversationContextRepository  # noqa: E402
from storage.database import Database, resolve_dsn  # noqa: E402

logger = logging.getLogger("backfill_sheet_export")


def conversation_ids(database) -> list[str]:
    rows = database.conn.execute(
        "SELECT conversation_id FROM customer_profile WHERE account_id = %s "
        "UNION SELECT conversation_id FROM customer_states WHERE account_id = %s "
        "ORDER BY conversation_id",
        (database.account_id, database.account_id),
    ).fetchall()
    return [row["conversation_id"] for row in rows]


def orders_oldest_first(store, account_id) -> list[dict]:
    rows = [row for row in store.list(account_id) if row.get("conversation_id")]
    return sorted(rows, key=lambda row: (row.get("updated_at") or row["created_at"], row["id"]))


async def backfill(dsn, config, accounts, *, dry_run: bool, batch_size: int) -> int:
    failures = 0
    order_store = OrderStore(dsn)
    for account in accounts:
        try:
            database = Database(dsn, account_id=account.id, config=config)
        except Exception:
            logger.exception("account=%s: could not connect", account.id)
            failures += 1
            continue
        try:
            ids = conversation_ids(database)
            orders = orders_oldest_first(order_store, account.id)
            logger.info(
                "account=%s: %d conversations, %d orders",
                account.id, len(ids), len(orders),
            )
            if dry_run:
                continue
            repository = ConversationContextRepository(database, shop_name=account.label)
            for start in range(0, len(ids), batch_size):
                for conversation_id in ids[start:start + batch_size]:
                    repository.export_to_sheet(conversation_id)
                await asyncio.gather(*sheet_export._background_tasks)
                logger.info(
                    "account=%s: sent %d/%d", account.id,
                    min(start + batch_size, len(ids)), len(ids),
                )
            # One at a time: two orders of one conversation must land in order.
            for done, order in enumerate(orders, start=1):
                await sheet_export.export_order(order, shop=account.label)
                if done % batch_size == 0 or done == len(orders):
                    logger.info("account=%s: orders %d/%d", account.id, done, len(orders))
        except Exception:
            logger.exception("account=%s: backfill failed", account.id)
            failures += 1
        finally:
            database.conn.close()
    order_store.close()
    await sheet_export.close_client()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="only count conversations and orders per account, send nothing",
    )
    parser.add_argument(
        "--batch-size", type=int, default=10,
        help="conversations per progress line (default: 10); sheet_export "
             "itself caps how many webhook calls are in flight",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config()
    if not sheet_export.configured():
        logger.error("GSHEET_WEBHOOK_URL is not set - nothing would be exported")
        return 1
    accounts = AccountRegistry().list()
    if not accounts:
        logger.info("no accounts registered - nothing to do")
        return 0
    failures = asyncio.run(backfill(
        resolve_dsn(config), config, accounts,
        dry_run=args.dry_run, batch_size=max(1, args.batch_size),
    ))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
