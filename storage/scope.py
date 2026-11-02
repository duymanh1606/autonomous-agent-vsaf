"""One object that hands out repositories already bound to one account.

Honest status: the running agent and supervisor do NOT go through this. They
build their repositories directly (main.build_agent, dashboard_api) and a good
deal of SQL lives outside `storage/` altogether. What keeps every one of those
statements account-scoped is `storage/audit.py`, run by
tests/test_account_isolation.py over the whole tree - not this class. The
scope is what those isolation tests use, and the shape new code should aim
for: ask a scope for a repository, and the repository injects `account_id`
into every statement:

    repo = RepositoryScope(db=database, account_id="acc_001")
    await repo.messages.recent(conversation_id="conv_001")

The account filter is therefore not something a developer has to remember - it
is a property of the object they were given. Constructing a scope is the only
place an account is named, which also makes cross-account access easy to spot
in review: it requires a second, visible `RepositoryScope(...)`.
"""

from functools import cached_property

from memory.conversation_store import PostgresConversationStore
from memory.processed_messages import PostgresProcessedMessageStore
from models.account import AccountContext, assert_account_scope, require_account_id
from services.escalation import PostgresEscalationService
from storage.conversation_context import ConversationContextRepository
from storage.repositories import TaskRepository


class RepositoryScope:
    """Every repository for one account, and nothing from any other."""

    def __init__(self, db, account_id=None, *, context: AccountContext | None = None,
                 minimum_fact_confidence=0.70):
        if context is not None and account_id is not None:
            assert_account_scope(
                context.account_id, account_id, boundary="RepositoryScope"
            )
        resolved = account_id if account_id is not None else (
            context.account_id if context is not None else getattr(db, "account_id", None)
        )
        self.account_id = require_account_id(resolved)
        # The database was opened for an account; a scope may not reinterpret
        # its rows as belonging to a different one.
        database_account = getattr(db, "account_id", None)
        if database_account:
            assert_account_scope(
                database_account, self.account_id, boundary="RepositoryScope/database"
            )
        self.db = db
        self.context = context
        self.minimum_fact_confidence = float(minimum_fact_confidence)

    # Built lazily: a runtime that never sends a message should not have to
    # construct a send-dedup repository to read a conversation.
    @cached_property
    def tasks(self) -> TaskRepository:
        return TaskRepository(self.db, account_id=self.account_id)

    @cached_property
    def conversations(self) -> PostgresConversationStore:
        return PostgresConversationStore(self.db, account_id=self.account_id)

    @cached_property
    def messages(self) -> PostgresConversationStore:
        """Alias: messages are read and written through the conversation store."""
        return self.conversations

    @cached_property
    def customers(self) -> ConversationContextRepository:
        return ConversationContextRepository(
            self.db, self.minimum_fact_confidence, account_id=self.account_id
        )

    @cached_property
    def processed_messages(self) -> PostgresProcessedMessageStore:
        return PostgresProcessedMessageStore(self.db, account_id=self.account_id)

    @cached_property
    def escalations(self) -> PostgresEscalationService:
        return PostgresEscalationService(self.db, account_id=self.account_id)

    def assert_owns(self, account_id, *, boundary: str = "") -> str:
        return assert_account_scope(self.account_id, account_id, boundary=boundary)

    def __repr__(self) -> str:
        return f"RepositoryScope(account_id={self.account_id!r})"
