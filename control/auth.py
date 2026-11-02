"""The shared secret that stands between the control server and everything
else on this machine.

The control server listens on loopback, and loopback was once treated as
proof of trust. It is not: every local process and user can reach it, and so
can any web page open in a browser on this host - including the workspace's
own Chrome - with nothing but a browser's local-network policy in the way.
Unauthenticated, that is a way to send any text to any customer as the shop,
shut the agent down, or read customer messages off the event stream, all
without passing the dashboard's per-account access checks.

So the agent mints a fresh token each time its control server starts and
writes it to a file only this OS user can read. The supervisor and
deploy/update.sh read it from there - which is also what lets a restarted
supervisor keep talking to an agent it adopted rather than spawned. A web page
can neither read the file nor guess the token.

The file is keyed by port because that is the one thing every caller already
knows: the supervisor from the account registry, update.sh from its port
loop, the agent from its own config.
"""

import hmac
import os
import secrets
from pathlib import Path

from load_config import BASE_DIR


TOKEN_HEADER = "X-Control-Token"


def token_dir() -> Path:
    # Read at call time so a test can point it at a temporary directory.
    override = os.environ.get("AGENT_CONTROL_TOKEN_DIR")
    return Path(override) if override else BASE_DIR / "data" / "runtime" / "control"


def token_path(port: int) -> Path:
    return token_dir() / f"{int(port)}.token"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write_token(port: int, token: str) -> Path:
    """Publish `token` for `port`, readable by this OS user only.

    Written to a temporary file with 0600 from the start and then renamed, so
    there is no moment where the token sits in a file with looser permissions
    or a reader sees half of it.
    """
    directory = token_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    target = token_path(port)
    temporary = target.with_suffix(f".tmp-{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("ascii"))
    finally:
        os.close(fd)
    os.replace(temporary, target)
    return target


def read_token(port: int) -> str | None:
    try:
        token = token_path(port).read_text(encoding="ascii").strip()
    except OSError:
        return None
    return token or None


def remove_token(port: int, token: str) -> None:
    """Delete the file, but only if it still holds `token`.

    A newer agent on the same port may already have replaced it; deleting that
    one would lock the supervisor out of a live workspace.
    """
    if read_token(port) == token:
        try:
            token_path(port).unlink()
        except OSError:
            pass


def headers_for(port: int) -> dict[str, str]:
    """What a caller sends to the control server on `port`.

    Empty when there is no token file: an agent started by code older than
    this check never wrote one and does not ask for it, so an update can drain
    it. A current agent answers 401 instead, which is the right failure.
    """
    token = read_token(port)
    return {TOKEN_HEADER: token} if token else {}


def token_matches(supplied: str | None, expected: str) -> bool:
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
