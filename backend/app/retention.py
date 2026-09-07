"""
Job records hold employee PII (names, sites, departments) with no
automatic expiry otherwise - this is what lets an operator actually clear
old ones out rather than have them accumulate indefinitely, which matters
for a system holding real personal data.

Deliberately manual/on-demand (triggered via POST /jobs/purge) rather than
a background schedule: there's no live environment here to verify a
recurring job actually fires reliably, and a manual trigger is easy to
wire into a real scheduled task (Azure's Logic Apps, a WebJob, cron
hitting the endpoint) once this is actually deployed somewhere.
"""
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from .models import Job, JobStatus


def purge_old_jobs(session: Session, older_than_days: int) -> int:
    """Deletes jobs older than the given retention window - only ones in a
    terminal state (succeeded/failed). A job still pending/claimed/running/
    awaiting_cloud_steps is never purged, no matter its age - deleting
    something still potentially in progress could interfere with the
    connector or a background task still expecting to find it.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    stale_jobs = session.exec(
        select(Job).where(
            Job.created_at < cutoff,
            Job.status.in_([JobStatus.succeeded, JobStatus.failed]),
        )
    ).all()

    count = len(stale_jobs)
    for job in stale_jobs:
        session.delete(job)
    session.commit()
    return count
