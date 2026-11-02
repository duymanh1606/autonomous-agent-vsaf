"""The outcome vocabulary, and the bug class it exists to remove.

Every question the inbox scan asks about how a conversation ended used to be
answered by membership in one of five separate collections - two in the
retry/settle logic, two more inside the metrics, and a third frozenset added
later. A status could therefore be listed in four of them and forgotten in the
fifth, and that is not a test failure, it is a silent behaviour change. It is
exactly how an already-answered thread ended up escalated to a person after 30
scans.

These tests pin the properties that make the table trustworthy rather than the
table's contents (those are documented where they are declared): one row per
outcome, every outcome classified, and an unknown one read conservatively.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platforms.tiktok import scan_outcomes
from platforms.tiktok.scan_outcomes import (
    MAX_TECHNICAL_RETRIES, MAX_UNANSWERED_REVISITS, OUTCOMES, outcome_for,
)


class OutcomeVocabularyTests(unittest.TestCase):
    def test_every_status_the_scan_returns_is_in_the_table(self):
        """The check that replaces "did I remember all five places?".

        Reads the statuses straight out of the skill's source, so a new one
        added without a row fails here instead of quietly behaving like an
        unanswered customer forever.
        """
        import re

        source = (
            Path(__file__).resolve().parents[1]
            / "platforms" / "tiktok" / "inbox_watcher.py"
        ).read_text()
        returned = set(re.findall(r'"status": "([a-z_]+)"', source))

        missing = sorted(returned - set(OUTCOMES))
        self.assertEqual(missing, [], f"statuses with no row: {missing}")

    def test_a_settled_outcome_never_spends_an_attempt(self):
        """Both halves of "finished" have to agree, or a finished conversation
        still accumulates retries towards an escalation."""
        for outcome in OUTCOMES.values():
            if outcome.settled:
                self.assertFalse(
                    outcome.counts_attempt,
                    f"{outcome.name} is settled but still spends an attempt",
                )

    def test_an_unknown_status_is_read_conservatively(self):
        """A customer still owed an answer, not something that vanishes."""
        verdict = outcome_for("something_new_nobody_listed")

        self.assertFalse(verdict.settled)
        self.assertTrue(verdict.counts_attempt)
        self.assertEqual(verdict.retry_budget, MAX_UNANSWERED_REVISITS)
        self.assertEqual(verdict.bucket, scan_outcomes.UNANSWERED)
        # And it keeps its own name, so the log and the ledger say which one.
        self.assertEqual(verdict.name, "something_new_nobody_listed")

    def test_an_empty_status_still_produces_a_verdict(self):
        self.assertEqual(outcome_for(None).name, "unknown")
        self.assertEqual(outcome_for("").name, "unknown")

    def test_a_read_failure_gets_the_larger_budget(self):
        """A slow render usually fixes itself; a customer mid-burst is a
        different thing and must reach a person far sooner."""
        self.assertEqual(
            outcome_for("no_inbound_message").retry_budget, MAX_TECHNICAL_RETRIES
        )
        self.assertEqual(
            outcome_for("open_unverified").retry_budget, MAX_TECHNICAL_RETRIES
        )
        self.assertEqual(
            outcome_for("waiting_for_customer").retry_budget,
            MAX_UNANSWERED_REVISITS,
        )

    def test_yielding_the_session_is_queued_but_not_counted(self):
        verdict = outcome_for("yielded_to_priority_task")

        self.assertFalse(verdict.settled)
        self.assertFalse(verdict.counts_attempt)

    def test_the_two_outcomes_a_person_owns_are_settled(self):
        """`human_controlled` and `nothing_to_answer` are the two that were
        got wrong historically, in opposite directions."""
        self.assertTrue(outcome_for("human_controlled").settled)
        self.assertTrue(outcome_for("nothing_to_answer").settled)

    def test_only_a_real_duplicate_is_counted_as_one(self):
        duplicates = sorted(
            name for name, outcome in OUTCOMES.items() if outcome.duplicate
        )
        self.assertEqual(duplicates, ["already_handled", "already_replied"])

    def test_every_row_is_keyed_by_its_own_name(self):
        for name, outcome in OUTCOMES.items():
            self.assertEqual(name, outcome.name)


if __name__ == "__main__":
    unittest.main()
