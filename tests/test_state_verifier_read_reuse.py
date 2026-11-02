"""One thread read per page state, not one per check.

Four different guards used to read the whole thread within a couple of
hundred milliseconds of each other before a single reply was typed: the
messaging skill's recipient check, the adapter's own, the typing gate, and
the stale-draft guard. Each is a full parse of every bubble - 0.12s p50 and
0.34s p95 measured on a 25-message thread - and all four ask about the same
instant.

The read is now reused while the extension's `sequence` has not moved. That
sequence is the page's own version number, so an unchanged one means the next
read would return the identical payload; every mutation makes the extension
publish and moves it. These tests pin that the reuse is driven by the page and
not by a guessed lifetime - the safety property is "the DOM as it is
immediately before I act", and it must still hold.
"""

import sys
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.state_verifier as module
from core.state_verifier import StateVerifier


def state(sequence, *, active="customer", trusted=True, messages_open=True):
    return SimpleNamespace(
        sequence=sequence, active_chat=active, active_chat_present=True,
        composer_present=True, messages_open=messages_open,
        thread_trusted=trusted, thread_source="extension", thread_nodes=3,
        thread_notices=0,
    )


class Observer:
    """A DOM observer plus the browser's published sequence, as the real pair."""

    def __init__(self, states):
        self._states = list(states)
        self.reads = 0
        self.published = self._states[0].sequence
        self.browser = SimpleNamespace(
            current_extension_sequence=self._current_sequence
        )

    async def observe_state(self):
        self.reads += 1
        index = min(self.reads - 1, len(self._states) - 1)
        return self._states[index]

    async def _current_sequence(self):
        return self.published


class ReadReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_second_check_of_the_same_page_state_costs_nothing(self):
        observer = Observer([state(10), state(10)])
        verifier = StateVerifier(platform=None, dom_observer=observer)

        first = await verifier.read_conversation()
        second = await verifier.read_conversation()

        self.assertIs(first, second)
        self.assertEqual(observer.reads, 1)

    async def test_a_page_that_moved_is_read_again(self):
        """Every mutation makes the extension publish, so this is the case
        that must never be served from the previous read."""
        observer = Observer([state(10), state(11, active="other_customer")])
        verifier = StateVerifier(platform=None, dom_observer=observer)

        await verifier.read_conversation()
        observer.published = 11
        second = await verifier.read_conversation()

        self.assertEqual(observer.reads, 2)
        self.assertEqual(second.active_chat, "other_customer")

    async def test_force_always_reads(self):
        observer = Observer([state(10), state(10)])
        verifier = StateVerifier(platform=None, dom_observer=observer)

        await verifier.read_conversation()
        await verifier.read_conversation(force=True)

        self.assertEqual(observer.reads, 2)

    async def test_a_frozen_sequence_is_not_trusted_for_long(self):
        """An extension that stopped publishing leaves the sequence
        unchanged, and that means "no news" rather than "nothing changed"."""
        observer = Observer([state(10), state(10)])
        verifier = StateVerifier(platform=None, dom_observer=observer)
        await verifier.read_conversation()

        with mock.patch.object(module, "MAX_REUSE_SECONDS", -1):
            await verifier.read_conversation()

        self.assertEqual(observer.reads, 2)

    async def test_a_raw_dom_state_is_never_reused(self):
        """It carries no sequence, so there is nothing to compare against."""
        observer = Observer([state(0), state(0)])
        verifier = StateVerifier(platform=None, dom_observer=observer)

        await verifier.read_conversation()
        await verifier.read_conversation()

        self.assertEqual(observer.reads, 2)

    async def test_an_untrusted_thread_is_rejected_and_not_remembered(self):
        """A rejected read must not become the answer to the next question."""
        observer = Observer([state(10, trusted=False), state(11)])
        verifier = StateVerifier(platform=None, dom_observer=observer)

        self.assertIsNone(await verifier.read_conversation())
        await verifier.read_conversation()

        self.assertEqual(observer.reads, 2)

    async def test_no_sequence_reader_means_no_reuse(self):
        """Without the browser's own sequence there is no evidence the page
        stood still, so the conservative path is to read again."""
        observer = Observer([state(10), state(10)])
        observer.browser = SimpleNamespace()
        verifier = StateVerifier(platform=None, dom_observer=observer)

        await verifier.read_conversation()
        await verifier.read_conversation()

        self.assertEqual(observer.reads, 2)

    async def test_a_sequence_reader_that_throws_means_no_reuse(self):
        async def broken():
            raise RuntimeError("bridge is gone")

        observer = Observer([state(10), state(10)])
        observer.browser = SimpleNamespace(current_extension_sequence=broken)
        verifier = StateVerifier(platform=None, dom_observer=observer)

        await verifier.read_conversation()
        await verifier.read_conversation()

        self.assertEqual(observer.reads, 2)


if __name__ == "__main__":
    unittest.main()
