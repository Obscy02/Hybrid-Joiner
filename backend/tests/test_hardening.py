"""
Tests for the additional hardening pass: auth-failure lockout, security
headers, upload size limits, and job data retention.
"""
import io
from datetime import datetime, timedelta, timezone

import openpyxl
from fastapi.testclient import TestClient

from app.main import app
from app.models import Job, JobStatus
from app.rate_limit import MAX_FAILURES


# ---- security headers ---------------------------------------------------

def test_security_headers_present_on_every_response(client):
    resp = client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" in resp.headers


# ---- auth failure lockout -------------------------------------------------

def test_repeated_auth_failures_trigger_lockout(test_engine):
    # Fresh TestClient so this test's failure count doesn't mix with
    # anything the `client` fixture's own setup might have triggered.
    with TestClient(app) as c:
        for _ in range(MAX_FAILURES):
            resp = c.get("/config", headers={"Authorization": "Bearer wrong"})
            assert resp.status_code == 401

        # One more attempt - even with valid-shaped auth - should now be
        # locked out rather than evaluated at all.
        locked_resp = c.get("/config", headers={"Authorization": "Bearer wrong"})
        assert locked_resp.status_code == 429


def test_lockout_is_keyed_per_source_not_global(test_engine):
    """Two different callers (distinguished here by X-Forwarded-For, the
    same header a real Azure App Service deployment would set from the
    actual origin IP) must not share a lockout bucket - one attacker
    guessing repeatedly shouldn't be able to lock out everyone else too.
    """
    with TestClient(app) as c:
        for _ in range(MAX_FAILURES):
            resp = c.get(
                "/config",
                headers={"Authorization": "Bearer wrong", "X-Forwarded-For": "1.1.1.1"},
            )
            assert resp.status_code == 401

        # 1.1.1.1 is now locked out...
        locked = c.get(
            "/config", headers={"Authorization": "Bearer wrong", "X-Forwarded-For": "1.1.1.1"}
        )
        assert locked.status_code == 429

        # ...but a different source IP is entirely unaffected.
        other = c.get(
            "/config", headers={"Authorization": "Bearer wrong", "X-Forwarded-For": "2.2.2.2"}
        )
        assert other.status_code == 401


def test_successful_request_resets_failure_count(test_engine):
    with TestClient(app) as c:
        for _ in range(MAX_FAILURES - 1):
            c.get("/config", headers={"Authorization": "Bearer wrong"})

        # A success just under the threshold should clear the count...
        c.get("/health")

        # ...so this next failure alone shouldn't trigger a lockout.
        resp = c.get("/config", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401  # not 429


# ---- upload size limit ---------------------------------------------------

def test_oversized_upload_is_rejected(configured_client, monkeypatch):
    import app.routes.jobs as jobs_module
    monkeypatch.setattr(jobs_module, "MAX_UPLOAD_BYTES", 100)  # tiny, for a fast test

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["E6"] = "x" * 1000  # comfortably over the 100-byte test limit once serialized
    buf = io.BytesIO()
    wb.save(buf)

    resp = configured_client["client"].post(
        "/preview-excel",
        headers=configured_client["client_headers"],
        files={"file": ("form.xlsx", buf.getvalue())},
    )
    assert resp.status_code == 413


# ---- job retention ---------------------------------------------------

def test_purge_deletes_only_old_terminal_jobs(client, dashboard_headers, session, test_engine):
    from sqlmodel import Session as SQLModelSession

    old_cutoff = datetime.now(timezone.utc) - timedelta(days=200)

    old_done = Job(status=JobStatus.succeeded, joiner_fields={}, ad_upn="old@example.com", created_at=old_cutoff)
    recent_done = Job(status=JobStatus.succeeded, joiner_fields={}, ad_upn="recent@example.com")
    old_but_pending = Job(status=JobStatus.pending, joiner_fields={}, created_at=old_cutoff)
    for j in (old_done, recent_done, old_but_pending):
        session.add(j)
    session.commit()
    old_done_id, recent_done_id, old_but_pending_id = old_done.id, recent_done.id, old_but_pending.id

    resp = client.post("/jobs/purge", headers=dashboard_headers, params={"older_than_days": 90})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1

    # A fresh session, not the one that originally loaded these objects -
    # the route's own session already deleted a row out from under it, and
    # re-querying via a stale identity-mapped instance raises
    # ObjectDeletedError instead of just reporting it's gone.
    with SQLModelSession(test_engine) as fresh:
        assert fresh.get(Job, old_done_id) is None
        assert fresh.get(Job, recent_done_id) is not None
        assert fresh.get(Job, old_but_pending_id) is not None  # never purged, no matter its age


def test_purge_requires_admin(configured_client):
    resp = configured_client["client"].post(
        "/jobs/purge", headers=configured_client["client_headers"], params={"older_than_days": 90}
    )
    assert resp.status_code == 401


def test_purge_rejects_invalid_retention_window(client, dashboard_headers):
    resp = client.post("/jobs/purge", headers=dashboard_headers, params={"older_than_days": 0})
    assert resp.status_code == 400
