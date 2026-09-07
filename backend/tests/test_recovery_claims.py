"""
Tests for reset_abandoned_claims() - the connector-side counterpart to
find_interrupted_jobs()/run_cloud_steps()'s recovery. Covers the gap
found while reviewing Connector-Agent.ps1: its outer poll-loop try/catch
swallows any error Invoke-JoinerJob doesn't specifically anticipate, so a
genuinely unexpected failure leaves a job claimed forever with no /fail
ever called - only the backend, watching claimed_at, can notice that.
"""
from datetime import datetime, timedelta, timezone

from app.models import Job, JobStatus
from app.recovery import CLAIM_STALE_AFTER, reset_abandoned_claims


def _claimed_job(session, minutes_ago: float, status=JobStatus.claimed) -> Job:
    claimed_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    job = Job(status=status, joiner_fields={}, claimed_at=claimed_at)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def test_stale_claimed_job_is_reset_to_pending(session):
    stale = _claimed_job(session, minutes_ago=30)

    reset_ids = reset_abandoned_claims(session)

    assert reset_ids == [stale.id]
    session.refresh(stale)
    assert stale.status == JobStatus.pending
    assert stale.claimed_at is None
    assert any("resetting to pending for retry" in line for line in stale.log_lines)


def test_stale_running_job_is_also_reset(session):
    """running (already got at least one /log call) is just as eligible
    as claimed (never got one) - both mean "claimed but never finished".
    """
    stale = _claimed_job(session, minutes_ago=25, status=JobStatus.running)

    reset_ids = reset_abandoned_claims(session)

    assert reset_ids == [stale.id]


def test_recently_claimed_job_is_left_alone(session):
    """A connector that's genuinely still working shouldn't have its job
    yanked out from under it.
    """
    fresh = _claimed_job(session, minutes_ago=2)

    reset_ids = reset_abandoned_claims(session)

    assert reset_ids == []
    session.refresh(fresh)
    assert fresh.status == JobStatus.claimed


def test_pending_and_terminal_jobs_are_never_touched(session):
    pending = Job(status=JobStatus.pending, joiner_fields={})
    succeeded = Job(status=JobStatus.succeeded, joiner_fields={}, ad_upn="done@example.com")
    for j in (pending, succeeded):
        session.add(j)
    session.commit()

    reset_ids = reset_abandoned_claims(session)

    assert reset_ids == []


def test_reset_is_recorded_in_audit_log(client, dashboard_headers, session):
    stale = _claimed_job(session, minutes_ago=30)

    reset_abandoned_claims(session)

    entries = client.get("/audit-log", headers=dashboard_headers).json()
    matches = [e for e in entries if e["action"] == "job.reset_abandoned_claim" and f"job_id={stale.id}" in e["detail"]]
    assert len(matches) == 1


def test_staleness_threshold_matches_documented_value():
    # Pin the constant itself, not just behavior around it - a silent
    # change here would quietly change how long a real abandoned job sits
    # stuck before recovery, which is worth noticing in a diff.
    assert CLAIM_STALE_AFTER == timedelta(minutes=20)
