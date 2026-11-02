"""One-off CLI to create or promote a dashboard admin account.

    python -m dashboard_api.create_admin <email> <password>

Registration through the UI (POST /api/auth/register) always creates a
"user" role account - this script is the only way to get the first admin,
or any additional one, onto the dashboard.
"""

import sys

# Before anything from dashboard_api is imported: `auth` reads
# DASHBOARD_SECRET_KEY at import time and `UserStore` reads
# AGENT_DATABASE_URL, and neither is set in a plain shell - .env is only
# handed to the supervisor by systemd/dev.sh. Without this the CLI dialled
# PostgreSQL's stock port 5432 instead of the 25432 in .env and failed with
# "connection refused" on a machine where the server was up the whole time.
from load_config import load_env_file

load_env_file()

from dashboard_api import auth  # noqa: E402  (must follow load_env_file)
from dashboard_api.users import UserStore  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: python -m dashboard_api.create_admin <email> <password>")
        raise SystemExit(1)
    email = auth.normalize_email(sys.argv[1])
    password = sys.argv[2]
    if error := auth.email_error(email):
        print(error)
        raise SystemExit(1)
    if len(password) < 8:
        print("mật khẩu cần tối thiểu 8 ký tự")
        raise SystemExit(1)

    store = UserStore()
    try:
        existing = store.get_by_email(email)
        if existing is not None:
            store.set_admin(existing["id"], True)
            # Also activate: a self-registered account starts inactive and
            # waits for approval, so promoting one without this would produce
            # an admin who cannot log in.
            store.set_active(existing["id"], True)
            print(f"promoted existing user {email} (id={existing['id']}) to admin")
        else:
            # A new account follows the same Gmail rule as every other new
            # account; promoting an existing one (above) does not, so an admin
            # made before the rule can still be re-promoted.
            if error := auth.gmail_error(email):
                print(error)
                raise SystemExit(1)
            row = store.create_user(email, auth.hash_password(password), is_admin=True)
            print(f"created admin {email} (id={row['id']})")
    finally:
        store.close()


if __name__ == "__main__":
    main()
