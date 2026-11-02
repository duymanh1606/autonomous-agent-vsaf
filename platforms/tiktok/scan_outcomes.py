"""Every way one conversation can end an inbox scan, and what each one means.

An outcome used to be a bare string compared against five separate
collections: `SETTLED_STATUSES`, `TECHNICAL_RETRY_STATUSES`,
`NEUTRAL_RETRY_STATUSES`, and `completed_names`/`pending_names` inside the
metrics. Adding one status therefore meant editing four places, and
forgetting one of them was a silent bug rather than an error - which is
exactly how an already-answered thread ended up escalated to a person after
30 scans: the status said "could not read this", every set agreed, and
nothing in the code held the opposite opinion.

So the vocabulary lives here, one row per outcome, and every question the
scan asks about an outcome is answered from that row:

* `settled` - nothing more is owed to this customer, so no retry row;
* `counts_attempt` - whether it spends one of the revisits that eventually
  hand the conversation to a person. False for an outcome that says nothing
  about the *thread* (the agent yielding its Chrome session to a staff reply
  is the agent's own business, not a sign the thread is unanswerable);
* `retry_budget` - how many attempts before a person is told;
* `bucket` - which number it adds to in the scan metrics;
* `duplicate` - it was already answered, counted separately for the operator.

An outcome nobody listed falls back to `DEFAULT`, which is the conservative
reading: not settled, spends an attempt, ordinary budget, counted as
unanswered. A new status therefore behaves like a customer still owed an
answer until somebody says otherwise, instead of vanishing from the ledger.
"""

from dataclasses import dataclass


# How many times a conversation that was read but not answered is re-opened
# before a person is told. Low on purpose: each revisit costs a scan slot.
MAX_UNANSWERED_REVISITS = 3

# The higher ceiling for outcomes that are the agent failing to *read* the
# screen rather than anything about the customer - a slow render, a click that
# missed. Retrying those is usually what fixes them, so they get far more
# attempts before a person is involved. A read still failing after this many
# was never a slow render.
MAX_TECHNICAL_RETRIES = 30

# Metrics buckets, as the dashboard reads them.
COMPLETED = "completed"
PENDING = "pending"
UNANSWERED = "unanswered"
SEND_UNVERIFIED = "send_unverified"
REQUIRES_HUMAN = "requires_human"
FAILED = "failed"
POLICY_REJECTED = "policy_rejected"


@dataclass(frozen=True)
class ScanOutcome:
    name: str
    settled: bool = False
    counts_attempt: bool = True
    retry_budget: int = MAX_UNANSWERED_REVISITS
    bucket: str = UNANSWERED
    duplicate: bool = False


def _settled(name, *, bucket=COMPLETED, duplicate=False):
    return ScanOutcome(
        name, settled=True, counts_attempt=False, bucket=bucket,
        duplicate=duplicate,
    )


OUTCOMES = {outcome.name: outcome for outcome in (
    # ------------------------------------------------------------- finished
    _settled("sent"),
    _settled("already_replied", duplicate=True),
    _settled("already_handled", duplicate=True),
    # The thread renders fine and its newest message is ours: there is simply
    # nothing unanswered in it. A finished conversation, not a read failure.
    _settled("nothing_to_answer"),
    # The asynchronous pipeline owns the reply from here.
    _settled("handed_to_pipeline"),
    # A send whose delivery could not be proven either way; the reconcile
    # path owns it and must not be retried blindly from here.
    _settled("reconcile_required", bucket=UNANSWERED),
    # A person owns this conversation. Never an agent retry, never an
    # escalation - it is already where it needs to be.
    _settled("human_controlled"),
    # Opened only to copy a thread read on another device (the TikTok app) into
    # the database; its newest customer message is recorded as handled so the
    # agent never answers what a person on that device already saw.
    _settled("synced_read_elsewhere"),

    # -------------------------------------------- still owed, still queued
    # The customer is mid-burst. Counted, because a thread that never settles
    # does eventually need a person to look at it.
    ScanOutcome("waiting_for_customer", bucket=PENDING),
    ScanOutcome("send_in_flight", bucket=PENDING),
    ScanOutcome("request_open_failed", bucket=PENDING),
    ScanOutcome("request_accept_failed", bucket=PENDING),
    # The agent gave up its Chrome session because a staff reply was queued
    # behind it. That says nothing about this thread, so it must not spend a
    # retry: counting it would escalate healthy conversations on a busy
    # workspace.
    ScanOutcome(
        "yielded_to_priority_task", counts_attempt=False, bucket=PENDING,
    ),
    # The send failed before Send was pressed (chat would not open, wrong
    # recipient on screen, composer would not take the text), so nothing
    # reached the customer and trying again cannot duplicate a reply. Retried
    # on later scans; only a run of these hands the thread to a person.
    ScanOutcome("send_not_attempted", bucket=UNANSWERED),

    # ------------------------------------------- the agent could not read it
    ScanOutcome(
        "no_inbound_message", retry_budget=MAX_TECHNICAL_RETRIES,
        bucket=UNANSWERED,
    ),
    ScanOutcome(
        "open_unverified", retry_budget=MAX_TECHNICAL_RETRIES,
        bucket=UNANSWERED,
    ),

    # ------------------------------------------------- reported, not retried
    ScanOutcome("send_unverified", bucket=SEND_UNVERIFIED),
    ScanOutcome("requires_human", bucket=REQUIRES_HUMAN),
    ScanOutcome("failed", bucket=FAILED),
    ScanOutcome("policy_rejected", bucket=POLICY_REJECTED),
    # A reply the pipeline deliberately did not write.
    ScanOutcome("no_reply", bucket=COMPLETED),
    # The fixed courtesy line after accepting a message request could not be
    # sent, because this runtime has no reply skill or no configured prompt.
    # It is a one-shot courtesy that never settles the conversation and is
    # never retried on its own (`_send_accept_ack_reply`); the stranger's real
    # answer follows immediately through the ordinary pipeline and is what the
    # ledger tracks. Listed with the conservative defaults so the vocabulary
    # is complete - being complete is the whole point of the table.
    ScanOutcome("ack_not_sent", bucket=UNANSWERED),
)}

DEFAULT = ScanOutcome("unknown")


def outcome_for(status) -> ScanOutcome:
    """What `status` means, or the conservative reading of an unknown one."""
    name = str(status or "").strip()
    listed = OUTCOMES.get(name)
    if listed is not None:
        return listed
    return ScanOutcome(
        name or DEFAULT.name,
        settled=DEFAULT.settled,
        counts_attempt=DEFAULT.counts_attempt,
        retry_budget=DEFAULT.retry_budget,
        bucket=DEFAULT.bucket,
    )
