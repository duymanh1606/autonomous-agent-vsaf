"""The recipient gate must not read "I could not see the header" as
"the wrong thread is open".

Live failure this covers (ws-bc391da5, 2026-09-21): while an attachment was
being staged, one extension-bridge read timed out
(`[EXT] rejected reason=bridge_unreadable ... Timeout 500ms exceeded`). The
observer fell back to the raw browser DOM, whose state carries no header
username at all, and the gate raised `recipient_mismatch: active chat does
not match recipient` - about the thread that was in fact the right one. The
reply was dropped and the draft (text plus a staged image) was left behind.

The gate still fails closed; it just has to re-read before it decides, and
say which of the two failures it hit.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platforms.tiktok.messaging import ReplyMessageSkill
from platforms.tiktok_web.adapter import TikTokWebAdapter


def extension_state(active="manh1"):
    """What a readable extension bridge produces: a named header."""
    return SimpleNamespace(
        source="extension", active_chat=active, active_chat_present=True,
        composer_present=True, conversations=(), messages=(),
    )


def blind_state():
    """What the raw-DOM fallback produces: a header with no username."""
    return SimpleNamespace(
        source="dom", active_chat="", active_chat_present=True,
        composer_present=True, conversations=(), messages=(),
    )


class Verifier:
    """`read_conversation` returns `reads` in order; `confirm_conversation`
    polls and returns `confirmed`, exactly like `StateVerifier` does."""

    def __init__(self, *reads, confirmed=None):
        self._reads = list(reads)
        self._confirmed = confirmed
        self.confirm_calls = 0

    async def read_conversation(self):
        return self._reads.pop(0) if self._reads else None

    async def confirm_conversation(self, target, previous_sequence=None, timeout_ms=None):
        self.confirm_calls += 1
        if self._confirmed is None:
            return False, blind_state()
        return True, self._confirmed


def adapter(verifier):
    return TikTokWebAdapter(
        browser=SimpleNamespace(), observer=SimpleNamespace(),
        state_verifier=verifier,
    )


class RecipientGateBlindReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_blind_first_read_is_re_read_instead_of_failing(self):
        verifier = Verifier(blind_state(), confirmed=extension_state("manh1"))

        state = await adapter(verifier)._require_active_recipient("manh1")

        self.assertEqual(state.active_chat, "manh1")
        self.assertEqual(verifier.confirm_calls, 1)

    async def test_a_read_that_returns_nothing_is_re_read_too(self):
        verifier = Verifier(None, confirmed=extension_state("manh1"))

        state = await adapter(verifier)._require_active_recipient("manh1")

        self.assertEqual(state.active_chat, "manh1")

    async def test_a_header_that_stays_unreadable_still_blocks_the_action(self):
        """Fail-closed is unchanged - only the reason is now accurate."""
        verifier = Verifier(blind_state())

        with self.assertRaises(RuntimeError) as raised:
            await adapter(verifier)._require_active_recipient("manh1")

        message = str(raised.exception)
        self.assertIn("recipient_mismatch", message)
        self.assertIn("header did not render", message)
        self.assertIn("source=dom", message)

    async def test_a_genuinely_wrong_thread_is_named_in_the_error(self):
        verifier = Verifier(extension_state("other_customer"))

        with self.assertRaises(RuntimeError) as raised:
            await adapter(verifier)._require_active_recipient("manh1")

        message = str(raised.exception)
        self.assertIn("active chat does not match recipient", message)
        self.assertIn("active=other_customer", message)
        self.assertIn("wanted=manh1", message)

    async def test_a_verifier_without_a_poll_behaves_exactly_as_before(self):
        verifier = SimpleNamespace(
            read_conversation=Verifier(extension_state("other_customer")).read_conversation
        )

        with self.assertRaises(RuntimeError):
            await adapter(verifier)._require_active_recipient("manh1")


class ReplySkillRecipientGateTests(unittest.IsolatedAsyncioTestCase):
    """`ReplyMessageSkill` runs its own copy of the gate before every draft
    mutation; it needs the same re-read."""

    @staticmethod
    def _context(verifier):
        return SimpleNamespace(
            platform=SimpleNamespace(state_verifier=verifier), observation=None,
        )

    async def test_a_blind_read_before_send_is_re_read(self):
        verifier = Verifier(blind_state(), confirmed=extension_state("manh1"))

        ok, reason = await ReplyMessageSkill()._verify_recipient_before_send(
            self._context(verifier), "manh1", "manh1",
        )

        self.assertTrue(ok, reason)
        self.assertEqual(reason, "recipient_and_composer_verified")

    async def test_a_real_switch_is_still_refused(self):
        verifier = Verifier(extension_state("other_customer"))

        ok, reason = await ReplyMessageSkill()._verify_recipient_before_send(
            self._context(verifier), "manh1", "manh1",
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "active_header_does_not_match_recipient")


if __name__ == "__main__":
    unittest.main()
