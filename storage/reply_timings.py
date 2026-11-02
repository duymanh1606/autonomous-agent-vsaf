"""Per-reply step timings (agent/reply_timing.py), one row per answered burst.

Bound to one account like every other repository: `account_id` comes from the
database the store was opened for and is injected into every statement.
"""

import json

from agent.reply_timing import STEP_PHASES
from models.account import require_account_id


class ReplyTimingRepository:
    def __init__(self, database, *, account_id=None):
        resolved = account_id if account_id is not None else getattr(database, "account_id", None)
        self.account_id = require_account_id(resolved)
        self.database = database

    async def record(self, *, conversation_key, task_id, status, burst_size, milliseconds):
        steps = {name: milliseconds.get(name) for name in STEP_PHASES}
        detail = {
            name: value for name, value in milliseconds.items()
            if name not in STEP_PHASES and name != "total"
        }
        self.database.conn.execute(
            "INSERT INTO reply_timings(account_id, conversation_key, task_id, status, "
            "burst_size, open_ms, hold_ms, reply_ms, recheck_ms, send_ms, verify_ms, "
            "total_ms, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                self.account_id, str(conversation_key or ""), str(task_id or ""),
                str(status or ""), int(burst_size or 0),
                steps["open"], steps["hold"], steps["reply"], steps["recheck"],
                steps["send"], steps["verify"], int(milliseconds.get("total") or 0),
                json.dumps(detail),
            ),
        )
        self.database.conn.commit()
