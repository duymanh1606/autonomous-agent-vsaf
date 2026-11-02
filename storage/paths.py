"""Per-account runtime directories.

Chrome profiles, screenshots, uploads, temp files and logs are all account
state. A shared path is not a cosmetic problem: one Chrome profile reused by
two accounts means one session cookie, and the agent would act as the wrong
account without any error being raised.

Every path is derived here from `account_id` so that no caller has to remember
the layout, and so `account_id` is structurally impossible to omit.
"""

from pathlib import Path

from load_config import BASE_DIR
from models.account import require_account_id


ACCOUNTS_ROOT = BASE_DIR / "data" / "accounts"


def get_account_data_dir(account_id, *, create: bool = True) -> Path:
    """`data/accounts/<account_id>/` - the root of one account's files."""
    path = ACCOUNTS_ROOT / require_account_id(account_id)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _subdirectory(account_id, name, *, create: bool) -> Path:
    path = get_account_data_dir(account_id, create=create) / name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_account_profile_dir(account_id, *, create: bool = True) -> Path:
    return _subdirectory(account_id, "chrome-profile", create=create)


def get_account_screenshot_dir(account_id, *, create: bool = True) -> Path:
    return _subdirectory(account_id, "screenshots", create=create)


def get_account_upload_dir(account_id, *, create: bool = True) -> Path:
    return _subdirectory(account_id, "uploads", create=create)


def get_account_temp_dir(account_id, *, create: bool = True) -> Path:
    return _subdirectory(account_id, "temp", create=create)


def get_account_log_dir(account_id, *, create: bool = True) -> Path:
    return _subdirectory(account_id, "logs", create=create)


def get_account_media_dir(account_id, *, create: bool = True) -> Path:
    """`data/accounts/<id>/media/` - pictures downloaded out of DM threads.

    A cache, not a record: `services.media_cache.MediaCache` fills it so the
    dashboard has something durable to display (TikTok's own URLs expire and
    answer 403 without the session), and evicts from it under a size budget.
    Deleting the directory loses no history, only the pictures.
    """
    return _subdirectory(account_id, "media", create=create)


# There is deliberately no per-account database path. Conversations, messages,
# customers, tasks and checkpoints all live in one shared PostgreSQL database
# and are separated by `account_id`. Only *files* are split per account: a
# Chrome profile, screenshots, uploads, temp files and logs cannot be shared,
# because one profile reused by two accounts means one session cookie and the
# agent acting as the wrong account.
