import argparse
import asyncio
import json
import uuid

from load_config import BASE_DIR, load_config
from models.account import AccountContext
from models.tasks import Task
from storage.database import Database, resolve_dsn
from storage.repositories import TaskRepository


async def submit(args):
    config = load_config(args.config)
    account = AccountContext.from_config(config)
    database = Database(
        resolve_dsn(config),
        BASE_DIR / "storage/schema.sql",
        account_id=account.account_id,
    )
    try:
        task = Task(
            id=args.id or str(uuid.uuid4()),
            account_id=account.account_id,
            capability=args.capability,
            platform=args.platform,
            priority=args.priority,
            payload=json.loads(args.payload),
        )
        await TaskRepository(database).submit(task)
        print(task.id)
    finally:
        database.close()


def main():
    parser = argparse.ArgumentParser(description="Submit an authorized task to the local agent")
    parser.add_argument("capability")
    parser.add_argument("--payload", default="{}", help="JSON task payload")
    parser.add_argument("--platform", default="tiktok_web")
    parser.add_argument("--priority", type=int, default=5)
    parser.add_argument("--id")
    parser.add_argument("--config", default=None)
    asyncio.run(submit(parser.parse_args()))


if __name__ == "__main__":
    main()
