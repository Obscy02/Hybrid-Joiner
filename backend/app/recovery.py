"""
Two distinct kinds of "this job is stuck and nobody's coming back to it",
handled here and in graph_actions.py respectively:

  - claimed/running (this module): the connector claimed the job but
    crashed, restarted, or hit a genuinely unexpected error before ever
    calling onprem-complete or /fail. The specific New-ADUser failure path
    in Connector-Agent.ps1 already reports cleanly via /fail - this covers
    everything else, the errors nobody anticipated, which is exactly the
    kind that isn't caught by a specific try/catch. The connector that
    died can't be trusted to notice this about itself; only the backend,
    watching the clock, can.
  - awaiting_cloud_steps: see graph_actions.find_interrupted_jobs().

Resetting an abandoned claim back to "pending" is safe, not just
convenient: Connector-Agent.ps1's own pre-flight duplicate check
(UserPrincipalName/SamAccountName collision) means a retry that lands on
an account a silently-dead first attempt actually finished creating will
cleanly abort with "already exists" and report failure properly, rather
than create a duplicate account. That existing safety check doubles as an
idempotency guard for this retry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from .audit import log_action
from .models import Job, JobStatus

# Generous margin over how long on-prem AD work actually takes (typically
# well under a couple of minutes) - long enough that a still-working
# connector is never preempted, short enough that a genuinely abandoned
# job doesn't sit unnoticed for long. Matches graph_actions.py's analogous
# CLOUD_STEPS_CLAIM_STALE_AFTER for consistency.
CLAIM_STALE_AFTER = timedelta(minutes=20)


def reset_abandoned_claims(session: Session) -> list[int]:
    """Finds jobs claimed more than CLAIM_STALE_AFTER ago that never
    reached onprem-complete or /fail, and resets them to pending so a
    healthy connector can pick them up again. Uses claimed_at specifically
    (set once, at claim time) rather than updated_at, which - for a job
    that was claimed but never got a single /log call - would still
    reflect its original creation time, not when it was claimed.
    """
    cutoff = datetime.now(timezone.utc) - CLAIM_STALE_AFTER
    stale_jobs = session.exec(
        select(Job).where(
            Job.status.in_([JobStatus.claimed, JobStatus.running]),
            Job.claimed_at != None,  # noqa: E711 - should always be set in these statuses, but be explicit
            Job.claimed_at < cutoff,
        )
    ).all()

    reset_ids = []
    for job in stale_jobs:
        minutes = int(CLAIM_STALE_AFTER.total_seconds() // 60)
        job.log_lines = [
            *job.log_lines,
            f"[WARN] No progress for over {minutes} minutes after being claimed - "
            "assuming the connector that claimed this died, resetting to pending for retry.",
        ]
        job.status = JobStatus.pending
        job.claimed_at = None
        session.add(job)
        reset_ids.append(job.id)

    session.commit()
    for job_id in reset_ids:
        log_action(session, "system", "job.reset_abandoned_claim", detail=f"job_id={job_id}")
    return reset_ids
