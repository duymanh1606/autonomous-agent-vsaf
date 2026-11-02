"""Account identity and the guard that keeps two accounts apart.

One TikTok account is one runtime, one browser profile and one data scope. The
failure this module exists to prevent is the quiet one: an agent logged in as
account A answering a customer with account B's conversation history. That is
not a crash, it is a wrong reply sent to a real person, so every boundary
between the two checks identity explicitly instead of trusting the caller.

`account_id` is never inferred. A component that needs one and did not receive
one must fail, because guessing is exactly how a reply reaches the wrong shop.
"""

from dataclasses import dataclass


class AccountIsolationError(RuntimeError):
    """Raised when data or control crosses an account boundary."""


def require_account_id(account_id) -> str:
    """Normalize an account id, refusing every empty or absent value.

    There is deliberately no default and no fallback to a "current" account:
    a missing id is a bug in the caller, and turning it into a real account
    silently would let one account read another's data.
    """
    text = str(account_id or "").strip()
    if not text:
        raise AccountIsolationError(
            "account_id is required; refusing to query without an account scope"
        )
    return text


def assert_account_scope(expected_account_id, actual_account_id, *, boundary: str = "") -> str:
    """Fail unless both sides of a boundary name the same account.

    Used where control or data passes between components - task to runtime,
    runtime to browser, runtime to repository, conversation to message, and the
    LLM context builder - so a mismatch stops at the boundary instead of
    becoming a reply sent from the wrong account.
    """
    expected = require_account_id(expected_account_id)
    actual = require_account_id(actual_account_id)
    if expected != actual:
        location = f" at {boundary}" if boundary else ""
        raise AccountIsolationError(
            f"Cross-account access blocked{location}: "
            f"expected={expected} actual={actual}"
        )
    return expected


@dataclass(frozen=True)
class AccountContext:
    """The identity carried through the whole pipeline, unchanged.

    Frozen because nothing downstream may retarget a running worker at another
    account; a different account means a different runtime.
    """

    account_id: str
    platform: str
    username: str = ""
    display_name: str = ""

    def __post_init__(self):
        object.__setattr__(self, "account_id", require_account_id(self.account_id))
        platform = str(self.platform or "").strip().casefold()
        if not platform:
            raise AccountIsolationError("account platform is required")
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "username", str(self.username or "").strip())
        object.__setattr__(self, "display_name", str(self.display_name or "").strip())

    def assert_owns(self, account_id, *, boundary: str = "") -> str:
        """Check that `account_id` is this context's account."""
        return assert_account_scope(self.account_id, account_id, boundary=boundary)

    @property
    def log_prefix(self) -> str:
        """`account=<id>`, so every log line names the account that wrote it."""
        return f"account={self.account_id}"

    @classmethod
    def from_config(cls, config) -> "AccountContext":
        """Read this process's account identity from its config.

        Raises when `account.id` is absent rather than inventing one. Every
        shipped config names its account explicitly - including the
        single-account default - so a missing id means the config is wrong,
        and continuing under a guessed identity would let this process write
        into whichever account's rows it happened to pick.
        """
        account_id = config.get("account.id")
        if not str(account_id or "").strip():
            raise AccountIsolationError(
                "config is missing `account.id`; refusing to start without an "
                "explicit account identity"
            )
        return cls(
            account_id=account_id,
            platform=config.get("account.platform")
            or config.get("platform.default", "tiktok_web"),
            username=config.get("account.username", ""),
            display_name=config.get("account.label", ""),
        )
