"""Conversation data for exactly one account.

The repository is *bound* to an account at construction and injects
`account_id` into every statement itself. Callers therefore cannot write a
query that forgets the account scope - the only way to reach another account's
rows is to construct another repository, which is a visible act.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from integrations.sheet_export import format_chat_address, spawn_export_customer_state
from models.account import assert_account_scope, require_account_id
from models.customer_state import CustomerState


logger = logging.getLogger(__name__)

PROFILE_FACT_FIELDS = frozenset({"name", "phone", "email", "address", "city"})
ALLOWED_FACT_TYPES = frozenset({
    "name", "phone", "email", "address", "city", "product_interest",
    "size", "color", "quantity", "purchase_note",
})


class ConversationContextRepository:
    """Persist only validated, visible conversation data and explicit facts."""

    def __init__(
        self, database, minimum_fact_confidence=0.70, *, account_id=None, shop_name=None,
    ):
        # The account comes from the database it was opened for; passing one
        # explicitly is allowed only to confirm the same account.
        resolved = account_id if account_id is not None else getattr(database, "account_id", None)
        self.account_id = require_account_id(resolved)
        if account_id is not None:
            assert_account_scope(
                getattr(database, "account_id", account_id), self.account_id,
                boundary="ConversationContextRepository",
            )
        self.database = database
        self.minimum_fact_confidence = float(minimum_fact_confidence)
        # The workspace's name, shown as the Google Sheet's "Shop" column.
        self.shop_name = shop_name

    @staticmethod
    def message_hash(conversation_id, direction, text, account_id="", order=""):
        """Content hash of one visible message.

        `account_id` participates so the identical text sent by the same
        customer to two accounts produces two hashes, never one shared row.

        `order` (DOM position within the thread as currently rendered, see
        extension/platforms/tiktok.js's own `messageId` fallback and the
        comment on it) is optional and defaults to "" - callers that don't
        have it (the vision fallback path below) hash exactly as before.
        `store_visible_messages` does supply it, because a customer sending
        the exact same text twice in one conversation ("Bạn ơi", "dạ", ...)
        is common, and without a positional tiebreaker the second, genuinely
        new message hashed identically to the first and was silently
        dropped as a duplicate by the `ON CONFLICT DO NOTHING` insert below
        - the same failure mode the extension's own comment describes at
        the DOM layer, just recreated one layer up because this hash used
        to ignore the position it already gets handed.
        """
        normalized = "\0".join((
            str(account_id or "").strip(),
            str(conversation_id).strip().casefold(),
            str(direction).strip().upper(),
            " ".join(str(text).split()),
            str(order if order is not None else "").strip(),
        ))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------ registry
    def register_conversation(self, conversation_id, customer_id="", platform=""):
        """Record `account + platform_conversation_id` as one relationship."""
        self.database.conn.execute(
            "INSERT INTO conversations("
            "account_id, platform_conversation_id, platform, customer_id) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT(account_id, platform_conversation_id) DO UPDATE SET "
            "customer_id=COALESCE(NULLIF(excluded.customer_id, ''), conversations.customer_id), "
            "platform=COALESCE(NULLIF(excluded.platform, ''), conversations.platform), "
            "updated_at=CURRENT_TIMESTAMP",
            (self.account_id, str(conversation_id), str(platform or ""), str(customer_id or "")),
        )

    def register_customer(self, customer_id, username="", platform_user_id="", metadata=None):
        """One customer *of this account*; the same person elsewhere is separate."""
        current = self.database.conn.execute(
            "SELECT metadata FROM account_customers "
            "WHERE account_id = %s AND customer_id = %s",
            (self.account_id, str(customer_id)),
        ).fetchone()
        current_metadata = {}
        if current and current["metadata"]:
            try:
                current_metadata = json.loads(current["metadata"])
            except (TypeError, ValueError):
                logger.warning(
                    "[DB] account=%s invalid customer metadata replaced customer_id=%s",
                    self.account_id, customer_id,
                )
        if not isinstance(current_metadata, dict):
            logger.warning(
                "[DB] account=%s non-object customer metadata replaced customer_id=%s",
                self.account_id, customer_id,
            )
            current_metadata = {}
        merged_metadata = {**current_metadata, **dict(metadata or {})}
        self.database.conn.execute(
            "INSERT INTO account_customers("
            "account_id, customer_id, platform_user_id, username, metadata) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT(account_id, customer_id) DO UPDATE SET "
            "username=COALESCE(NULLIF(excluded.username, ''), account_customers.username), "
            "platform_user_id=COALESCE(NULLIF(excluded.platform_user_id, ''), "
            "account_customers.platform_user_id), metadata=excluded.metadata, "
            "updated_at=CURRENT_TIMESTAMP",
            (
                self.account_id, str(customer_id), str(platform_user_id or ""),
                str(username or ""),
                json.dumps(merged_metadata, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _normalize_avatar_url(raw_value):
        """Only accept http(s) image URLs - the value is rendered as an <img
        src> on the dashboard verbatim, so anything else (a bare filename, a
        `javascript:`/`data:` URI, garbage) is dropped rather than stored."""
        value = str(raw_value or "").strip()
        if not value or len(value) > 2000:
            return ""
        if not re.match(r"^https?://", value, re.IGNORECASE):
            return ""
        return value

    def upsert_customer_identity(
        self, conversation_id, customer_id=None, platform="", *,
        username=None, display_name=None, platform_user_id=None, metadata=None,
        avatar_url=None,
    ):
        """Persist DOM-verified identity in both the registry and CRM profile.

        Identity is observable platform data, so it must not depend on Gemini's
        confidence in extracted message facts.  Keeping this in one method also
        prevents `account_customers` and `customer_profile` drifting apart.
        """
        customer_id = str(customer_id or conversation_id)
        username = str(username or "").strip()
        display_name = str(display_name or "").strip()
        avatar_url = self._normalize_avatar_url(avatar_url)
        identity_metadata = dict(metadata or {})
        if display_name:
            identity_metadata["display_name"] = display_name
        self.register_conversation(conversation_id, customer_id, platform)
        self.register_customer(
            customer_id,
            username=username,
            platform_user_id=str(platform_user_id or "").strip(),
            metadata=identity_metadata,
        )
        if username or display_name or avatar_url:
            self.database.conn.execute(
                "INSERT INTO customer_profile("
                "account_id, conversation_id, username, display_name, avatar_url, "
                "updated_at) "
                "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP) "
                "ON CONFLICT(account_id, conversation_id) DO UPDATE SET "
                "username=COALESCE(NULLIF(excluded.username, ''), customer_profile.username), "
                "display_name=COALESCE(NULLIF(excluded.display_name, ''), "
                "customer_profile.display_name), "
                "avatar_url=COALESCE(NULLIF(excluded.avatar_url, ''), "
                "customer_profile.avatar_url), updated_at=CURRENT_TIMESTAMP",
                (self.account_id, str(conversation_id), username, display_name, avatar_url),
            )

    def export_to_sheet(self, conversation_id):
        """Push this conversation's current customer row to the Google Sheet.

        The row is assembled from two tables written on unrelated paths -
        customer_profile (DOM-scraped identity, inbox scans) and
        customer_states (stage/intent, agent replies) - so every writer of
        either calls this after committing. A conversation seen in the inbox
        but never answered has no state yet; it still gets a row, at stage
        "new", so the sheet lists every customer and not only replied ones.
        """
        conversation_id = str(conversation_id)
        profile = self.database.conn.execute(
            "SELECT username, display_name, phone, address, city FROM customer_profile "
            "WHERE account_id = %s AND conversation_id = %s",
            (self.account_id, conversation_id),
        ).fetchone() or {}
        state_row = self.database.conn.execute(
            "SELECT state_json FROM customer_states "
            "WHERE account_id = %s AND conversation_id = %s",
            (self.account_id, conversation_id),
        ).fetchone()
        if state_row:
            state = CustomerState.model_validate_json(state_row["state_json"])
        else:
            conversation = self.database.conn.execute(
                "SELECT customer_id FROM conversations "
                "WHERE account_id = %s AND platform_conversation_id = %s",
                (self.account_id, conversation_id),
            ).fetchone()
            state = CustomerState(
                customer_id=(conversation or {}).get("customer_id") or conversation_id,
                conversation_id=conversation_id,
            )
        spawn_export_customer_state(
            state,
            id_tiktok=profile.get("username"),
            customer_name=profile.get("display_name"),
            phone=profile.get("phone"),
            chat_address=format_chat_address(profile.get("address"), profile.get("city")),
            shop=self.shop_name,
        )

    # -------------------------------------------------------------- writes
    @staticmethod
    def _normalize_fact(fact_type, raw_value):
        """Normalize contact values and reject malformed model output."""
        value = " ".join(str(raw_value or "").split()).strip()
        if not value:
            return ""
        if fact_type == "phone":
            leading_plus = value.startswith("+")
            digits = re.sub(r"\D", "", value)
            if not 9 <= len(digits) <= 15:
                return ""
            return f"+{digits}" if leading_plus else digits
        if fact_type == "email":
            value = value.casefold()
            if len(value) > 254 or not re.fullmatch(
                r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
                r"@[a-z0-9-]+(?:\.[a-z0-9-]+)+",
                value,
            ):
                return ""
            return value
        if fact_type == "address":
            return value[:250] if len(value) >= 5 else ""
        if fact_type in {"name", "city"}:
            return value[:100]
        return value[:250]

    async def store_visible_messages(
        self, conversation_id, messages, customer_id=None, source="vision", *,
        platform="", username=None, display_name=None, platform_user_id=None,
        customer_metadata=None, avatar_url=None,
    ):
        stored_ids = []
        customer_id = customer_id or conversation_id
        self.upsert_customer_identity(
            conversation_id,
            customer_id,
            platform,
            username=username,
            display_name=display_name,
            platform_user_id=platform_user_id,
            metadata=customer_metadata,
            avatar_url=avatar_url,
        )
        for message in messages:
            text = str(message.text).strip()
            direction = str(message.direction).strip().upper()
            # A media-only bubble (a customer's screenshot, a product photo
            # the agent sent) has no text at all - dropping it here for lack
            # of one used to erase it from history entirely.
            media = [item for item in (getattr(message, "media", None) or ()) if item]
            if (not text and not media) or direction not in {"INBOUND", "OUTBOUND"}:
                continue
            order = getattr(message, "order", None)
            digest = self.message_hash(conversation_id, direction, text, self.account_id, order)
            message_id = f"{source}-{digest[:32]}"
            media_json = json.dumps(media, ensure_ascii=False) if media else None
            if direction == "OUTBOUND" and str(order or "").strip():
                # The same reply is written down twice: once by
                # `ReplyService.record_sent` the moment it is verified as
                # delivered (no DOM position yet, so it hashes with an empty
                # `order`), and again when a later scan re-reads the thread
                # and does have one. Two different hashes meant two rows -
                # one bubble carrying the local attachment `path`, one
                # carrying TikTok's own `src` - and the dashboard drew both.
                #
                # OUTBOUND only: `record_sent` never records what a customer
                # wrote, and doing this for INBOUND would make a customer who
                # repeats one word ("dạ") lose every occurrence after the
                # first - exactly what `order` is in the hash to prevent.
                sent_digest = self.message_hash(
                    conversation_id, direction, text, self.account_id, ""
                )
                if self._promote_sent_row(
                    conversation_id, sent_digest, digest, message_id, media_json,
                    source,
                ):
                    logger.info(
                        "[DB] account=%s reply already recorded at send time; "
                        "not stored twice conversation_id=%s",
                        self.account_id, conversation_id,
                    )
                    continue
            cursor = self.database.conn.execute(
                "INSERT INTO conversation_messages("
                "account_id, message_id, conversation_id, customer_id, direction, text, "
                "created_at, source, message_hash, media_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    self.account_id, message_id, conversation_id, customer_id, direction,
                    text, datetime.now(timezone.utc).isoformat(), source, digest, media_json,
                ),
            )
            if cursor.rowcount:
                stored_ids.append(message_id)
                logger.info(
                    "[DB] account=%s message stored conversation_id=%s",
                    self.account_id, conversation_id,
                )
            else:
                logger.info("[DB] account=%s duplicate message skipped", self.account_id)
        self.database.conn.commit()
        self.export_to_sheet(conversation_id)
        return stored_ids

    def _promote_sent_row(
        self, conversation_id, sent_digest, digest, message_id, media_json,
        source="dom",
    ):
        """Re-key the row `record_sent` wrote onto the position the DOM shows.

        Returns True when that row existed, meaning this bubble is already
        stored and must not be inserted a second time. Re-keying rather than
        merely skipping the insert is what keeps the *next* reply straight: a
        positionless row left in place would go on matching every later bubble
        with the same text, so an agent that answers "Dạ vâng ạ" twice would
        lose the second one. Once re-keyed, the ordinary
        `ON CONFLICT DO NOTHING` insert handles every further re-read.

        The local attachment `path` is never overwritten with the CDN `src` a
        DOM read found: it is the only version the dashboard can serve. The
        other direction does happen - a reply recorded with no media, whose
        picture only the re-read thread reveals - and that media is filled in.

        `source` becomes the re-read's own, so a row recorded as "unverified"
        stops being marked that way once the thread has shown it in place.
        """
        row = self.database.conn.execute(
            "SELECT id, media_json FROM conversation_messages "
            "WHERE account_id = %s AND conversation_id = %s AND message_hash = %s "
            "LIMIT 1",
            (self.account_id, conversation_id, sent_digest),
        ).fetchone()
        if row is None:
            return False
        merged = row["media_json"] or media_json
        occupied = self.database.conn.execute(
            "SELECT id FROM conversation_messages "
            "WHERE account_id = %s AND conversation_id = %s AND message_hash = %s "
            "LIMIT 1",
            (self.account_id, conversation_id, digest),
        ).fetchone()
        if occupied is not None:
            # This position is already stored (a backfill got here first).
            # Keep that row, give it whatever media the send recorded, and
            # drop the now-redundant positionless one.
            self.database.conn.execute(
                "UPDATE conversation_messages SET media_json = COALESCE(media_json, %s) "
                "WHERE account_id = %s AND id = %s",
                (merged, self.account_id, occupied["id"]),
            )
            self.database.conn.execute(
                "DELETE FROM conversation_messages WHERE account_id = %s AND id = %s",
                (self.account_id, row["id"]),
            )
            return True
        self.database.conn.execute(
            "UPDATE conversation_messages "
            "SET message_hash = %s, message_id = %s, media_json = %s, source = %s "
            "WHERE account_id = %s AND id = %s",
            (digest, message_id, merged, source, self.account_id, row["id"]),
        )
        return True

    async def merge_facts(
        self, conversation_id, facts, source_message_id, confidence,
        *, username=None, display_name=None, customer_id=None,
        platform="", platform_user_id=None, customer_metadata=None,
    ):
        self.upsert_customer_identity(
            conversation_id,
            customer_id,
            platform,
            username=username,
            display_name=display_name,
            platform_user_id=platform_user_id,
            metadata=customer_metadata,
        )
        confidence = float(confidence)
        if confidence < self.minimum_fact_confidence or not source_message_id:
            self.database.conn.commit()
            self.export_to_sheet(conversation_id)
            return 0
        saved = 0
        for fact_type, raw_value in dict(facts or {}).items():
            if fact_type not in ALLOWED_FACT_TYPES or raw_value is None:
                continue
            fact_value = self._normalize_fact(fact_type, raw_value)
            if not fact_value:
                continue
            current = self.database.conn.execute(
                "SELECT fact_value, confidence FROM customer_facts "
                "WHERE account_id = %s AND conversation_id = %s AND fact_type = %s",
                (self.account_id, conversation_id, fact_type),
            ).fetchone()
            if current and current["fact_value"] == fact_value:
                continue
            if current and confidence < float(current["confidence"]):
                continue
            self.database.conn.execute(
                "INSERT INTO customer_facts("
                "account_id, conversation_id, fact_type, fact_value, source_message_id, "
                "confidence) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT(account_id, conversation_id, fact_type) DO UPDATE SET "
                "fact_value=excluded.fact_value, source_message_id=excluded.source_message_id, "
                "confidence=excluded.confidence, updated_at=CURRENT_TIMESTAMP",
                (
                    self.account_id, conversation_id, fact_type, fact_value,
                    source_message_id, confidence,
                ),
            )
            logger.info(
                "[DB] account=%s customer fact stored type=%s", self.account_id, fact_type
            )
            saved += 1
            if fact_type in PROFILE_FACT_FIELDS:
                profile_field = "display_name" if fact_type == "name" else fact_type
                self.database.conn.execute(
                    f"INSERT INTO customer_profile(account_id, conversation_id, {profile_field}, "
                    "username, updated_at) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(account_id, conversation_id) DO UPDATE SET "
                    f"{profile_field}=excluded.{profile_field}, "
                    "username=COALESCE(NULLIF(excluded.username, ''), "
                    "customer_profile.username), "
                    "updated_at=CURRENT_TIMESTAMP",
                    (self.account_id, conversation_id, fact_value, username),
                )
                logger.info(
                    "[DB] account=%s customer profile updated field=%s",
                    self.account_id, profile_field,
                )
        self.database.conn.commit()
        self.export_to_sheet(conversation_id)
        return saved

    # --------------------------------------------------------------- reads
    async def get_customer_facts(self, conversation_id):
        rows = self.database.conn.execute(
            "SELECT fact_type, fact_value, confidence, source_message_id "
            "FROM customer_facts WHERE account_id = %s AND conversation_id = %s "
            "ORDER BY updated_at DESC",
            (self.account_id, conversation_id),
        ).fetchall()
        return {
            row["fact_type"]: {
                "value": row["fact_value"],
                "confidence": row["confidence"],
                "source_message_id": row["source_message_id"],
            }
            for row in rows
        }

    async def load_recent_messages(self, conversation_id, recent_limit=12):
        rows = self.database.conn.execute(
            "SELECT direction, text, created_at, source FROM conversation_messages "
            "WHERE account_id = %s AND conversation_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (self.account_id, conversation_id, max(1, int(recent_limit))),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    async def get_context(self, conversation_id, recent_limit=12):
        profile = self.database.conn.execute(
            "SELECT username, display_name, phone, email, address, city "
            "FROM customer_profile WHERE account_id = %s AND conversation_id = %s",
            (self.account_id, conversation_id),
        ).fetchone()
        facts = await self.get_customer_facts(conversation_id)
        recent_messages = await self.load_recent_messages(conversation_id, recent_limit)
        logger.info(
            "[CONTEXT] account=%s recent_messages=%s customer_facts=%s",
            self.account_id, len(recent_messages), len(facts),
        )
        return {
            "account_id": self.account_id,
            "customer_profile": dict(profile) if profile else {},
            "customer_facts": facts,
            "recent_messages": recent_messages,
        }
