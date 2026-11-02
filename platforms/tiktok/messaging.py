import asyncio
import hashlib
import logging
import uuid

from pydantic import ValidationError
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from models.action import Action
from models.conversation_view import ConversationView, resolve_inbound
from models.message_delivery import (
    MessageSendState,
    SendTaskState,
    message_signature,
    reply_text_hash,
)
from models.outbound_message import Attachment
from models.result import ActionResult
from models.tasks import Task
from platforms.tiktok.screens import TikTokScreen
from platforms.tiktok_web.selectors import CHAT_HEADER_RESOURCE_IDS
from skills.base import Skill


logger = logging.getLogger(__name__)

REPLY_CAPABILITIES = frozenset({"reply_to_message", "messaging.reply"})


@dataclass(frozen=True)
class SimpleClaim:
    """Stand-in for repositories without the dedup layer (in-memory adapters)."""

    granted: bool
    state: str
    reason: str = "no_dedup_layer"
    task_id: str = ""
    attempt_count: int = 0
    signature: str = ""


class ReplyMessageSkill(Skill):
    name = "messaging"
    capabilities = [
        "send_message", "messaging.send",
        "reply_to_message", "messaging.reply",
    ]

    def __init__(self, input_action="browser.input_text", max_attachments=2):
        self.input_action = input_action
        self.max_attachments = max(1, min(10, int(max_attachments)))

    async def can_handle(self, task, context):
        # Messaging tasks must never fall through to the generic planner when
        # their payload is invalid. execute() performs the safe validation.
        return task.capability in self.capabilities

    async def execute(self, task, context):
        logger.info(
            "Agent reasoning: task_id=%s skill=messaging stage=validate_task",
            task.id,
        )
        conversation_id = str(task.payload.get("conversation_id") or "").strip()
        target = str(
            task.payload.get("recipient")
            or conversation_id
            or ""
        ).strip()
        response = str(
            task.payload.get("response")
            or task.payload.get("message")
            or task.payload.get("text")
            or ""
        ).strip()
        try:
            attachments = tuple(
                Attachment.model_validate(item)
                for item in (task.payload.get("attachments") or ())
            )
        except (ValidationError, TypeError):
            return ActionResult(success=False, error="Invalid attachment payload")
        attachment_kinds = [attachment.kind for attachment in attachments]
        if (
            len(attachments) > self.max_attachments
            or len(set(attachment_kinds)) != len(attachment_kinds)
        ):
            return ActionResult(
                success=False,
                error=(
                    f"At most {self.max_attachments} verified attachments and one per "
                    "media kind may be sent per reply"
                ),
            )

        if not target or len(target) > 200:
            return ActionResult(
                success=False,
                error="A specific conversation_id or recipient is required",
            )
        if not task.payload.get("confirm_send", False):
            return ActionResult(
                success=False,
                error="confirm_send=true is required before opening the conversation",
            )
        if task.capability in {"send_message", "messaging.send"} and not response and not attachments:
            # A person may send a picture on its own; nothing ever sends
            # neither text nor a file.
            return ActionResult(
                success=False,
                error="A message or an attachment is required; messages are never AI-generated",
            )
        if not response and not attachments:
            # Every reply is written before it gets here: by the reply
            # pipeline (analysis, product lookup, ReplyValidator) or by a
            # person. This skill used to fill an empty one in itself from the
            # raw text on screen - customer messages included, so a customer
            # could steer what the shop "said" - with none of those checks.
            # An empty task is a caller bug; refuse it before touching the UI.
            return ActionResult(
                success=False,
                error="A non-empty message is required; this skill never writes replies itself",
            )
        if len(response) > 1000:
            return ActionResult(success=False, error="Message exceeds the 1000 character limit")

        current_chat_matches = (
            context.observation.screen == TikTokScreen.CONVERSATION
            and self._conversation_matches(context.observation, target)
        )
        if current_chat_matches:
            logger.info(
                "Agent reasoning: task_id=%s skill=messaging decision=keep_conversation "
                "recipient=%s",
                task.id, target,
            )
        elif context.observation.screen != TikTokScreen.INBOX:
            logger.info(
                "Agent reasoning: task_id=%s skill=messaging decision=open_inbox evidence_screen=%s",
                task.id, context.observation.screen,
            )
            result = await context.execute_action(Action(
                name="platform.open_inbox", reason="Messaging starts from the verified Inbox"
            ))
            if not result.success:
                return self._not_sent(result)
        else:
            logger.info(
                "Agent reasoning: task_id=%s skill=messaging decision=keep_inbox evidence_screen=%s",
                task.id, context.observation.screen,
            )

        visual_input_used = False
        if not current_chat_matches:
            logger.info(
                "Agent reasoning: task_id=%s skill=messaging decision=open_conversation source=dom_first",
                task.id,
            )
            result = await context.execute_action(Action(
                name="platform.open_conversation", args={"target": target},
                reason="Open only the exact requested conversation",
            ))
            if not result.success:
                visual_target = self._exact_visual_target(
                    context.observation, "conversation", target
                )
                logger.info(
                    "Agent reasoning: task_id=%s skill=messaging stage=conversation_fallback "
                    "visual_target_confirmed=%s",
                    task.id, bool(visual_target),
                )
                if not visual_target:
                    return self._not_sent(result)
                result = await context.execute_action(Action(
                    name="browser.click_bbox", args={"target_id": visual_target.target_id},
                    reason="Use the confirmed conversation bbox only after DOM selection failed",
                    confidence=visual_target.confidence,
                ))
                if not result.success or context.observation.screen != TikTokScreen.CONVERSATION:
                    return self._not_sent(result)

        # ------------------------------------------------ DOM is the truth
        # The open conversation - not a checkpoint, not the task database and
        # not a send signature - decides whether this customer already got an
        # answer. Everything else is only a hint.
        view = await self._conversation_view(context)
        inbound = resolve_inbound(view, task.payload.get("inbound_message_id"))
        # A stranger's accepted request gets a short fixed acknowledgement
        # first and the real LLM answer to the same inbound message right
        # after - two outbound replies to one inbound by design. Without this
        # flag the ack itself would satisfy "any outbound after this inbound"
        # below and silently swallow the real answer.
        skip_already_replied = bool(task.payload.get("skip_already_replied_check"))
        if (
            task.capability in REPLY_CAPABILITIES and inbound is not None
            and not skip_already_replied
        ):
            if attachments:
                outbound = self._outbound_matching(view, inbound, response)
                media_complete = all(
                    view.outbound_media_after(inbound, kind=kind) is not None
                    for kind in dict.fromkeys(item.kind for item in attachments)
                )
                if not media_complete:
                    outbound = None
            else:
                outbound = view.outbound_after(inbound)
            if outbound is not None:
                logger.info(
                    "Agent decision: task_id=%s decision=skip_already_replied "
                    "conversation=%s inbound=%s outbound=%s",
                    task.id, target, inbound.id, outbound.id,
                )
                return ActionResult(success=True, data={
                    "decision": "skip_already_replied",
                    "status": "already_replied",
                    "conversation_id": conversation_id or target,
                    "inbound_message_id": inbound.id,
                    "outbound_message_id": outbound.id,
                })


        inbound_message_id = str(
            task.payload.get("inbound_message_id")
            or (inbound.id if inbound else "")
        )
        # Pinned before send so post-send verification never drifts its
        # anchor onto a message the customer sent *after* this reply was
        # generated - see StateVerifier.confirm_reply_after_inbound.
        inbound_order = inbound.order if inbound else None
        try:
            attachment_hashes = self._attachment_hashes(context, attachments)
        except ValueError as exc:
            result = ActionResult(
                success=False, error=str(exc), data={"status": "media_error"}
            )
            cleanup = await self._cleanup_draft(context, "media_error")
            if not cleanup.success:
                result.error = (
                    f"{result.error}; draft_cleanup_failed: "
                    f"{cleanup.error or 'cleanup was not verified'}"
                )
                result.data["draft_cleanup_verified"] = False
            return result
        inbound_watermark = str(
            task.payload.get("inbound_watermark") or inbound_message_id
        )
        signature = message_signature(
            task.platform, conversation_id or target, inbound_message_id,
            response,
            inbound_watermark=inbound_watermark,
            attachment_hashes=attachment_hashes,
        )
        repository = getattr(getattr(context, "memory", None), "repository", None)
        if repository is not None:
            claim = await self._claim_send(
                repository, task, signature,
                conversation_id=conversation_id or target,
                inbound_message_id=inbound_message_id,
                inbound_watermark=inbound_watermark,
                response=response,
                attachment_hashes=attachment_hashes,
            )
            if not claim.granted:
                # A conflicting signature is never treated as "already sent".
                return await self._resolve_signature_conflict(
                    task, context, claim, target, conversation_id,
                    inbound_message_id, response, attachments,
                )

        recipient_ok, recipient_reason = await self._verify_recipient_before_send(
            context, target, conversation_id or target
        )
        if not recipient_ok:
            return await self._abort_before_send(
                context, repository, task, signature,
                ActionResult(
                    success=False,
                    error=f"recipient_mismatch: {recipient_reason}",
                    data={"status": "recipient_mismatch"},
                ),
            )

        logger.info(
            "Agent reasoning: task_id=%s skill=messaging decision=focus_composer "
            "evidence_screen=%s",
            task.id, context.observation.screen,
        )
        result = await context.execute_action(Action(
            name="platform.focus_message_input", args={"recipient": target},
            reason="Focus the verified chat input"
        ))
        if not result.success:
            input_target = self._single_visual_target(
                context.observation, "message_input"
            )
            if not input_target:
                return await self._abort_before_send(
                    context, repository, task, signature, result
                )
            result = await context.execute_action(Action(
                name="browser.click_bbox", args={"target_id": input_target.target_id},
                reason="Focus the current high-confidence message input bbox",
                confidence=input_target.confidence,
            ))
            if not result.success or context.observation.screen != TikTokScreen.CONVERSATION:
                return await self._abort_before_send(
                    context, repository, task, signature, result
                )
            visual_input_used = True
        current_input_target = (
            self._single_visual_target(context.observation, "message_input")
            if visual_input_used else None
        )
        # A file sent on its own (an operator's photo from the dashboard) has
        # no text to type: TikTok's media dialog sends the file, and there is
        # no text message to send after it. Typing "" used to fail the whole
        # task before the file was ever staged (reported from a log:
        # "typing reply characters=0" then "Message must contain 1-1000
        # printable characters").
        media_only = not response and bool(attachments)
        if not media_only:
            logger.info(
                "[ACTION] typing reply conversation=%s characters=%s task_id=%s",
                target, len(response), task.id,
            )
            result = await context.execute_action(Action(
                name=self.input_action, args={
                    "text": response,
                    "recipient": target,
                    **({"visual_target_id": current_input_target.target_id} if current_input_target else {}),
                },
                reason="Enter the approved response for pre-send verification",
            ))
            if not result.success:
                return await self._abort_before_send(
                    context, repository, task, signature, result
                )
        sequence_before_media = self._sequence(context)
        media_baseline_ids = {
            item.id for item in view.messages
            if item.is_outbound and item.has_media
            and (inbound is None or not item.is_after(inbound))
        }
        attachments_to_send = tuple(
            attachment for attachment in attachments
            if view.outbound_media_after(inbound, kind=attachment.kind) is None
        )
        media_sent = False
        for attachment in attachments_to_send:
            recipient_ok, recipient_reason = await self._verify_recipient_before_send(
                context, target, conversation_id or target
            )
            if not recipient_ok:
                return await self._abort_before_send(
                    context, repository, task, signature,
                    ActionResult(
                        success=False,
                        error=f"recipient_mismatch: {recipient_reason}",
                        data={"status": "recipient_mismatch"},
                    ),
                    may_have_sent=media_sent,
                )
            logger.info(
                "[ACTION] staging attachment conversation=%s kind=%s name=%s task_id=%s",
                target, attachment.kind, attachment.name or "catalogue-media", task.id,
            )
            result = await context.execute_action(Action(
                name="platform.attach_message_media",
                args={"path": attachment.path, "recipient": target},
                reason="Attach only the verified local catalogue file",
            ))
            if not result.success:
                if (
                    not media_sent
                    and self._safe_text_only_fallback(response, result)
                ):
                    cleanup = await self._cleanup_draft(
                        context, "media_blocked_text_only_fallback"
                    )
                    if not cleanup.success:
                        return await self._abort_before_send(
                            context, repository, task, signature, result
                        )
                    if repository is not None:
                        await self._release_claim(
                            repository, task, signature, SendTaskState.ABORTED,
                            result.error,
                        )
                    attachments = ()
                    attachment_hashes = ()
                    signature = message_signature(
                        task.platform, conversation_id or target, inbound_message_id,
                        response, inbound_watermark=inbound_watermark,
                        attachment_hashes=(),
                    )
                    if repository is not None:
                        claim = await self._claim_send(
                            repository, task, signature,
                            conversation_id=conversation_id or target,
                            inbound_message_id=inbound_message_id,
                            inbound_watermark=inbound_watermark,
                            response=response,
                            attachment_hashes=(),
                        )
                        if not claim.granted:
                            return await self._resolve_signature_conflict(
                                task, context, claim, target, conversation_id,
                                inbound_message_id, response, (),
                            )
                    recipient_ok, recipient_reason = await self._verify_recipient_before_send(
                        context, target, conversation_id or target
                    )
                    if not recipient_ok:
                        return await self._abort_before_send(
                            context, repository, task, signature,
                            ActionResult(
                                success=False,
                                error=f"recipient_mismatch: {recipient_reason}",
                                data={"status": "recipient_mismatch"},
                            ),
                        )
                    result = await context.execute_action(Action(
                        name=self.input_action,
                        args={"text": response, "recipient": target},
                        reason="Re-enter safe text after blocked media was removed",
                    ))
                    if not result.success:
                        return await self._abort_before_send(
                            context, repository, task, signature, result
                        )
                    break
                return await self._abort_before_send(
                    context, repository, task, signature, result,
                    may_have_sent=media_sent or self._media_may_have_sent(result),
                )
            media_sent = True
        recipient_ok, recipient_reason = await self._verify_recipient_before_send(
            context, target, conversation_id or target
        )
        if not recipient_ok:
            logger.error(
                "Agent decision: task_id=%s decision=recipient_mismatch reason=%s",
                task.id, recipient_reason,
            )
            return await self._abort_before_send(
                context, repository, task, signature,
                ActionResult(
                    success=False,
                    error=f"recipient_mismatch: {recipient_reason}",
                    data={"status": "recipient_mismatch"},
                ),
                may_have_sent=media_sent,
            )
        if media_only and media_sent:
            # The file went out through TikTok's media dialog; nothing is left
            # in the composer to send. The verdict is the same one a reply
            # with text and a file gets: the file visible in the thread.
            logger.info(
                "Agent reasoning: task_id=%s skill=messaging decision=media_only_sent",
                task.id,
            )
            verify_started = monotonic()
            decision = await self._decide_after_send(
                task, context, target, response, signature,
                ActionResult(success=True, data={"status": "media_only"}),
                inbound_message_id=inbound_message_id,
                sequence_before_send=sequence_before_media,
                attachments=attachments,
                media_baseline_ids=media_baseline_ids,
                inbound_order=inbound_order,
            )
            decision.data["verify_seconds"] = monotonic() - verify_started
            return await self._finish_after_decision(
                context, repository, task, signature, decision,
            )
        logger.info(
            "Agent reasoning: task_id=%s skill=messaging decision=send_once "
            "evidence=composer_text_verified",
            task.id,
        )
        current_input_target = (
            self._single_visual_target(context.observation, "message_input")
            if visual_input_used else None
        )
        send_args = {
                "text": response,
                "conversation_id": conversation_id or target,
                "recipient": target,
                "message_signature": signature,
                **({"visual_target_id": current_input_target.target_id} if current_input_target else {}),
        }
        # The extension sequence before the send: the reply only counts as
        # landed once the extension has published a *newer* state.
        sequence_before_send = self._sequence(context)
        logger.info(
            "[ACTION] sending reply conversation=%s task_id=%s signature=%s",
            target, task.id, signature,
        )
        result = await context.execute_action(Action(
            name="platform.send_message", args=send_args,
            reason=f"Send only after conversation {target!r} and input text have been verified",
        ))
        pre_click_failure = any(
            marker in str(result.error or "").casefold()
            for marker in (
                "policy rejected", "recipient_mismatch",
                "composer text changed before send",
                "visible message editor was not found",
                "send button", "send_button", "send target",
            )
        )
        if not result.success and pre_click_failure:
            return await self._abort_before_send(
                context, repository, task, signature, result,
                may_have_sent=media_sent,
            )
        if (
            not result.success
            and result.error
            and "duplicate or unverified message send attempt" in result.error
        ):
            # Nothing went out on this attempt, but the block is about an
            # earlier one that may have: reconciling that is a person's call.
            return await self._abort_before_send(
                context, repository, task, signature, result, retry_safe=False,
            )
        # The read-back that proves the reply landed, timed apart from the
        # typing and clicking before it (agent/reply_timing.py).
        verify_started = monotonic()
        decision = await self._decide_after_send(
            task, context, target, response, signature, result,
            inbound_message_id=inbound_message_id,
            sequence_before_send=sequence_before_send,
            attachments=attachments,
            media_baseline_ids=media_baseline_ids,
            inbound_order=inbound_order,
        )
        decision.data["verify_seconds"] = monotonic() - verify_started
        return await self._finish_after_decision(context, repository, task, signature, decision)

    async def _finish_after_decision(self, context, repository, task, signature, decision):
        """Settle a send whose outcome has been judged: clear the composer if
        it did not verify, and record the claim as SENT or EXPIRED."""
        if not decision.success:
            cleanup = await self._cleanup_draft(context, "send_unverified")
            if not cleanup.success:
                decision = ActionResult(
                    success=False,
                    error=(
                        f"{decision.error or 'send_unverified'}; draft_cleanup_failed: "
                        f"{cleanup.error or 'cleanup was not verified'}"
                    ),
                    data={**(decision.data or {}), "draft_cleanup_verified": False},
                )
        if repository is not None:
            await self._release_claim(
                repository, task, signature,
                # Once Send was clicked, an unverified outcome may have landed.
                # EXPIRED forces reconciliation before any future retry.
                SendTaskState.SENT if decision.success else SendTaskState.EXPIRED,
                None if decision.success else decision.error,
            )
        return decision

    # ------------------------------------------------------------ DOM truth
    @staticmethod
    def _outbound_matching(view, inbound, text):
        """An outbound after `inbound` identifying this reply's text.

        Exact-text matching only makes sense when there is text to match.
        For a deliberate media-only reply (`text` empty) exact matching would
        never fire - `ViewMessage.matches_text` treats an empty query as
        never matching, by design, so it cannot tell "already sent" apart
        from "never sent" here. Any outbound after `inbound` is compared
        instead; callers combine this with an explicit media-kind check
        (`outbound_media_after`), which is what actually identifies a
        media-only reply's content. Same idiom as
        `core.state_verifier.StateVerifier.confirm_reply_after_inbound` and
        `bridge.protocol.ConversationSnapshot.replied_after`.
        """
        if text:
            return view.outbound_after_matching(inbound, text)
        return view.outbound_after(inbound)

    @staticmethod
    def _state_verifier(context):
        return getattr(getattr(context, "platform", None), "state_verifier", None)

    @staticmethod
    def _sequence(context):
        extension_state = (
            getattr(context.observation, "metadata", None) or {}
        ).get("extension_state") or {}
        return int(extension_state.get("sequence", 0) or 0)

    async def _conversation_view(self, context):
        """The messages currently rendered in the open thread.

        A fresh DOM read is preferred; the last observation is the fallback for
        contexts without a DOM observer. Either way the answer comes from the
        conversation itself.
        """
        verifier = self._state_verifier(context)
        reader = getattr(verifier, "read_conversation", None)
        if reader is not None:
            state = await reader()
            if state is not None:
                return ConversationView.from_dom_state(state)
        return ConversationView.from_observation(context.observation)

    @staticmethod
    def _header_named(state):
        """True when this DOM state's header names somebody - anybody.

        An unnamed header is the raw-DOM fallback the observer produces when
        the extension bridge read misses its budget; it is not evidence about
        which thread is open. A header naming the wrong customer is.
        """
        if state is None:
            return False
        active = str(
            getattr(state, "active_chat", "") or ""
        ).strip().casefold().lstrip("@")
        return bool(getattr(state, "active_chat_present", False) and active)

    async def _verify_recipient_before_send(self, context, target, conversation_id):
        """Fresh exact identity/composer gate before every draft mutation."""
        verifier = self._state_verifier(context)
        reader = getattr(verifier, "read_conversation", None)
        if reader is None:
            observation = getattr(context, "observation", None)
            if (
                observation is not None
                and observation.screen == TikTokScreen.CONVERSATION
                and self._conversation_matches(observation, target)
            ):
                return True, "exact_header_from_current_observation"
            return False, "fresh_dom_reader_unavailable"
        wanted = str(target or "").strip().casefold().lstrip("@")
        state = await reader()
        if not self._header_named(state):
            # One missed read is not evidence about which thread is open: the
            # extension bridge read has a 500ms budget and falls back to a raw
            # DOM state that carries no header username at all. Re-read before
            # concluding - a header that still does not name the recipient
            # blocks the send exactly as before, and a header naming somebody
            # else is never re-read at all.
            confirm = getattr(verifier, "confirm_conversation", None)
            if confirm is not None:
                _, polled = await confirm(target)
                if self._header_named(polled):
                    state = polled
        if state is None:
            return False, "fresh_dom_state_unavailable"
        active = str(getattr(state, "active_chat", "") or "").strip().casefold().lstrip("@")
        if not getattr(state, "active_chat_present", False) or active != wanted:
            return False, "active_header_does_not_match_recipient"
        if not getattr(state, "composer_present", False):
            return False, "composer_not_present_for_recipient"
        expected_id = str(conversation_id or "").strip()
        if expected_id and expected_id.casefold().lstrip("@") != wanted:
            matching_rows = [
                row for row in (getattr(state, "conversations", ()) or ())
                if str(getattr(row, "name", "") or "").strip().casefold().lstrip("@")
                == wanted
            ]
            stable_ids = {
                str(getattr(row, "conversation_id", "") or "").strip()
                for row in matching_rows if getattr(row, "conversation_id", "")
            }
            if stable_ids and stable_ids != {expected_id}:
                return False, "stable_conversation_id_mismatch"
        return True, "recipient_and_composer_verified"

    # -------------------------------------------------------- send dedup layer
    async def _claim_send(
        self, repository, task, signature, *, conversation_id,
        inbound_message_id, inbound_watermark, response, attachment_hashes,
    ):
        ensure_task = getattr(repository, "ensure_task", None)
        if ensure_task is not None:
            # Replies created inside an inbox scan are real send tasks; without
            # a task row their send state cannot be recorded at all.
            await ensure_task(task)
        await repository.bind_message_signature(task.id, signature)
        claim_send_task = getattr(repository, "claim_send_task", None)
        if claim_send_task is None:
            return SimpleClaim(granted=True, state=SendTaskState.IN_FLIGHT.value)
        claim = await claim_send_task(
            signature=signature,
            task_id=task.id,
            platform=task.platform,
            conversation_id=conversation_id,
            inbound_message_id=inbound_message_id,
            inbound_watermark=inbound_watermark,
            reply_hash=reply_text_hash(response),
            attachment_hashes=attachment_hashes,
        )
        logger.info(
            "Agent reasoning: task_id=%s skill=messaging stage=send_claim granted=%s "
            "state=%s reason=%s attempts=%s",
            task.id, claim.granted, claim.state, claim.reason, claim.attempt_count,
        )
        return claim

    @staticmethod
    def _attachment_hashes(context, attachments):
        """SHA-256 identities for verified catalogue files used by send dedup."""
        if not attachments:
            return ()
        browser = getattr(getattr(context, "platform", None), "browser", None)
        allowed_root = getattr(browser, "allowed_media_root", None)
        # Credential-free unit fixtures deliberately have no filesystem-backed
        # browser. Keep their signature stable without pretending this is a
        # content hash; production always wires TikTokWebAdapter.browser.
        if allowed_root is None:
            return tuple(
                "fixture:" + hashlib.sha256(
                    f"{item.kind}\0{item.path}".encode("utf-8")
                ).hexdigest()
                for item in attachments
            )
        roots = tuple(
            Path(root).resolve()
            for root in (getattr(browser, "allowed_media_roots", None) or (allowed_root,))
        )
        hashes = []
        for item in attachments:
            unresolved = Path(str(item.path or ""))
            if unresolved.is_symlink():
                raise ValueError("Attachment symlinks are not allowed")
            candidate = unresolved.resolve()
            if not any(candidate.is_relative_to(root) for root in roots):
                raise ValueError("Attachment is outside the configured media roots")
            if not candidate.is_file():
                raise ValueError("Attachment file does not exist")
            digest = hashlib.sha256()
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes.append(digest.hexdigest())
        return tuple(hashes)

    async def _release_claim(self, repository, task, signature, state, error=None):
        marker = getattr(repository, "mark_send_task", None)
        if marker is None:
            return
        await marker(signature, state, task_id=task.id, error=error)

    async def _cleanup_draft(self, context, reason):
        """Best-effort emergency cleanup used before leaving a failed reply."""
        logger.warning("[ACTION] clearing message draft reason=%s", reason)
        try:
            result = await context.execute_action(Action(
                name="platform.clear_message_draft",
                reason=f"Clear all unsent text and media after {reason}",
            ))
        except Exception as exc:
            logger.error(
                "Message draft cleanup raised reason=%s error_type=%s",
                reason, type(exc).__name__, exc_info=True,
            )
            return ActionResult(success=False, error=str(exc))
        if not result.success:
            logger.error(
                "Message draft cleanup was not verified reason=%s error=%s",
                reason, result.error,
            )
        return result

    @staticmethod
    def _safe_text_only_fallback(response, result):
        """Allow fallback only for an explicit pre-send media rejection."""
        text = " ".join(str(response or "").casefold().split())
        error = str(getattr(result, "error", "") or "").casefold()
        if not text or any(marker in text for marker in (
            "gửi ảnh", "gửi hình", "gửi video", "đính kèm",
            "attached", "attachment", "photo", "video below",
            # The same promise in Burmese ("the shop is sending you a photo").
            # Bare nouns, because Burmese writes the verb onto them without a
            # space; a false match only costs a text-only send we skip.
            "ဓာတ်ပုံ", "ရုပ်ပုံ", "ဗီဒီယို", "ဗွီဒီယို",
        )):
            return False
        if any(marker in error for marker in (
            "recipient_mismatch", "policy rejected", "confirmation was clicked",
            "modal stayed open", "send_unverified",
        )):
            return False
        return any(marker in error for marker in (
            "attachment", "media button", "media confirmation",
            "file does not exist", "mime type", "file extension",
            "video duration", "size limit", "tiktok rejected",
        ))

    @staticmethod
    def _media_may_have_sent(result):
        error = str(getattr(result, "error", "") or "").casefold()
        tool = (getattr(result, "data", None) or {}).get("tool") or {}
        confirmation = tool.get("confirmation") or {} if isinstance(tool, dict) else {}
        return bool(
            confirmation.get("clicked")
            or "confirmation was clicked" in error
            or "modal stayed open" in error
        )

    @staticmethod
    def _not_sent(result):
        """Mark a failure that provably sent nothing: Send was never pressed.

        The inbox scan retries these on a later scan instead of handing the
        conversation to a person, so this is only for failures before the
        click - never for an unverified send.
        """
        return result.model_copy(update={
            "data": {**(result.data or {}), "send_status": MessageSendState.NOT_SENT.value},
        })

    async def _abort_before_send(
        self, context, repository, task, signature, result, *, may_have_sent=False,
        retry_safe=True,
    ):
        """Clear the draft and release only claims that are safe to retry.

        A clean abort (nothing may have gone out, the draft verified empty)
        is marked NOT_SENT so the scan retries it; `retry_safe=False` keeps a
        block that is about an earlier attempt with a person.
        """
        if may_have_sent:
            # Said out loud for the operator: a failure after a file went out
            # is "check TikTok", not "not delivered" (see _graph_finalize).
            result = result.model_copy(update={
                "data": {**(result.data or {}), "send_status": "UNCERTAIN"},
            })
        cleanup = await self._cleanup_draft(
            context, (result.data or {}).get("status") or result.error or "pre_send_failure"
        )
        if repository is not None:
            await self._release_claim(
                repository, task, signature,
                SendTaskState.EXPIRED if may_have_sent else SendTaskState.ABORTED,
                result.error,
            )
        if not cleanup.success:
            return ActionResult(
                success=False,
                error=(
                    f"{result.error or 'Reply aborted'}; draft_cleanup_failed: "
                    f"{cleanup.error or 'cleanup was not verified'}"
                ),
                data={**(result.data or {}), "draft_cleanup_verified": False},
            )
        if retry_safe and not may_have_sent:
            return self._not_sent(result)
        return result

    async def _resolve_signature_conflict(
        self, task, context, claim, target, conversation_id, inbound_message_id,
        response, attachments=(),
    ):
        """A refused claim only decides *after* the conversation was re-read.

        `signature conflict -> re-observe -> outbound after the latest inbound?`
        Yes: the customer has an answer, mark the message handled. No: the old
        send task decides, and only `SENT`/`IN_FLIGHT` may block a retry.
        """
        view = await self._conversation_view(context)
        inbound = resolve_inbound(view, inbound_message_id)
        outbound = self._outbound_matching(view, inbound, response)
        if attachments and outbound is not None:
            if not all(
                view.outbound_media_after(inbound, kind=kind) is not None
                for kind in dict.fromkeys(item.kind for item in attachments)
            ):
                outbound = None
        logger.info(
            "Agent reasoning: task_id=%s skill=messaging stage=signature_conflict "
            "claim_state=%s reason=%s inbound=%s outbound_after_inbound=%s",
            task.id, claim.state, claim.reason,
            inbound.id if inbound else "none", outbound.id if outbound else "none",
        )
        if outbound is not None:
            return ActionResult(success=True, data={
                "decision": "skip_already_replied",
                "status": "already_replied",
                "conversation_id": conversation_id or target,
                "inbound_message_id": inbound.id if inbound else inbound_message_id,
                "outbound_message_id": outbound.id,
                "signature_conflict": claim.reason,
            })
        if claim.state == SendTaskState.SENT.value:
            # The dedup layer says SENT while the thread shows no answer after
            # the inbound message. Sending again could duplicate a delivered
            # reply, so this is escalated instead of retried silently.
            return ActionResult(
                success=False,
                error=(
                    "send task is marked SENT but the conversation shows no reply "
                    "after the latest inbound message; manual verification is required"
                ),
                data={
                    "decision": "abort_task",
                    "status": "sent_without_dom_evidence",
                    "message_signature": claim.signature,
                    "conversation_id": conversation_id or target,
                },
            )
        if claim.state == SendTaskState.EXPIRED.value:
            return ActionResult(
                success=False,
                error=(
                    "an earlier send claim expired and the thread does not prove "
                    "delivery; manual reconciliation is required"
                ),
                data={
                    "decision": "reconcile_before_retry",
                    "status": "send_unverified",
                    "send_task_state": claim.state,
                    "message_signature": claim.signature,
                    "conversation_id": conversation_id or target,
                },
            )
        return ActionResult(
            success=False,
            error=(
                f"another send task for this reply is {claim.state}; "
                "not sending a second copy"
            ),
            data={
                "decision": "wait_for_in_flight_send",
                "status": "send_in_flight",
                "send_task_state": claim.state,
                "message_signature": claim.signature,
                "conversation_id": conversation_id or target,
            },
        )

    async def _confirm_reply_landed(
        self, context, inbound_message_id, response, previous_sequence,
        attachments=(), media_baseline_ids=(), inbound_order=None,
    ):
        """`outbound after the inbound message` is the only accepted proof."""
        verifier = self._state_verifier(context)
        confirm = getattr(verifier, "confirm_reply_after_inbound", None)
        if confirm is not None and inbound_message_id:
            confirmed, state, outbound = await confirm(
                inbound_message_id=inbound_message_id or None,
                text=response,
                previous_sequence=previous_sequence,
                inbound_order=inbound_order,
            )
            if state is not None and not attachments:
                return bool(confirmed), (outbound.id if outbound else None)
        if verifier is not None and getattr(verifier, "read_conversation", None):
            state = await verifier.read_conversation()
            view = ConversationView.from_dom_state(state) if state is not None else ConversationView()
        else:
            view = ConversationView.from_observation(context.observation)
        inbound = resolve_inbound(view, inbound_message_id)
        if inbound is None and not inbound_message_id:
            outbound = next(
                (
                    item for item in reversed(view.messages)
                    if item.is_outbound
                    # Same empty-response idiom as `_outbound_matching`: with
                    # no text to match, any outbound message is the signal -
                    # the media-kind check below is what actually identifies
                    # a media-only reply's content.
                    and (not response or item.matches_text(response))
                ),
                None,
            )
        else:
            outbound = self._outbound_matching(view, inbound, response)
        if attachments:
            if inbound is None and not inbound_message_id:
                excluded = set(media_baseline_ids or ())
                media_outbounds = [
                    next((
                        item for item in view.messages
                        if item.is_outbound and item.has_media and item.id not in excluded
                        and any(
                            str(media.get("kind") or media.get("type") or "").casefold()
                            == kind
                            for media in item.media
                        )
                    ), None)
                    for kind in dict.fromkeys(a.kind for a in attachments)
                ]
            else:
                media_outbounds = [
                    view.outbound_media_after(
                        inbound, kind=kind, excluding_ids=media_baseline_ids,
                    )
                    for kind in dict.fromkeys(a.kind for a in attachments)
                ]
            confirmed = outbound is not None and all(media_outbounds)
            last_media = next(
                (item for item in reversed(media_outbounds) if item is not None), None
            )
            return confirmed, (last_media.id if last_media else None)
        return outbound is not None, (outbound.id if outbound else None)

    @staticmethod
    def _conversation_matches(observation, target):
        """Match only a trusted visible chat header, never arbitrary body text."""
        normalized = str(target).strip().casefold().lstrip("@")
        if not normalized:
            return False
        return any(
            str(element.get("resource_id", "")).casefold()
            in CHAT_HEADER_RESOURCE_IDS
            and str(element.get("text", "")).strip().casefold().lstrip("@") == normalized
            for element in observation.elements
        )

    async def _verified_result(
        self, task, context, signature, response, inbound_message_id,
        sequence_before_send, *, attempts=1, evidence="SENT", attachments=(),
        media_baseline_ids=(), inbound_order=None,
    ):
        """Turn a `SENT` classification into a verdict backed by the thread."""
        landed, outbound_id = await self._confirm_reply_landed(
            context, inbound_message_id, response, sequence_before_send,
            attachments, media_baseline_ids, inbound_order=inbound_order,
        )
        logger.info(
            "Agent decision: task_id=%s decision=%s evidence=%s attempts=%s "
            "outbound_after_inbound=%s",
            task.id, "complete_task" if landed else "send_unverified", evidence,
            attempts, outbound_id or "none",
        )
        if landed:
            logger.info(
                "[VERIFY] reply sent successfully task_id=%s outbound=%s attempts=%s",
                task.id, outbound_id or "unknown", attempts,
            )
            return ActionResult(success=True, data={
                "decision": "complete_task",
                "send_status": "SENT",
                "verified": True,
                "message_signature": signature,
                "inbound_message_id": inbound_message_id,
                "outbound_message_id": outbound_id,
                "send_attempt_count": attempts,
                "verification_source": context.observation.perception_source,
            })
        reply_visible = await self._reply_visible(context, response)
        return ActionResult(
            success=False,
            error=(
                "send was not confirmed: no outbound message appeared after "
                "the inbound message in the open conversation"
            ),
            data={
                "decision": "abort_task",
                "send_status": "UNCERTAIN",
                "verified": False,
                "message_signature": signature,
                "inbound_message_id": inbound_message_id,
                "send_attempt_count": attempts,
                "reply_visible": reply_visible,
            },
        )

    async def _reply_visible(self, context, response):
        """True when the newest bubble in the open thread is this reply.

        Weaker than `_confirm_reply_landed` - it cannot place the reply after
        the inbound it answers, typically because the thread rendered only
        part of itself - so it never makes the send count as verified or
        allows a retry. It only lets the caller write down what the customer
        can already see on TikTok, instead of leaving it out of the history.
        """
        if not str(response or "").strip():
            return False
        try:
            view = await self._conversation_view(context)
        except Exception:
            logger.exception("[VERIFY] reading the thread for a visible reply failed")
            return False
        last = view.messages[-1] if view.messages else None
        return bool(last is not None and last.is_outbound and last.matches_text(response))

    async def _decide_after_send(
        self, task, context, target, response, signature, send_result,
        inbound_message_id="", sequence_before_send=None, attachments=(),
        media_baseline_ids=(), inbound_order=None,
    ):
        observation = context.observation
        logger.info(
            "Agent reasoning: task_id=%s skill=messaging stage=post_send_decision "
            "screen=%s send_state=%s composer_state=%s text_matches=%s source=%s",
            task.id,
            observation.screen,
            observation.message_send_state.value,
            observation.composer_state.value,
            observation.composer_text_matches_expected,
            observation.perception_source,
        )
        if observation.auth_state.value != "LOGGED_IN" or observation.security_challenge:
            logger.warning(
                "Agent decision: task_id=%s decision=abort_task reason=auth_or_security",
                task.id,
            )
            return ActionResult(
                success=False,
                error="Authentication/security state changed after send; task paused",
            )
        if observation.screen != TikTokScreen.CONVERSATION:
            logger.warning(
                "Agent decision: task_id=%s decision=recover_navigation",
                task.id,
            )
            return ActionResult(
                success=False,
                error="recover_navigation: browser left the verified conversation",
            )
        if observation.message_send_state == MessageSendState.SENT:
            # `SENT` from the send-state classifier is evidence, not the
            # verdict: the reply must be visible in the thread *after* the
            # inbound message it answers.
            return await self._verified_result(
                task, context, signature, response, inbound_message_id,
                sequence_before_send, attachments=attachments,
                media_baseline_ids=media_baseline_ids, inbound_order=inbound_order,
            )

        # A staged attachment may have been accepted even when TikTok's text
        # classifier cannot prove it. Never press Send a second time in that
        # situation: one ambiguous image is safer than a duplicate image.
        if attachments:
            return await self._verified_result(
                task, context, signature, response, inbound_message_id,
                sequence_before_send, evidence="media_send_observation",
                attachments=attachments, media_baseline_ids=media_baseline_ids,
                inbound_order=inbound_order,
            )

        if observation.message_send_state == MessageSendState.NOT_SENT and (
            observation.composer_text_matches_expected is True
        ):
            logger.info(
                "Agent decision: task_id=%s decision=retry_send evidence=NOT_SENT_and_text_retained",
                task.id,
            )
            retry = await context.execute_action(Action(
                name="platform.retry_send",
                args={
                    "text": response,
                    "conversation_id": target,
                    "recipient": target,
                    "message_signature": signature,
                },
                reason="One policy-gated retry after clear NOT_SENT evidence",
            ))
            if context.observation.message_send_state == MessageSendState.SENT:
                return await self._verified_result(
                    task, context, signature, response, inbound_message_id,
                    sequence_before_send, attempts=2, evidence="SENT_after_retry",
                    inbound_order=inbound_order,
                )
            return ActionResult(
                success=False,
                error=(
                    retry.error
                    or "Message remained unverified after the single permitted retry"
                ),
                data={"decision": "abort_task", "send_status": (
                    context.observation.message_send_state.value
                )},
            )

        # UNCERTAIN never triggers an immediate second Enter. Wait once and
        # re-observe using DOM first and Vision only if evidence remains weak.
        logger.info(
            "Agent decision: task_id=%s decision=wait_then_reobserve evidence=UNCERTAIN",
            task.id,
        )
        await asyncio.sleep(1.0)
        context.observation = await context.observer.observe(
            force_vision=True,
            after_action="platform.send_message",
            expected_message=response,
        )
        context.state.update_observation(context.observation)
        repository = getattr(getattr(context, "memory", None), "repository", None)
        if repository is not None:
            await repository.update_send_verification(
                task.id,
                signature,
                context.observation.message_send_state.value,
                context.observation.perception_source,
            )
        if context.observation.message_send_state == MessageSendState.SENT:
            return await self._verified_result(
                task, context, signature, response, inbound_message_id,
                sequence_before_send, evidence="SENT_after_reobserve",
                inbound_order=inbound_order,
            )
        if context.observation.message_send_state == MessageSendState.NOT_SENT and (
            context.observation.composer_text_matches_expected is True
        ):
            logger.info(
                "Agent decision: task_id=%s decision=retry_send evidence=NOT_SENT_after_reobserve",
                task.id,
            )
            retry = await context.execute_action(Action(
                name="platform.retry_send",
                args={
                    "text": response,
                    "conversation_id": target,
                    "recipient": target,
                    "message_signature": signature,
                },
                reason="Retry after wait/reobserve produced clear NOT_SENT evidence",
            ))
            if context.observation.message_send_state == MessageSendState.SENT:
                return await self._verified_result(
                    task, context, signature, response, inbound_message_id,
                    sequence_before_send, attempts=2, evidence="SENT_after_retry",
                    inbound_order=inbound_order,
                )
            return ActionResult(
                success=False,
                error=(retry.error or "Single retry could not be verified as SENT"),
                data={
                    "decision": "abort_task",
                    "send_status": context.observation.message_send_state.value,
                },
            )
        logger.warning(
            "Agent decision: task_id=%s decision=abort_task evidence=still_UNCERTAIN",
            task.id,
        )
        return ActionResult(
            success=False,
            error="UNCERTAIN: send was not retried; manual verification is required",
            data={
                "decision": "wait_then_reobserve",
                "send_status": context.observation.message_send_state.value,
                "message_signature": signature,
            },
        )

    @staticmethod
    def _single_visual_target(observation, target_type):
        matches = [
            target for target in observation.visual_targets
            if target.type == target_type
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _exact_visual_target(observation, target_type, text):
        wanted = text.strip().casefold().lstrip("@")
        matches = [
            target for target in observation.visual_targets
            if target.type == target_type
            and target.text.strip().casefold().lstrip("@") == wanted
        ]
        return matches[0] if len(matches) == 1 else None


class BatchMessageSkill(Skill):
    """Preview and enqueue reviewed recipients; never sends inside the parent task."""

    name = "messaging_batch"
    capabilities = ["send_message_batch", "messaging.batch"]

    async def can_handle(self, task, context):
        return task.capability in self.capabilities

    async def execute(self, task, context):
        logger.info(
            "Agent reasoning: task_id=%s skill=messaging_batch stage=validate_batch",
            task.id,
        )
        if task.platform != "tiktok_web":
            return ActionResult(
                success=False, error="Batch messaging is available only for TikTok Web"
            )
        count = task.payload.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 5:
            return ActionResult(
                success=False, error="Batch count must be an integer between 1 and 5"
            )
        preview_only = task.payload.get("preview_only", False) is True
        approved = task.payload.get("approved_recipients")
        if not preview_only and not isinstance(approved, list):
            return ActionResult(
                success=False,
                error="Preview first, then provide approved_recipients for the batch",
            )

        if context.observation.screen != TikTokScreen.INBOX:
            logger.info(
                "Agent reasoning: task_id=%s skill=messaging_batch decision=open_inbox",
                task.id,
            )
            opened = await context.execute_action(Action(
                name="platform.open_inbox",
                reason="Batch preview requires the verified Inbox",
            ))
            if not opened.success:
                return opened

        logger.info(
            "Agent reasoning: task_id=%s skill=messaging_batch decision=preview_first_n count=%s",
            task.id, count,
        )
        listed = await context.execute_action(Action(
            name="platform.list_conversations",
            args={"limit": count},
            reason="Read exact visible conversation names without sending",
        ))
        if not listed.success:
            return listed
        conversations = list(listed.data.get("tool", {}).get("conversations", []))
        if len(conversations) != count:
            return ActionResult(
                success=False, error="Conversation preview did not return the requested count"
            )
        if preview_only:
            logger.info(
                "Agent decision: task_id=%s decision=await_batch_approval recipient_count=%s",
                task.id, count,
            )
            return ActionResult(success=True, data={
                "decision": "await_batch_approval",
                "recipient_count": count,
                "approved_recipients": conversations,
            })

        approved_names = [str(item).strip() for item in approved]
        if len(approved_names) != count or approved_names != conversations:
            logger.warning(
                "Agent decision: task_id=%s decision=abort_batch reason=inbox_order_changed",
                task.id,
            )
            return ActionResult(
                success=False,
                error="The first conversations no longer exactly match approved_recipients; preview again",
            )
        message = str(task.payload.get("message") or task.payload.get("text") or "").strip()
        if not message or len(message) > 1000:
            return ActionResult(
                success=False, error="Batch message must contain 1-1000 characters"
            )
        if task.payload.get("confirm_send") is not True:
            return ActionResult(
                success=False, error="confirm_send=true is required for an approved batch"
            )

        repository = getattr(getattr(context, "memory", None), "repository", None)
        if repository is None:
            return ActionResult(success=False, error="Task repository is required for batch enqueue")
        child_ids = []
        for recipient in approved_names:
            child = Task(
                id=str(uuid.uuid4()),
                account_id=task.account_id,
                capability="send_message",
                platform=task.platform,
                priority=task.priority,
                payload={
                    "recipient": recipient,
                    "message": message,
                    "confirm_send": True,
                    "batch_parent_id": task.id,
                },
            )
            await repository.submit(child)
            child_ids.append(child.id)
        logger.info(
            "Agent decision: task_id=%s decision=enqueue_individual_sends child_count=%s",
            task.id, len(child_ids),
        )
        return ActionResult(success=True, data={
            "decision": "batch_enqueued",
            "recipient_count": len(child_ids),
            "child_task_ids": child_ids,
        })
