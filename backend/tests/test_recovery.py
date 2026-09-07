"""
Integration test for the interrupted-job recovery fix: a job left in
awaiting_cloud_steps (state that's already durable in Postgres/SQLite)
must get picked back up the next time the app starts - not stay stuck
forever just because a FastAPI background task doesn't survive a restart.

Entering TestClient as a context manager runs the real lifespan function
from main.py, so this exercises the actual startup code path, not a
reimplementation of it.
"""
import time

import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.main import app
from app.models import Config, GraphCredential, Job, JobStatus, SINGLETON_ID


@respx.mock
def test_stuck_job_is_resumed_when_app_restarts(session, monkeypatch):
    import app.graph_actions as ga
    monkeypatch.setattr(ga, "SYNC_POLL_ATTEMPTS", 1)
    monkeypatch.setattr(ga, "SYNC_POLL_INTERVAL_SECONDS", 0)

    # Set up state directly in the database, as if a previous process had
    # already gotten this far and then died mid-background-task.
    session.add(Config(id=SINGLETON_ID, cloud_group_rules=[], excel_field_map={}))
    session.add(GraphCredential(id=SINGLETON_ID, aad_tenant_id="t", client_id="c", client_secret="s"))
    stuck = Job(status=JobStatus.awaiting_cloud_steps, ad_upn="jamie@example.com", joiner_fields={})
    session.add(stuck)
    session.commit()
    session.refresh(stuck)
    stuck_id = stuck.id

    respx.post("https://login.microsoftonline.com/t/oauth2/v2.0/token").mock(
        return_value=Response(200, json={"access_token": "fake-token"})
    )
    respx.get(f"https://graph.microsoft.com/v1.0/users/{stuck.ad_upn}").mock(
        return_value=Response(200, json={"id": "user-1"})
    )
    respx.patch("https://graph.microsoft.com/v1.0/users/user-1").mock(return_value=Response(200))
    respx.get("https://graph.microsoft.com/v1.0/subscribedSkus").mock(return_value=Response(200, json={"value": []}))

    # Entering TestClient here IS the "restart" - lifespan runs and should
    # find + resume the stuck job automatically, with no request from
    # anyone triggering it.
    with TestClient(app):
        final_status = None
        deadline = time.time() + 5
        while time.time() < deadline:
            session.expire_all()
            job = session.get(Job, stuck_id)
            if job.status in (JobStatus.succeeded, JobStatus.failed):
                final_status = job
                break
            time.sleep(0.05)

    assert final_status is not None, "stuck job was never resumed within 5s of the app restarting"
    assert final_status.status == JobStatus.succeeded
    assert final_status.log_lines[-1] == "[OK] Hybrid provisioning complete."


def test_abandoned_claim_is_also_reset_on_startup(session):
    """Confirms main.py's startup sweep actually calls
    reset_abandoned_claims() - not just that the function works in
    isolation (test_recovery_claims.py already covers that), but that
    it's really wired into the same startup path as the cloud-steps
    recovery.
    """
    from datetime import datetime, timedelta, timezone

    stale = Job(
        status=JobStatus.claimed,
        joiner_fields={},
        claimed_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    session.add(stale)
    session.commit()
    session.refresh(stale)
    stale_id = stale.id

    with TestClient(app):
        pass  # the startup sweep runs synchronously before lifespan yields

    session.expire_all()
    resumed = session.get(Job, stale_id)
    assert resumed.status == JobStatus.pending
    assert resumed.claimed_at is None


def test_jobs_not_awaiting_cloud_steps_are_left_alone_on_restart(session):
    """A job that's merely pending (nobody's claimed it yet) or already
    finished shouldn't be touched by the recovery sweep - only jobs whose
    background task specifically could have been interrupted.
    """
    pending = Job(status=JobStatus.pending, joiner_fields={})
    done = Job(status=JobStatus.succeeded, joiner_fields={}, ad_upn="done@example.com")
    session.add(pending)
    session.add(done)
    session.commit()
    session.refresh(pending)
    session.refresh(done)

    with TestClient(app):
        time.sleep(0.2)  # give any (wrongly scheduled) task a moment to misbehave

    session.expire_all()
    assert session.get(Job, pending.id).status == JobStatus.pending
    assert session.get(Job, done.id).status == JobStatus.succeeded
