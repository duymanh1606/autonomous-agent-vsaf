import unittest

from core.state_verifier import StateVerifier
from perception.state_parser import ParsedDomState, ParsedMessage


class _FakeDomObserver:
    """Returns one canned DOM snapshot, as if the poll settled on it."""

    def __init__(self, state):
        self.state = state

    async def wait_for(self, predicate, timeout_ms=None, poll_interval_ms=None, reason=""):
        return self.state


def _state(messages):
    return ParsedDomState(
        screen="TIKTOK_CONVERSATION", confidence=0.95, source="dom",
        sequence=99, messages=tuple(messages),
    )


class ConfirmReplyAfterInboundAnchorTests(unittest.IsolatedAsyncioTestCase):
    """A chatty customer sending more messages mid-poll must never make a
    reply that already landed look unconfirmed - see the shop3 "helloworld!!"
    incident: burst=4, three real sends misread as failed, one blocked by
    TikTok's own spam filter, ending in a human handoff."""

    async def test_real_send_is_confirmed_even_when_the_id_disappears_and_customer_keeps_typing(self):
        # The inbound this reply answers: order=5, with a `dom-*` fallback id
        # (text-derived, known to collide/shift across re-renders).
        # Our reply landed right after it, at order=6. While the poll for
        # confirmation was still running, the customer fired off two more
        # messages (order=7) - a real burst, matching the incident.
        # The re-rendered DOM no longer carries "dom-old-id" at all.
        messages = [
            ParsedMessage(text="helloworld!!", direction="INBOUND", message_id="dom-new-1", index=5),
            ParsedMessage(text="Cam on ban da nhan tin", direction="OUTBOUND", message_id="out-1", index=6),
            ParsedMessage(text="alo", direction="INBOUND", message_id="dom-new-2", index=7),
            ParsedMessage(text="co ai o do khong", direction="INBOUND", message_id="dom-new-3", index=8),
        ]
        verifier = StateVerifier(platform=None, dom_observer=_FakeDomObserver(_state(messages)))

        confirmed, state, outbound = await verifier.confirm_reply_after_inbound(
            inbound_message_id="dom-old-id",  # no longer present after the re-render
            text="Cam on ban da nhan tin",
            inbound_order=5,  # pinned before send
        )

        self.assertTrue(confirmed)
        self.assertEqual(outbound.id, "out-1")

    async def test_without_a_pinned_order_the_old_latest_inbound_fallback_still_applies(self):
        """Backward compatibility: callers that never pass `inbound_order`
        keep the previous (looser) behaviour rather than being newly broken."""
        messages = [
            ParsedMessage(text="helloworld!!", direction="INBOUND", message_id="dom-new-1", index=5),
            ParsedMessage(text="Cam on ban da nhan tin", direction="OUTBOUND", message_id="out-1", index=6),
            ParsedMessage(text="alo", direction="INBOUND", message_id="dom-new-2", index=7),
        ]
        verifier = StateVerifier(platform=None, dom_observer=_FakeDomObserver(_state(messages)))

        confirmed, state, outbound = await verifier.confirm_reply_after_inbound(
            inbound_message_id="dom-old-id",
            text="Cam on ban da nhan tin",
        )

        # No anchor was pinned, so this reproduces the old bug: the fallback
        # jumps to the newest inbound (order=7) and the real reply, sitting
        # before it, is misread as unconfirmed.
        self.assertFalse(confirmed)
        self.assertIsNone(outbound)

    async def test_id_still_resolves_directly_when_it_survives_the_rerender(self):
        messages = [
            ParsedMessage(text="helloworld!!", direction="INBOUND", message_id="dom-old-id", index=5),
            ParsedMessage(text="Cam on ban da nhan tin", direction="OUTBOUND", message_id="out-1", index=6),
        ]
        verifier = StateVerifier(platform=None, dom_observer=_FakeDomObserver(_state(messages)))

        confirmed, state, outbound = await verifier.confirm_reply_after_inbound(
            inbound_message_id="dom-old-id",
            text="Cam on ban da nhan tin",
            inbound_order=5,
        )

        self.assertTrue(confirmed)
        self.assertEqual(outbound.id, "out-1")


if __name__ == "__main__":
    unittest.main()
